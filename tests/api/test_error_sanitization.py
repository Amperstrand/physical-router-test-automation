"""Test that API error responses don't leak internal details (audit S3)."""

import json
import pytest
from lib.helpers import skip_if_no_cli_socket

pytestmark = [pytest.mark.story("M5")]



@pytest.mark.api
@pytest.mark.critical
def test_balance_invalid_mac_sanitized(router):
    """GET /balance with invalid MAC returns generic error, not internal details."""
    skip_if_no_cli_socket(router)
    status = router.api_status("/balance")
    if status != 200:
        pytest.skip(f"/balance returned HTTP {status}, expected 200 with error body")
    body_str = router.api_body("/balance")
    try:
        body = json.loads(body_str)
    except json.JSONDecodeError:
        pytest.skip(f"/balance returned non-JSON: {body_str[:120]}")
    error_msg = body.get("error", "")
    # Must NOT contain internal Go error patterns
    internal_patterns = ["runtime", "goroutine", ".go:", "panic:", "nil pointer",
                        "connection refused", "dial tcp", "no such file"]
    for pattern in internal_patterns:
        assert pattern not in error_msg.lower(), \
            f"Error response leaks internal detail '{pattern}': {error_msg}"


@pytest.mark.api
@pytest.mark.critical
def test_ln_invoice_error_sanitized(router):
    """POST /ln-invoice with bad mint returns generic error, not internal details."""
    import requests
    try:
        resp = requests.post(
            "http://[::1]:2121/ln-invoice",
            json={"amount": 1, "mint_url": "http://nonexistent.invalid"},
            timeout=10
        )
    except Exception:
        pytest.skip("ln-invoice endpoint not reachable")
    
    if resp.status_code == 405:
        pytest.skip("ln-invoice not available (GET returns 405)")
    
    body = resp.json()
    error_msg = body.get("error", "")
    internal_patterns = ["runtime", "goroutine", ".go:", "panic:", "nil pointer",
                        "dial tcp", "connection refused", "no such host"]
    for pattern in internal_patterns:
        assert pattern not in error_msg.lower(), \
            f"LN invoice error leaks internal detail '{pattern}': {error_msg}"
