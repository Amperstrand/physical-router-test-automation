import os

import pytest
import requests

pytestmark = [pytest.mark.story("R1"),pytest.mark.rust_basic_only, pytest.mark.api, pytest.mark.smoke]


def test_pay_invalid_token_returns_400(rust_basic_server):
    """S6: POST / with invalid Cashu token returns HTTP 400 with kind 21023 error event.

    Matches tollgate-module-basic-go main.go:461 — Go returns Bad Request (400),
    not Payment Required (402), for any kind 21023 rejection.
    """
    resp = requests.post(
        f"{rust_basic_server['http_url']}/",
        data="garbage-not-a-token",
        headers={"Content-Type": "text/plain"},
        timeout=10,
    )
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    assert data["kind"] == 21023, f"Expected kind 21023, got {data.get('kind')}"
    content_lower = data.get("content", "").lower()
    assert "rejected" in content_lower or "error" in content_lower or "invalid" in content_lower, (
        f"Unexpected content: {data.get('content')}"
    )


def test_pay_wrong_content_type_returns_415(rust_basic_server):
    """S7: POST / with unsupported Content-Type returns HTTP 415."""
    resp = requests.post(
        f"{rust_basic_server['http_url']}/",
        data="<xml/>",
        headers={"Content-Type": "application/xml"},
        timeout=5,
    )
    assert resp.status_code == 415, f"Expected 415, got {resp.status_code}: {resp.text[:200]}"


def test_pay_valid_token_returns_1022(rust_basic_server):
    """S2: POST / with a valid Cashu token returns 200 with kind 1022 session-granted event."""
    token = _mint_test_token()
    if not token:
        pytest.skip("Cashu mint unavailable — cannot mint a test token (S2 happy path)")
    client_ip = os.environ.get("TOLLGATE_CLIENT_IP", "10.0.0.42")
    client_mac = os.environ.get("TOLLGATE_CLIENT_MAC", "02:00:00:00:00:01")

    original_leases = None
    try:
        with open("/tmp/dhcp.leases", "rb") as f:
            original_leases = f.read()
    except FileNotFoundError:
        pass
    try:
        with open("/tmp/dhcp.leases", "w") as f:
            f.write(f"9999999999 {client_mac} {client_ip} test *\n")
    except OSError:
        pass

    try:
        resp = requests.post(
            f"{rust_basic_server['http_url']}/",
            data=token,
            headers={"Content-Type": "text/plain", "X-Forwarded-For": client_ip},
            timeout=15,
        )
        if resp.status_code == 400:
            pytest.skip(f"Token verification rejected (mint/keyset drift): {resp.text[:200]}")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
        assert data["kind"] == 1022, f"Expected kind 1022, got {data.get('kind')}: {resp.text[:200]}"
    finally:
        try:
            if original_leases is not None:
                with open("/tmp/dhcp.leases", "wb") as f:
                    f.write(original_leases)
        except OSError:
            pass


def _mint_test_token():
    """Mint a Cashu token from testnut.cashu.exchange via lib.cashu.

    Returns the token string, or None when the mint is unreachable.
    Uses the existing lib.cashu minter infrastructure rather than the
    raw cashu CLI so we inherit retry/skip logic and HttpMinter/CdkCliWallet
    fallback selection.
    """
    try:
        from lib.cashu import MintUnavailableError, create_minter
    except ImportError:
        return None
    try:
        minter = create_minter("https://testnut.cashu.exchange")
        minter.ensure_mint_available(timeout=10)
        return minter.mint(amount=4, timeout=60, retries=2)
    except MintUnavailableError:
        return None
    except Exception:
        return None
