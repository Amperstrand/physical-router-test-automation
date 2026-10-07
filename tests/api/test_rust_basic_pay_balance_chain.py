"""Regression test: pay→balance chain verifies session lookup by real MAC.

Closes a TDD coverage gap identified by Oracle: the pay.rs handler was
previously storing sessions under a hardcoded "00:00:00:00:00:00" MAC
instead of the real client MAC resolved from /tmp/dhcp.leases.  The
balance handler looks up sessions by the real MAC, so the mismatch meant
sessions were *never* found by /balance after payment.

The bug was fixed (pay.rs now resolves the real MAC via get_client_ip →
get_mac_address, same chain as balance.rs).  This test exercises the full
end-to-end chain:

    1. Inject a fake /tmp/dhcp.leases entry for 127.0.0.1.
    2. Mint a real Cashu token from testnut.cashu.exchange.
    3. POST the token to / — expect 200 + kind 1022 (session granted).
    4. GET /balance for the same client — expect session_active=true,
       allotment > 0.

If the MAC resolution diverges between pay and balance, step 4 will return
session_active=false and the assertion will fail.
"""

import os
import time

import pytest
import requests

pytestmark = [pytest.mark.story("R1"),pytest.mark.rust_basic_only, pytest.mark.api, pytest.mark.extended]


# ---------------------------------------------------------------------------
# DHCP lease injection helpers (adapted from test_go_rust_basic_parity.py)
# ---------------------------------------------------------------------------


def _inject_dhcp_leases() -> bytes | None:
    """Back up /tmp/dhcp.leases and inject a fake 127.0.0.1 → MAC entry.

    Returns the original file content (or None if no file existed) so it
    can be restored by _restore_dhcp_leases.
    """
    original: bytes | None
    try:
        with open("/tmp/dhcp.leases", "rb") as f:
            original = f.read()
    except FileNotFoundError:
        original = None

    # Inject a fake lease: 127.0.0.1 → 00:11:22:33:44:55
    fake_entry = (
        f"{int(time.time())} 00:11:22:33:44:55 127.0.0.1 "
        f"pay-balance-chain *\n"
    )
    try:
        with open("/tmp/dhcp.leases", "w") as f:
            f.write(fake_entry)
    except OSError:
        pass  # non-fatal — resolver may still work via /proc/net/arp

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


def _mint_test_token():
    """Mint a Cashu token from testnut.cashu.exchange via lib.cashu.

    Returns the token string, or None when the mint is unreachable.
    """
    try:
        from lib.cashu import MintUnavailableError, create_minter
    except ImportError:
        return None
    try:
        mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL", "https://testnut.cashu.exchange")
        minter = create_minter(mint_url)
        minter.ensure_mint_available(timeout=10)
        return minter.mint(amount=4, timeout=60, retries=2)
    except MintUnavailableError:
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Test: pay → balance end-to-end chain
# ---------------------------------------------------------------------------


def test_pay_then_balance_shows_active_session(rust_basic_server):
    """Regression: successful payment must be visible to /balance.

    Verifies the full chain:
      - POST / with a valid Cashu token creates a session keyed by the
        real client MAC (resolved from /tmp/dhcp.leases).
      - GET /balance for the same client returns session_active=true with
        a non-zero allotment.

    If pay.rs stores the session under a wrong MAC (the original bug),
    /balance will not find it and session_active will be false.
    """
    # Step 1: inject DHCP lease so MAC resolver maps 127.0.0.1 → 00:11:22:33:44:55
    dhcp_backup = _inject_dhcp_leases()

    try:
        # Step 2: mint a real Cashu token (skip gracefully if mint is down)
        token = _mint_test_token()
        if not token:
            pytest.skip(
                "Cashu mint unavailable — cannot mint a test token "
                "(pay→balance chain requires a real payment)"
            )

        base_url = rust_basic_server["http_url"]

        # Step 3: POST the token to / (payment endpoint)
        pay_resp = requests.post(
            f"{base_url}/",
            data=token,
            headers={"Content-Type": "text/plain"},
            timeout=15,
        )
        if pay_resp.status_code == 400:
            pytest.skip(
                f"Token verification rejected (mint/keyset drift): "
                f"{pay_resp.text[:200]}"
            )
        assert pay_resp.status_code == 200, (
            f"Expected 200 from POST /, got {pay_resp.status_code}: "
            f"{pay_resp.text[:200]}"
        )
        pay_data = pay_resp.json()
        assert pay_data["kind"] == 1022, (
            f"Expected kind 1022 (session granted), got "
            f"{pay_data.get('kind')}: {pay_resp.text[:200]}"
        )

        # Step 4: GET /balance for the same client.
        # X-Forwarded-For ensures get_client_ip resolves to 127.0.0.1,
        # which the DHCP lease maps to 00:11:22:33:44:55 — the same MAC
        # that pay.rs used when creating the session.
        balance_resp = requests.get(
            f"{base_url}/balance",
            headers={"X-Forwarded-For": "127.0.0.1"},
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
    finally:
        _restore_dhcp_leases(dhcp_backup)
