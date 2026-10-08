import re

import pytest

pytestmark = [pytest.mark.story("P3"),pytest.mark.api, pytest.mark.smoke]


@pytest.mark.smoke
def test_root_endpoint(router, backend):
    code = router.api_status("/")
    assert code == 200, f"Expected 200, got {code}"
    body = router.api_body("/")
    if backend.is_rust:
        if '"kind":10021' in body:
            return
        if '"kind":21023' in body:
            pytest.skip("Discovery in degraded mode")
        pytest.fail(f"Rust SUT should return JSON advertisement at /: {body[:200]}")
    if '"kind":21023' in body:
        pytest.skip("Discovery in degraded mode, skipping kind:10021 check")
    assert '"kind":10021' in body, f"Response missing kind:10021: {body[:200]}"


@pytest.mark.smoke
def test_pay_endpoint(router, backend):
    pay_path = "/" if backend.is_rust_family else "/pay"
    code = router.api_status(pay_path)
    body = router.api_body(pay_path)
    assert code in (200, 402), f"Expected 200 or 402, got {code}"
    if backend.is_rust:
        if code == 402:
            if not body.strip():
                return
            assert '"price"' in body or '"error"' in body, \
                f"Rust 402 should have price or error: {body[:200]}"
        else:
            assert '"kind"' in body, f"Rust 200 should have kind field: {body[:200]}"
        return
    if code == 402:
        assert '"payment_request"' in body, "402 but missing payment_request"
        assert '"qr_image"' in body, "402 but missing qr_image"
    else:
        assert '"kind"' in body, f"200 but missing kind field: {body[:200]}"


@pytest.mark.smoke
def test_whoami_endpoint(router):
    code = router.api_status("/whoami")
    body = router.api_body("/whoami")
    assert code in (200, 400, 500), f"Expected 200, 400, or 500, got {code}"
    if code == 200:
        assert body, "200 but empty body"
        first_line = body.strip().split("\n")[0]
        match = re.match(r'^(\w+)=(.*)$', first_line)
        assert match, f"Response not in type=value format: {body[:100]}"
        id_type, id_value = match.group(1), match.group(2).strip()
        assert id_type == "mac", f"Expected type 'mac', got '{id_type}'"
        if id_value:
            assert re.match(r'^([0-9a-f]{2}:){5}[0-9a-f]{2}$', id_value, re.IGNORECASE), \
                f"Invalid MAC format: {id_value}"
    else:
        assert "error" in body.lower() or "unknown" in body.lower() or not body, \
            f"{code} without error message: {body[:200]}"


@pytest.mark.smoke
def test_balance_endpoint(router):
    """Hit /balance and verify it responds.

    400 is valid when getMacAddress() can't resolve the source IP (no ARP entry
    for the test runner).  200 is valid when there's an active session or when
    usage is '-1/-1' (no session).  Both codes prove the endpoint exists and
    the handler is wired up correctly.
    """
    code = router.api_status("/balance")
    body = router.api_body("/balance")
    assert code in (200, 400), f"Expected 200 or 400, got {code}"
    if code == 400:
        assert "mac" in body.lower() or "error" in body.lower(), \
            f"400 without MAC/error message: {body[:200]}"
    else:
        assert '"remaining"' in body or '"allotment"' in body \
            or '"session_active"' in body or '"kind":10021' in body, \
            f"Balance response missing expected fields: {body[:200]}"
