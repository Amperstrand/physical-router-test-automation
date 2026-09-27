# Wallet-driven phone payment E2E through the rig — the real user flow.
#
# The phone's cashu.me web wallet (self-hosted on the bench host, preauth-
# allowed through NDS) holds tokens from the rig mint; it SENDS a Cashu
# token which is pasted into the DUT's captive portal; the backend consumes
# it, NDS authenticates the phone, and the network flips to VALIDATED.
#
# Token format: the deployed cashu.me emits V4 (cashuB, CBOR). The DUT
# firmware (TMBG main @5f2201e4, gonuts-tollgate fork v0.12.1) decodes
# V1/V3/V4 x keyset V1/V2 — V4 is the plan of record (tollgate verified in
# fork source; empirical 5s discriminator runs before the recorded take).
#
# Driving model (all mechanics rehearsed green):
#   - wallet tab via CDP: tab hygiene (single-tab guard!), Page.bringToFront
#     (background tabs freeze and evaluates hang), Vue-visible input
#     setting, TRUSTED clicks via Input.dispatchMouseEvent — the send
#     button MUST be located by [data-testid=send-ecash]: a decoy mounted
#     'SEND' button exists and text-find clicks on it silently no-op.
#   - seeding via u2 clipboard: host mints a canonical-URL token,
#     d.set_clipboard, the wallet's Receive sheet 'Paste' block reads it
#     (one-time Chrome clipboard permission 'Tillat' per origin).
#   - portal tab via CDP: set the token input value, click submit.
#
# Auth assertions are honest: NDS state is polled directly (no manual
# `ndsctl auth` fallback — that would grant internet without a payment),
# then the NDS 5.0.2 auth-mark repair is applied (packet-mark fix for
# already-authenticated clients, not an auth grant).

import logging
import os
import re
import time

import pytest

from lib.clients.cdp_wallet import CdpTab
from lib.clients.evidence import EvidenceRecorder
from lib.clients.u2phone import U2Phone, connect_u2
from lib.constants import TOKEN_DEFAULT
from lib.helpers import assert_internet, assert_session_active

log = logging.getLogger("tollgate.test_rig_wallet_payment")

pytestmark = [
    pytest.mark.phone,
    pytest.mark.physical_hardware,
    pytest.mark.slow,
    pytest.mark.pay_via("skip"),
]

WALLET_ORIGIN = os.environ.get(
    "TOLLGATE_WALLET_URL", "http://192.168.105.2:3080").rstrip("/")
MINT_URL = os.environ.get("TOLLGATE_TEST_MINT_URL")
AMOUNT = int(os.environ.get("TOLLGATE_WALLET_SEND_AMOUNT", "4"))
AUTHED_STATES = ("authed", "countdown", "usage_dashboard")


@pytest.fixture(scope="session")
def d():
    serial = os.environ.get("PHONE_SERIAL", "")
    if not serial:
        pytest.skip("PHONE_SERIAL not set")
    return connect_u2(serial)


@pytest.fixture(scope="session")
def u2phone(d):
    return U2Phone(d, pin=os.environ.get("PHONE_PIN", ""))


@pytest.fixture(scope="session")
def wallet_tab(d):
    tab = CdpTab(WALLET_ORIGIN.split("//", 1)[1], adb_serial=d.serial)
    tab.close_duplicate_tabs()
    tab.connect()
    return tab


@pytest.fixture
def evidence(d, results_dir):
    rec = EvidenceRecorder(d, d.serial,
                           os.path.join(results_dir, "artifacts"))
    rec.start_video(limit_s=180)
    yield rec
    rec.stop_video(pull=True)
    rec.write_manifest()


def _wallet_balance_sat(wallet_tab) -> int:
    body = wallet_tab.ev("document.body.innerText") or ""
    m = re.search(r"₿\s*([\d.]+)", body)
    return int(float(m.group(1))) if m else 0


def _seed_wallet(u2phone, wallet_tab, cashu) -> None:
    """Top up the wallet from the rig mint via clipboard + Receive>Paste."""
    token = cashu.mint(TOKEN_DEFAULT)
    log.info("minted %d-sat seed token (%d chars) from %s",
             TOKEN_DEFAULT, len(token), getattr(cashu, "mint_url", "?"))
    u2phone.d.set_clipboard(token)
    time.sleep(1)
    wallet_tab.ev(
        "(() => { const b = Array.from(document.querySelectorAll('button'))"
        ".find(b => b.innerText.trim() === 'RECEIVE');"
        " if (!b) return 'NO_RECEIVE'; b.click(); return 'OPENED'; })()")
    time.sleep(2.5)
    # Ecash tab, then the Paste block (clipboard read; permission may
    # prompt once per origin — u2 taps 'Tillat').
    u2phone.d(text="Ecash").click()
    time.sleep(1.5)
    u2phone.d.swipe(0.5, 0.72, 0.5, 0.30, 0.4)
    time.sleep(1)
    u2phone.d.click(360, 790)
    time.sleep(2.5)
    h = u2phone.d.dump_hierarchy()
    if "Tillat" in h:
        u2phone.d(text="Tillat").click()
        time.sleep(3)
    ok = wallet_tab.ev(
        "(() => { const b = Array.from(document.querySelectorAll('button'))"
        ".find(b => b.innerText.trim() === 'RECEIVE');"
        " if (!b) return 'NO_BTN'; b.click(); return 'CLICKED'; })()")
    log.info("redeem RECEIVE: %s", ok)
    time.sleep(5)


def _send_from_wallet(wallet_tab, amount: int) -> str:
    """Keypad the amount, trusted-click the real send, return the token."""
    for digit in str(amount):
        wallet_tab.ev(
            f"(() => {{ const b = Array.from("
            f"document.querySelectorAll('button'))"
            f".find(b => b.innerText.trim() === '{digit}');"
            f" if (!b) return false; b.click(); return true; }})()")
        time.sleep(0.6)

    rect = wallet_tab.ev(
        "(() => { const b = document.querySelector("
        "'[data-testid=send-ecash]');"
        " if (!b || b.disabled) return null; const r = b.getBoundingClientRect();"
        " return JSON.stringify({x: r.x + r.width / 2,"
        " y: r.y + r.height / 2}); })()")
    assert rect, "send button missing/disabled (amount not registered?)"
    import json as _json
    r = _json.loads(rect)
    ws = wallet_tab._ws
    for ctype in ("mousePressed", "mouseReleased"):
        ws.send(_json.dumps({
            "id": 9, "method": "Input.dispatchMouseEvent",
            "params": {"type": ctype, "x": r["x"], "y": r["y"],
                       "button": "left", "clickCount": 1}}))
        time.sleep(0.15)

    for _ in range(10):
        time.sleep(2)
        body = wallet_tab.ev("document.body.innerText") or ""
        if "Pending Ecash" in body:
            break
    else:
        raise AssertionError("wallet did not produce a Pending Ecash dialog")

    wallet_tab.ev(
        "(() => { const b = Array.from(document.querySelectorAll('button'))"
        ".find(b => b.innerText.trim() === 'COPY');"
        " if (!b) return 'NO_BTN'; b.click(); return 'CLICKED'; })()")
    time.sleep(1.5)
    token = wallet_tab.ev(
        "navigator.clipboard.readText().catch(() => '')",
        await_promise=True, timeout=10) or ""
    m = re.search(r"cashu[AB][A-Za-z0-9_\-]{40,}", str(token))
    assert m, f"no token on clipboard after COPY: {str(token)[:80]}"
    log.info("wallet produced %d-char %s token", len(m.group(0)),
             m.group(0)[:6])
    return m.group(0)


def _pay_portal(u2phone, wallet_tab, router, token: str) -> str:
    """Open the DUT portal, paste the token, submit, return portal state."""
    portal = f"http://{router.host}:2051/splash.html"
    u2phone.open_url(portal)
    time.sleep(5)
    u2phone.dismiss_translate()

    ptab = CdpTab(f"{router.host}:2051", adb_serial=u2phone.d.serial)
    ptab.close_duplicate_tabs()
    ptab.connect()
    ok = ptab.ev(
        f"(() => {{ const t = document.querySelector("
        f"'textarea, input[type=text]');"
        f" if (!t) return 'NO_INPUT';"
        f" const set = Object.getOwnPropertyDescriptor("
        f"t.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype"
        f" : window.HTMLInputElement.prototype, 'value').set;"
        f" set.call(t, {token!r});"
        f" t.dispatchEvent(new Event('input', {{bubbles: true}}));"
        f" const b = Array.from(document.querySelectorAll('button'))"
        f".find(b => /purchase|pay|submit|connect|go/i.test(b.innerText));"
        f" if (!b) return 'NO_BUTTON'; b.click(); return 'SUBMITTED'; }})()")
    log.info("portal submit: %s", ok)
    assert ok == "SUBMITTED", f"portal drive failed: {ok}"

    deadline = time.time() + 60
    state = ""
    while time.time() < deadline:
        state = ptab.ev(
            "(() => { const m = document.body.innerHTML.match("
            "/data-sm=.([a-z_]+)/); return m ? m[1] : ''; })()") or ""
        if state in AUTHED_STATES:
            return state
        time.sleep(3)
    return state


def _wait_nds_authenticated(router, timeout: int = 45) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if router.get_nds_state() == "Authenticated":
            router.fix_nodogsplash_auth_marks()
            log.info("NDS Authenticated after %ds", int(time.time() - start))
            return True
        time.sleep(2)
    return False


@pytest.fixture
def preconnected(u2phone, wifi):
    u2phone.ensure_awake()
    if not wifi.is_connected():
        out = u2phone.wifi_connect(wifi.ssid, "open")
        log.info("cmd wifi connect-network %r: %s", wifi.ssid, out[:120])
        for _ in range(10):
            if wifi.is_connected():
                break
            time.sleep(3)
    return wifi.is_connected()


def test_rig_wallet_payment_e2e(evidence, preconnected, u2phone, wallet_tab,
                                router, adb, cashu, wifi, connected_wifi):
    assert preconnected, f"phone could not join {wifi.ssid}"
    assert MINT_URL, "TOLLGATE_TEST_MINT_URL not set"
    evidence.shot(
        "01-wifi-connected",
        "Phone connected to the TollGate SSID with a rig-subnet IP",
    )

    assert u2phone.ensure_unlocked(), "keyguard locked (set PHONE_PIN)"
    wallet_tab.close_duplicate_tabs()
    wallet_tab.connect()
    if _wallet_balance_sat(wallet_tab) < AMOUNT:
        log.info("wallet underfunded — seeding from %s", MINT_URL)
        _seed_wallet(u2phone, wallet_tab, cashu)
    bal = _wallet_balance_sat(wallet_tab)
    assert bal >= AMOUNT, f"wallet balance {bal} < {AMOUNT} after seed"

    token = _send_from_wallet(wallet_tab, AMOUNT)
    evidence.shot(
        "02-wallet-paid",
        "cashu.me wallet showing the Pending Ecash dialog with the created "
        "token amount and a COPY button",
    )

    state = _pay_portal(u2phone, wallet_tab, router, token)
    assert state in AUTHED_STATES, (
        f"portal did not authenticate after wallet payment (state={state!r})"
    )

    assert _wait_nds_authenticated(router), (
        "NDS did not authenticate the phone after payment — payment likely "
        "did not reach the backend"
    )
    evidence.shot(
        "03-portal-authed",
        "TollGate portal showing an authenticated/paid state (countdown or "
        "remaining data), not the token entry form",
    )

    session = assert_session_active(router)
    log.info("session active: %s", str(session)[:200])

    assert u2phone.wait_wifi_validated(wifi.ssid), (
        "phone's WIFI network did not reach VALIDATED after payment"
    )
    assert u2phone.ping_ok("1.1.1.1"), "no internet on phone after payment"
    assert assert_internet(adb, "1.1.1.1"), "PRTA assert_internet failed"

    u2phone.open_url("http://example.com/")
    time.sleep(4)
    evidence.shot(
        "04-internet-unlocked",
        "Chrome loading a real internet page (example.com content, not a "
        "portal and not an error page)",
    )
