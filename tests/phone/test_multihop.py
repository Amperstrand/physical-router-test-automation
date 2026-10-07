# Multihop payment test: alpha (seller) → WiFi 5GHz → beta (reseller) → WiFi 2.4GHz → phone
#
# Topology:
#   Internet → Main Router → alpha (seller)
#                         → WiFi 5GHz STA → beta (reseller, buys from alpha)
#                                          → WiFi 2.4GHz AP → Phone
#
# Prerequisites:
#   - alpha: TollGate running, seller mode, private SSID <nym>-<code> on 5GHz
#   - beta: TollGate running, reseller_mode=true, WiFi STA to alpha on 5GHz,
#            client AP broadcasting the private SSID on 2.4GHz (br-private),
#            NoDogSplash on br-private, testnut mint accepted,
#            wallet funded, upstream buying session active
#   - Phone connected to beta's private SSID via WiFi AND USB/ADB
#
# The reseller SSID is resolved from the ROUTER at runtime (device code +
# private-SSID prefix, default nym c08r4d0r). Override with RESELLER_SSID.
#
# Run:
#   PHONE_SERIAL=R5CR508MD9R TOLLGATE_ROUTER_ID=beta \
#     pytest tests/phone/test_multihop.py -v --no-deploy

import json
import logging
import os
import time

import pytest

from lib.router import Router
from lib.cashu import CashuMint
from lib.clients.adb import ADBDevice
from lib.helpers import assert_internet, is_session_event, assert_session_active
from lib.ssid import LEGACY_RESELLER_SSID, resolve_reseller_ssid

log = logging.getLogger("tollgate.multihop")

pytestmark = [pytest.mark.story("P9"),pytest.mark.phone, pytest.mark.timeout(120)]

SCRIPT_DIR = os.path.join(os.path.dirname(__file__), "..", "..")


def _load_router(router_id: str) -> Router:
    inventory_path = os.environ.get(
        "TOLLGATE_ROUTER_INVENTORY",
        os.path.join(SCRIPT_DIR, "config", "routers.json"),
    )
    with open(inventory_path) as f:
        inventory = json.load(f)
    entry = inventory["routers"][router_id]
    identity_file = os.environ.get("TOLLGATE_SSH_KEY", "") or None
    return Router(
        host=entry["sshHost"],
        phone_ip="",
        phone_mac="",
        domain="",
        identity_file=identity_file,
        jump_host=entry.get("jumpHost") or None,
    )


@pytest.fixture(scope="module")
def router_a():
    return _load_router("beta")


@pytest.fixture(scope="module")
def router_b():
    return _load_router("alpha")


@pytest.fixture(scope="module")
def reseller_ssid(router_a) -> str:
    """RESELLER_SSID env override → router-resolved private SSID → legacy literal."""
    ssid = resolve_reseller_ssid(router_a.ssh, fallback=LEGACY_RESELLER_SSID)
    log.info("Reseller SSID resolved: %s", ssid)
    return ssid


@pytest.fixture(scope="module")
def adb():
    serial = os.environ.get("PHONE_SERIAL", "")
    pin = os.environ.get("PHONE_PIN", "")
    return ADBDevice(serial=serial or None, pin=pin or None)


@pytest.fixture(scope="module")
def cashu():
    return CashuMint()


def test_01_infrastructure_healthy(router_a, router_b):
    """Both routers have TollGate running."""
    for r, label in [(router_b, "router-b"), (router_a, "router-a")]:
        pid = r.ssh("pidof tollgate-wrt").strip()
        assert pid, f"TollGate not running on {label}"
        adv = r.ssh("wget -qO- http://127.0.0.1:2121/ 2>/dev/null | head -c 30")
        assert '"kind"' in adv, f"TollGate not responding on {label}:2121"
        log.info(f"{label}: TollGate healthy (pid={pid})")

    config = router_a.ssh("cat /etc/tollgate/config.json")
    assert "reseller_mode" in config and "true" in config, "Router-a not in reseller mode"


def test_02_phone_on_reseller_wifi(adb, router_a, reseller_ssid):
    """Phone is connected to the reseller's 2.4GHz AP."""
    wifi_info = adb.shell("dumpsys wifi 2>/dev/null | grep 'mWifiInfo'").strip()
    assert reseller_ssid in wifi_info, \
        f"Phone not on {reseller_ssid}. Current: {wifi_info[:100]}"
    ip = adb.wifi_ip()
    assert ip, "Phone has no WiFi IP"
    router_a.phone_ip = ip
    router_a.phone_mac = adb.wifi_mac()
    log.info(f"Phone: IP={ip} MAC={router_a.phone_mac}")


def test_03_captive_portal_visible(adb):
    """Captive portal page is rendered — NDS intercepts HTTP."""
    adb.shell("am start -a android.intent.action.VIEW -d 'http://example.com'")
    time.sleep(5)
    xml = adb.ui_xml()
    has_portal = "Tollgate Captive Portal" in xml or "Purchase Internet Access" in xml
    if not has_portal:
        adb.shell("am start -a android.intent.action.VIEW -d 'http://192.168.2.1/splash.html'")
        time.sleep(4)
        xml = adb.ui_xml()
        has_portal = "Tollgate Captive Portal" in xml or "Purchase Internet Access" in xml
    assert has_portal, "Captive portal page not visible"
    log.info("Captive portal page confirmed")


def test_04_pay_and_verify_multihop(router_a, adb, cashu):
    """Pay with Cashu token, verify internet through full multihop chain."""
    token = cashu.mint(1)
    log.info(f"Minted token: {token[:40]}...")

    router_a.resolve_phone_client(adb)
    resp = router_a.pay_direct(token)
    log.info(f"Payment: kind={resp.get('kind')}, raw={str(resp)[:200]}")

    assert is_session_event(resp), \
        f"Payment failed on reseller: {str(resp)[:300]}"

    assert router_a.wait_for_auth(timeout=30), \
        "Not authenticated after 30s"

    assert_session_active(router_a)
    log.info("Phone authenticated, session active")

    assert assert_internet(adb, "1.1.1.1", retries=5), \
        "No internet after payment — multihop chain broken!"
    log.info("SUCCESS: Internet verified through full multihop chain!")


def test_05_multihop_path_verified(adb, router_a):
    """Verify traffic goes through router-a → router-b → internet."""
    assert adb.ping("10.0.3.1", count=2, timeout=5), \
        "Cannot reach upstream gateway 10.0.3.1 — multihop path broken"
    log.info("Multihop path confirmed: phone → router-a → router-b (10.0.3.1) → internet")

    time.sleep(2)
    usage_log = router_a.ssh("logread 2>/dev/null | grep 'upstream usage' | tail -3")
    log.info(f"Upstream usage: {usage_log.strip()}")
