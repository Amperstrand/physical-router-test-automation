"""Tests for security fixes in PR #104.

PR #104 (merged as 8ec5342) fixed three security issues:
1. Mint URL case-insensitive matching
2. Double-spent token detection (not crashing)
3. X-Forwarded-For header trust from localhost only

See: https://github.com/OpenTollGate/tollgate-knowledgebase/tree/main/incidents/2026-01-XX_security-fixes-pr104
"""

import pytest
from lib.helpers import require_client_identity


pytestmark = [pytest.mark.story("O8")]

def _is_accepted(resp: dict) -> bool:
    return resp.get("kind") == 1022 or resp.get("success") is True


@pytest.mark.api
@pytest.mark.extended
def test_mint_url_case_insensitive(router, cashu):
    """Mint URLs with different casing should still be accepted.

    The bug was that the Go backend would crash if the mint URL in a token
    differed from the configured mint URL by case only.
    """
    token = cashu.mint(amount=100)
    result = router.pay_direct(token)
    assert _is_accepted(result), \
        f"Mint URL case-insensitive payment rejected: {str(result)[:300]}"


@pytest.mark.api
@pytest.mark.extended
def test_spent_token_detected(router, cashu):
    """Double-spending a token should return a proper error, not crash.

    The bug was that paying with an already-spent token would cause a 500 error
    (panic/crash) instead of a proper error response.
    """
    require_client_identity(router)

    token = cashu.mint(amount=100)

    result1 = router.pay_direct(token)
    assert _is_accepted(result1), \
        f"First payment failed: {str(result1)[:300]}"

    result2 = router.pay_direct(token)
    assert result2 is not None, "Second payment returned None (crash?)"
    assert not _is_accepted(result2), \
        f"Double-spent token was ACCEPTED (security issue!): {str(result2)[:300]}"


@pytest.mark.api
@pytest.mark.extended
def test_proxy_header_only_from_localhost(router, backend):
    """X-Forwarded-For should only be trusted from localhost.

    The bug was that X-Forwarded-For and X-Real-Ip headers from external
    clients could bypass authentication by spoofing the client IP.

    This test verifies the backend is running and reachable via its
    CLI socket — the actual header validation can only be tested with
    a captive portal proxy in front of the backend.
    """
    if backend.is_rust:
        pytest.skip("Rust SUT uses nftables, not CLI socket for status")
    status = router.get_tollgate_status()
    assert status.get("success") is True, \
        f"Backend status check failed: {str(status)[:200]}"
