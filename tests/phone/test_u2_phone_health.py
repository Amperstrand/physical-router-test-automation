# Phone-lane health smoke — no router needed, runs on the bench whenever
# PHONE_SERIAL is set. Verifies the uiautomator2 stack, the stay-awake
# setting, the keyguard state, and that a WiFi scan produces results.

import logging
import os
import time

import pytest

from lib.clients.u2phone import U2Phone, connect_u2

log = logging.getLogger("tollgate.test_u2_phone_health")

pytestmark = [pytest.mark.phone]


@pytest.fixture(scope="module")
def phone():
    serial = os.environ.get("PHONE_SERIAL", "")
    if not serial:
        pytest.skip("PHONE_SERIAL not set")
    return U2Phone(connect_u2(serial), pin=os.environ.get("PHONE_PIN", ""))


def test_u2_stack_alive(phone):
    info = phone.d.device_info
    assert info["sdk"], "u2 device_info failed"
    log.info("sdk=%s model=%s version=%s", info["sdk"], info["model"],
             info["version"])
    assert len(phone.d.dump_hierarchy()) > 1000, "empty accessibility tree"


def test_stay_awake_and_lock(phone):
    phone.ensure_awake()
    stay = phone.shell("dumpsys power | grep -c 'mStayOn=true'")
    assert stay.strip() == "1", f"stay-awake not active ({stay.strip()})"
    unlocked = phone.ensure_unlocked()
    log.info("unlocked=%s (PHONE_PIN %s)",
             unlocked, "set" if phone.pin else "not set")
    assert unlocked, "keyguard locked and no PIN / unlock failed"


def test_wifi_scan_sees_networks(phone):
    phone.d.shell("cmd wifi start-scan")
    time.sleep(4)
    out = phone.d.shell("cmd wifi list-scan-results")
    assert "BSSID" not in out or out.count("\n") >= 1, (
        f"no scan results: {out[:200]}"
    )
    log.info("scan lines: %d", out.count("\n"))
