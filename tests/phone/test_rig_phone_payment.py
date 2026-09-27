# First phone-payment E2E through the rig — beta DUT (OpenWrt 24.10.8,
# tollgate-wrt v0.6.0-alpha4), moto g(7) power (LineageOS, Android 15) over
# WiFi, driven via uiautomator2 per ~/android-tooling-brief.md.
#
# Flow: phone joins beta's open TollGate SSID (TollGate-E782) via
# `cmd wifi connect-network` (non-privileged on Android 15), opens the
# captive portal with a TIP-03 prehydrated Cashu token
# (splash.html?token=... -> window.__INITIAL_TOKEN__, consumed by the SPA on
# first render — the browser equivalent of pay_direct()), the backend
# consumes the token and authenticates the phone's MAC with NDS, and the
# phone's network flips to VALIDATED with working internet.
#
# Payment path: one-intent TIP-03 prehydration. Fallbacks, in order:
#   1. tap the portal's purchase/submit button (SPA prehydrated the field
#      but waits for confirmation);
#   2. u2 set_text the token into the field (u2 round-trips 400+ chars
#      exactly; adb `input text` silently truncates near 200 and is never
#      used here).
#
# Auth assertions deliberately avoid router.wait_for_auth(): its >10s
# fallback runs `ndsctl auth <mac>` manually, which would grant internet
# even if the payment never went through. This test must only pass on a
# REAL payment, so NDS state is polled directly. After authentication is
# observed, the NDS 5.0.2 auth-mark repair (fix_nodogsplash_auth_marks —
# a packet-mark fix for already-authenticated clients, not an auth grant)
# is applied so new connections from the phone are accepted.

import logging
import os
import time

import pytest

from lib.clients.evidence import EvidenceRecorder
from lib.clients.u2phone import U2Phone, connect_u2
from lib.constants import TOKEN_DEFAULT
from lib.helpers import assert_internet, assert_session_active

log = logging.getLogger("tollgate.test_rig_phone_payment")

pytestmark = [
    pytest.mark.phone,
    pytest.mark.physical_hardware,
    pytest.mark.slow,
    # connected_wifi: connect to the SSID only; this test drives the portal
    # itself via the TIP-03 URL. (The fixture's default portal open targets
    # port 80, which is not the portal on this rig — the SPA is served by
    # uhttpd on :2051 and the :2050 NDS page is a redirect shim that
    # preserves ?token=.)
    pytest.mark.pay_via("skip"),
]

PORTAL_PORT = 2051
AUTHED_STATES = ("authed", "countdown", "usage_dashboard")
READY_STATES = ("portal_ready", "token_typing")
SUBMIT_TEXTS = ("Purchase", "Pay", "Submit", "Connect", "Go")


@pytest.fixture(scope="session")
def d():
    serial = os.environ.get("PHONE_SERIAL", "")
    if not serial:
        pytest.skip("PHONE_SERIAL not set")
    return connect_u2(serial)


@pytest.fixture(scope="session")
def u2phone(d):
    return U2Phone(d, pin=os.environ.get("PHONE_PIN", ""))


@pytest.fixture
def evidence(d, results_dir):
    """Video + step-screenshot capture for the whole payment run.

    Listed FIRST in the test signature so the video starts before the
    wifi join. 180 s is screenrecord's hard per-file cap; the extra
    headroom over the plan's example 120 s keeps the internet-unlocked
    moment on tape when fixtures run long."""
    rec = EvidenceRecorder(d, os.environ.get("PHONE_SERIAL", ""),
                           os.path.join(results_dir, "artifacts"))
    rec.start_video(limit_s=180)
    yield rec
    rec.stop_video(pull=True)
    rec.write_manifest()


@pytest.fixture
def preconnected(u2phone, wifi):
    """Join the TollGate SSID via `cmd wifi` before connected_wifi runs.

    Works behind a locked keyguard (no settings UI). When already
    associated, connected_wifi's fast-path skips its 1080p-assuming UI flow
    entirely."""
    u2phone.ensure_awake()
    if not wifi.is_connected():
        out = u2phone.wifi_connect(wifi.ssid, "open")
        log.info("cmd wifi connect-network %r: %s", wifi.ssid, out[:200])
        for _ in range(10):
            if wifi.is_connected():
                break
            time.sleep(3)
    connected = wifi.is_connected()
    log.info("preconnected: %s (ssid=%s)", connected, wifi.ssid)
    return connected


def _pay_through_portal(u2phone, token: str, timeout: int = 75) -> str:
    """Drive the portal to an authenticated state.

    TIP-03 prehydration should auto-pay; if the SPA stalls, tap the submit
    button, and as a last resort type the token with u2 set_text."""
    start = time.time()
    tapped = typed = False
    state = ""
    while time.time() - start < timeout:
        state = u2phone.portal_state()
        if state in AUTHED_STATES:
            log.info("Portal authenticated (state=%s) after %ds",
                     state, int(time.time() - start))
            return state
        if state in READY_STATES:
            elapsed = time.time() - start
            if not tapped and elapsed > 12:
                tapped = u2phone.tap_button(*SUBMIT_TEXTS)
                if tapped:
                    log.info("Tapped submit button after %ds", int(elapsed))
            if not typed and tapped and elapsed > 25 and state == "token_typing":
                log.info("Typing token via u2 set_text (prehydrate empty?)")
                typed = u2phone.type_token(token)
                if typed:
                    u2phone.tap_button(*SUBMIT_TEXTS)
        time.sleep(3)
    log.warning("Portal not authenticated within %ds (last state=%r)",
                timeout, state)
    return state


def _wait_nds_authenticated(router, timeout: int = 45) -> bool:
    """Poll NDS for the phone MAC — no manual `ndsctl auth` fallback."""
    start = time.time()
    while time.time() - start < timeout:
        if router.get_nds_state() == "Authenticated":
            # NDS 5.0.2 auth-mark repair: authenticated clients' packets are
            # marked 0x30000 but the ndsNET accept rule tests 0x20000/0x30000,
            # so NEW connections are REJECTed. This inserts the client-agnostic
            # auth-bit accept rule — repairs gating for already-authenticated
            # clients, does not authenticate anyone.
            router.fix_nodogsplash_auth_marks()
            log.info("NDS Authenticated after %ds", int(time.time() - start))
            return True
        time.sleep(2)
    return False


def test_rig_phone_payment_e2e(evidence, preconnected, u2phone, router, adb,
                               cashu, wifi, connected_wifi, screenshot_portal):
    assert preconnected, (
        f"Phone could not join {wifi.ssid} via cmd wifi connect-network"
    )
    evidence.shot(
        "01-wifi-connected",
        "Android WiFi settings/status showing the phone connected to the "
        "open SSID TollGate-E782 with a 192.168.103.x IP address",
    )

    token = cashu.mint(TOKEN_DEFAULT)
    log.info(
        "Minted %d-sat token (%d chars) from %s",
        TOKEN_DEFAULT, len(token), getattr(cashu, "mint_url", "<unknown>"),
    )

    assert u2phone.ensure_unlocked(), (
        "Phone keyguard locked and could not be unlocked "
        "(set PHONE_PIN per rig secret convention)"
    )
    screenshot_portal("rig-payment-portal-open.png")

    portal_url = (
        f"http://{router.host}:{PORTAL_PORT}/splash.html?token={token}"
    )
    log.info("Opening portal with prehydrated token (%d char URL)",
             len(portal_url))
    adb.force_stop_browser()
    u2phone.open_url(portal_url)

    u2phone.wait_web_input(timeout=20)
    u2phone.dismiss_translate()

    evidence.shot(
        "02-portal-opened",
        "Chrome showing the TollGate captive portal (address bar with "
        "192.168.103.51:2051), token input visible, no translate popup "
        "covering the page",
    )

    state = _pay_through_portal(u2phone, token)
    assert state in AUTHED_STATES, (
        f"Portal did not authenticate after token payment (last state={state!r})"
    )

    evidence.shot(
        "03-portal-paid",
        "TollGate portal showing an authenticated/paid state (countdown, "
        "remaining data, or success message) — not the token entry form",
    )

    assert _wait_nds_authenticated(router), (
        "NDS did not authenticate the phone after payment — payment likely "
        "did not reach the backend"
    )

    screenshot_portal("rig-payment-authed.png")

    session = assert_session_active(router)
    log.info("Session active: %s", str(session)[:200])

    assert u2phone.wait_wifi_validated(wifi.ssid), (
        "Phone's WIFI network did not reach VALIDATED after payment"
    )
    assert u2phone.ping_ok("1.1.1.1"), "No internet on phone after payment"
    assert assert_internet(adb, "1.1.1.1"), "PRTA assert_internet failed"

    evidence.shot(
        "04-payment-authed",
        "Android status bar with full WiFi connectivity (no captive portal "
        "/'sign in to network' icon) after the Cashu payment",
    )

    u2phone.open_url("http://example.com/")
    time.sleep(4)
    evidence.shot(
        "05-internet-unlocked",
        "Chrome loading a real internet page (example.com content, not a "
        "captive portal and not an error page)",
    )

    screenshot_portal("rig-payment-internet.png")
