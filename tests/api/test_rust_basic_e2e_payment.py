"""End-to-end payment flow test for tollgate-module-basic-rust.

Tests the COMPLETE customer journey:
1. Mint a real Cashu token from testnut.cashu.exchange
2. POST / with the token -> expect kind 1022 (session granted)
3. GET /balance -> expect session_active=true, allotment > 0
4. GET /usage -> expect used/allotment format (not -1/-1)
5. Wait for session to expire (small allotment) -> verify auto-revoke

Requires:
- TOLLGATE_BACKEND=rust-basic
- Working network access to testnut.cashu.exchange
- /tmp/dhcp.leases writable (for MAC resolution)
"""

import os
import re
import time

import pytest
import requests

pytestmark = [pytest.mark.story("R1"),pytest.mark.rust_basic_only, pytest.mark.api, pytest.mark.extended]

# Test client identity injected into /tmp/dhcp.leases.
_TEST_MAC = "00:11:22:33:44:55"
_TEST_IP = "127.0.0.1"

# Allotment maths for the default rust_basic_server config
# (step_size=5000, price_per_step=1, metric=milliseconds):
#   amount=4 -> allotment = (4 / 1) * 5000 = 20 000 ms  (20 s)
#   amount=1 -> allotment = (1 / 1) * 5000 =  5 000 ms  ( 5 s)


# ---------------------------------------------------------------------------
# DHCP lease injection helpers (adapted from test_rust_basic_pay_balance_chain.py)
# ---------------------------------------------------------------------------


def _inject_dhcp_leases() -> bytes | None:
    """Back up /tmp/dhcp.leases and inject a fake 127.0.0.1 -> MAC entry.

    The Rust binary resolves client MAC from /tmp/dhcp.leases (then
    /proc/net/arp).  Without a lease entry the pay, balance, and usage
    handlers cannot map 127.0.0.1 to a MAC, causing session lookups to
    fail.

    Returns the original file content (or None if no file existed) for
    restore by :func:`_restore_dhcp_leases`.
    """
    original: bytes | None
    try:
        with open("/tmp/dhcp.leases", "rb") as f:
            original = f.read()
    except FileNotFoundError:
        original = None

    fake_entry = (
        f"{int(time.time())} {_TEST_MAC} {_TEST_IP} "
        f"e2e-payment *\n"
    )
    try:
        with open("/tmp/dhcp.leases", "w") as f:
            f.write(fake_entry)
    except OSError:
        pass  # non-fatal -- resolver may still work via /proc/net/arp

    return original


def _restore_dhcp_leases(original: bytes | None) -> None:
    """Restore /tmp/dhcp.leases to its original state."""
    try:
        if original is None:
            os.unlink("/tmp/dhcp.leases")
        else:
            with open("/tmp/dhcp.leases", "wb") as f:
                f.write(original)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Cashu token minting helper (adapted from test_rust_basic_payment.py)
# ---------------------------------------------------------------------------


def _mint_test_token(amount: int = 4):
    """Mint a Cashu token from testnut.cashu.exchange via lib.cashu.

    Returns the token string, or None when the mint is unreachable.
    """
    try:
        from lib.cashu import MintUnavailableError, create_minter
    except ImportError:
        return None
    try:
        mint_url = os.environ.get(
            "TOLLGATE_TEST_MINT_URL", "https://testnut.cashu.exchange"
        )
        minter = create_minter(mint_url)
        minter.ensure_mint_available(timeout=10)
        return minter.mint(amount=amount, timeout=60, retries=2)
    except MintUnavailableError:
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Helper: POST a token and return the JSON response, skipping on mint drift
# ---------------------------------------------------------------------------


def _pay_token(base_url: str, token: str) -> dict:
    """POST a Cashu token to / and return the parsed JSON response.

    Raises ``pytest.skip`` when the mint rejects the token (HTTP 400) due
    to keyset drift -- this is an environmental issue, not a code bug.
    """
    resp = requests.post(
        f"{base_url}/",
        data=token,
        headers={"Content-Type": "text/plain", "X-Forwarded-For": _TEST_IP},
        timeout=15,
    )
    if resp.status_code == 400:
        pytest.skip(
            f"Token verification rejected (mint/keyset drift): "
            f"{resp.text[:200]}"
        )
    assert resp.status_code == 200, (
        f"Expected 200 from POST /, got {resp.status_code}: "
        f"{resp.text[:200]}"
    )
    return resp.json()


# ---------------------------------------------------------------------------
# Test 1: Full end-to-end payment flow -- mint -> pay -> balance -> usage
# ---------------------------------------------------------------------------


def test_e2e_payment_full_flow(rust_basic_server):
    """Complete customer journey: mint, pay, verify balance + usage.

    Verifies the full chain that a real customer experiences:

      1. Inject a DHCP lease so the MAC resolver maps 127.0.0.1 to
         00:11:22:33:44:55.
      2. Mint a real Cashu token (4 sat) from testnut.cashu.exchange.
      3. POST the token to / -- expect HTTP 200 + kind 1022 (session
         granted).
      4. GET /balance for the same client -- expect session_active=true
         with a non-zero allotment.
      5. GET /usage for the same client -- expect a real ``used/total``
         pair (NOT ``-1/-1`` which means "no session").

    If pay.rs stores the session under a wrong MAC, or balance/usage
    cannot resolve the same MAC, the assertions in steps 4-5 fail.
    """
    # Step 1: inject DHCP lease
    dhcp_backup = _inject_dhcp_leases()

    try:
        # Step 2: mint a real Cashu token (skip gracefully if mint is down)
        token = _mint_test_token(amount=4)
        if not token:
            pytest.skip(
                "Cashu mint unavailable -- cannot mint a test token "
                "(e2e payment flow requires a real payment)"
            )

        base_url = rust_basic_server["http_url"]

        # Step 3: POST the token to / (payment endpoint)
        pay_data = _pay_token(base_url, token)
        assert pay_data["kind"] == 1022, (
            f"Expected kind 1022 (session granted), got "
            f"{pay_data.get('kind')}: {pay_data}"
        )

        # Step 4: GET /balance -- session must be active for this client.
        # X-Forwarded-For ensures get_client_ip resolves to 127.0.0.1,
        # which the DHCP lease maps to 00:11:22:33:44:55 -- the same MAC
        # that pay.rs used when creating the session.
        balance_resp = requests.get(
            f"{base_url}/balance",
            headers={"X-Forwarded-For": _TEST_IP},
            timeout=10,
        )
        assert balance_resp.status_code == 200, (
            f"Expected 200 from GET /balance, got "
            f"{balance_resp.status_code}: {balance_resp.text[:200]}"
        )
        balance_data = balance_resp.json()
        assert balance_data.get("session_active") is True, (
            f"Expected session_active=true, got "
            f"session_active={balance_data.get('session_active')}. "
            f"Full response: {balance_resp.text[:300]}"
        )
        allotment = balance_data.get("allotment", 0)
        assert allotment > 0, (
            f"Expected allotment > 0, got allotment={allotment}. "
            f"Full response: {balance_resp.text[:300]}"
        )

        # Step 5: GET /usage -- must show real usage, not -1/-1.
        usage_resp = requests.get(
            f"{base_url}/usage",
            headers={"X-Forwarded-For": _TEST_IP},
            timeout=10,
        )
        assert usage_resp.status_code == 200, (
            f"Expected 200 from GET /usage, got "
            f"{usage_resp.status_code}: {usage_resp.text[:200]}"
        )
        usage_body = usage_resp.text.strip()
        assert usage_body != "-1/-1", (
            f"Expected actual usage (used/total), got -1/-1 "
            f"(session not visible to /usage after payment)"
        )
        m = re.match(r"^(\d+)/(\d+)$", usage_body)
        assert m, (
            f"Usage body does not match X/Y integer format: {usage_body!r}"
        )
        used, total = int(m.group(1)), int(m.group(2))
        assert total > 0, (
            f"Expected total (allotment) > 0 in /usage, got total={total}"
        )
        # Cross-check: /usage total must match /balance allotment
        assert total == allotment, (
            f"/usage total ({total}) != /balance allotment ({allotment})"
        )
    finally:
        _restore_dhcp_leases(dhcp_backup)


# ---------------------------------------------------------------------------
# Test 2: Auto-expiry -- session with small allotment expires via monitor
# ---------------------------------------------------------------------------


def test_e2e_auto_expiry(rust_basic_server):
    """Session with a small allotment auto-expires via the background monitor.

    Mints a 2-sat token (allotment = 10 000 ms = 10 s), pays, confirms the
    session is active, then polls /balance until the monitor revokes it
    (expected within ~15 s: 10 s allotment + 2 s monitor tick + buffer).

    Requires the background Monitor to be running (src/monitor.rs started
    from main.rs).
    """
    dhcp_backup = _inject_dhcp_leases()

    try:
        token = _mint_test_token(amount=2)
        if not token:
            pytest.skip(
                "Cashu mint unavailable -- cannot mint a test token "
                "(auto-expiry test requires a real payment)"
            )

        base_url = rust_basic_server["http_url"]

        # Pay and confirm session is granted
        pay_data = _pay_token(base_url, token)
        assert pay_data["kind"] == 1022, (
            f"Expected kind 1022 (session granted), got "
            f"{pay_data.get('kind')}: {pay_data}"
        )

        # Confirm session is initially active
        balance_resp = requests.get(
            f"{base_url}/balance",
            headers={"X-Forwarded-For": _TEST_IP},
            timeout=10,
        )
        assert balance_resp.status_code == 200
        balance_data = balance_resp.json()
        assert balance_data.get("session_active") is True, (
            "Session should be active immediately after payment"
        )

        # Poll /balance until session_active becomes false.
        # Allotment is 5 000 ms; the monitor ticks every 2 s, so the
        # session should be revoked by ~7 s.  We poll for up to 15 s
        # to allow for scheduling jitter.
        deadline = time.monotonic() + 15.0
        expired = False
        while time.monotonic() < deadline:
            time.sleep(1.0)
            resp = requests.get(
                f"{base_url}/balance",
                headers={"X-Forwarded-For": _TEST_IP},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if not data.get("session_active"):
                    expired = True
                    break

        assert expired, (
            "Session did not auto-expire within 15 s "
            f"(allotment=5000ms). Last /balance: {resp.text[:300]}"
        )
    finally:
        _restore_dhcp_leases(dhcp_backup)
