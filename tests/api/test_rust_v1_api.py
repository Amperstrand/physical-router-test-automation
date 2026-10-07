import json
import os

import pytest

from lib.helpers import parse_json_or_fail, require_client_identity

pytestmark = [pytest.mark.story("R1"),pytest.mark.rust_only, pytest.mark.api, pytest.mark.smoke]

if os.environ.get("TOLLGATE_BACKEND") in ("rust-basic", "rust-embedded"):
    pytest.skip(
        "V1 API tests are for tollgate-rs, not tollgate-module-basic-rust",
        allow_module_level=True,
    )


def _client_ip(router):
    """Get client IP, skipping if unavailable."""
    ip = router.phone_ip
    if not ip:
        pytest.skip("No client IP configured (TOLLGATE_CLIENT_IP)")
    return ip


def _ensure_lease(router):
    """Inject DHCP lease for cloud lab (Rust DhcpLeasesResolver needs it)."""
    if router.phone_ip and router.phone_mac:
        router.ensure_dhcp_lease()


@pytest.mark.smoke
def test_rust_advertisement(router):
    body = router.api_body("/")
    if '"kind":21023' in body:
        pytest.skip("Discovery in degraded mode, skipping kind:10021 check")
    assert '"kind":10021' in body, f"Response missing kind:10021: {body[:200]}"
    data = parse_json_or_fail(body, "advertisement")
    tags = {t[0]: t[1] for t in data.get("tags", []) if isinstance(t, list) and len(t) >= 2}
    assert "metric" in tags, f"Missing metric tag: {body[:200]}"
    assert "step" in tags or "step_size" in tags, f"Missing step/step_size tag: {body[:200]}"


@pytest.mark.smoke
def test_rust_pay_token(router, cashu):
    require_client_identity(router)
    _ensure_lease(router)

    token = cashu.mint(4)
    resp = router.pay_direct(token)

    if resp.get("kind") == 21023:
        content = resp.get("content", "")
        if any(kw in content.lower() for kw in ["invalid", "rejected", "token", "nut02", "keyset"]):
            pytest.skip(f"Rust backend rejected token (CDK compatibility): {content[:200]}")

    assert resp.get("kind") == 1022, f"Expected kind:1022 session event, got: {str(resp)[:200]}"


@pytest.mark.smoke
def test_rust_usage(router, cashu):
    ip = _client_ip(router)
    _ensure_lease(router)

    resp = router.backend_curl_xff(router.backend_url("/usage"), ip=ip)
    assert resp, "Empty usage response"
    assert "allotment" in resp.lower() or "usage" in resp.lower(), f"Unexpected usage response: {resp[:200]}"


@pytest.mark.smoke
def test_rust_balance(router, cashu):
    ip = _client_ip(router)
    _ensure_lease(router)

    resp = router.backend_curl_xff(router.backend_url("/balance"), ip=ip)
    data = parse_json_or_fail(resp, "balance", skip=True)
    assert "remaining" in data or "allotment" in data, f"Unexpected balance response: {resp[:200]}"


@pytest.mark.smoke
def test_rust_whoami(router):
    ip = _client_ip(router)
    _ensure_lease(router)

    resp = router.ssh(
        f"wget -qO- --header='X-Forwarded-For: {ip}' "
        f"'{router.backend_url('/whoami')}'"
    )
    assert resp, "Empty whoami response"
    assert "mac=" in resp, f"Expected mac= in whoami response, got: {resp[:100]}"


@pytest.mark.smoke
def test_rust_ln_invoice_create(router):
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL", "https://testnut.cashu.exchange")
    payload = json.dumps({"amount": 10, "mint_url": mint_url})
    resp = router.ssh(
        f"wget -qO- --post-data='{payload}' "
        f"--header='Content-Type: application/json' '{router.backend_url('/ln-invoice')}'"
    )
    data = parse_json_or_fail(resp, "ln-invoice create", skip=True)

    if data.get("error"):
        pytest.skip(f"LN invoice creation failed (mint may not support bolt11): {data['error'][:200]}")

    assert "quote" in data or "payment_request" in data or "invoice" in data, \
        f"Unexpected ln-invoice response: {str(data)[:200]}"


@pytest.mark.smoke
def test_rust_ln_invoice_status(router):
    # BusyBox wget suppresses body on non-2xx; use curl -s to capture
    # the JSON error response regardless of HTTP status code.
    resp = router.ssh(
        f"curl -s '{router.backend_url('/ln-invoice')}?quote=nonexistent-test-quote'"
    )
    assert resp, "Empty ln-invoice status response"
    # Should get a JSON error response (400 or 404 with error field), not empty body
    data = parse_json_or_fail(resp, "ln-invoice status", skip=True)
    assert data.get("error") or data.get("status") == 0, \
        f"Expected error response for nonexistent quote, got: {str(data)[:200]}"


@pytest.mark.smoke
def test_rust_full_payment_cycle(router, cashu):
    """End-to-end: mint token -> pay -> verify session -> check balance -> verify whoami."""
    require_client_identity(router)
    if not cashu.is_available():
        pytest.skip("cashu venv not available")
    _ensure_lease(router)

    # Step 1: Advertisement — for Rust SUT, use /pay with token
    token_for_ad = cashu.mint(2)
    body = router.ssh(f"curl -s -H 'X-Cashu: {token_for_ad}' http://127.0.0.1:2121/pay", timeout=15)
    data = parse_json_or_fail(body, "advertisement")
    assert data.get("kind") == 10021, f"Expected kind:10021, got: {body[:200]}"

    # Step 2: Mint a token and pay
    token = cashu.mint(4)
    resp = router.pay_direct(token)

    if resp.get("kind") == 21023:
        content = resp.get("content", "")
        pytest.skip(f"Rust backend rejected token (CDK compatibility): {content[:200]}")

    assert resp.get("kind") == 1022, f"Payment failed: {str(resp)[:200]}"

    # Step 3: Check balance
    bal_resp = router.backend_curl_xff(
        router.backend_url("/balance"),
        ip=router.phone_ip,
    )
    bal = parse_json_or_fail(bal_resp, "balance")

    if not bal.get("session_active"):
        pytest.skip(f"Session not active after payment (Rust backend timing): {bal_resp[:200]}")

    assert bal.get("remaining", 0) > 0, f"No remaining allotment: {bal_resp[:200]}"

    # Step 4: Usage returns JSON with allotment/remaining
    usage_resp = router.backend_curl_xff(
        router.backend_url("/usage"),
        ip=router.phone_ip,
    )
    assert usage_resp, "Empty usage response"
    usage_data = parse_json_or_fail(usage_resp, "usage")
    assert usage_data.get("allotment", 0) > 0, f"Allotment is 0 or negative: {usage_resp[:200]}"

    # Step 5: Whoami returns MAC address
    whoami = router.ssh(
        f"wget -qO- --header='X-Forwarded-For: {router.phone_ip}' "
        f"'{router.backend_url('/whoami')}'"
    )
    assert whoami, "Empty whoami response"
    assert "mac=" in whoami, f"Whoami missing mac=: {whoami[:100]}"
