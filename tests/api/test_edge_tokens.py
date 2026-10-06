# TIP-02: Cashu Payments — Edge-Case Token Handling

import pytest
from lib.helpers import is_session_event, require_client_identity, is_degraded

pytestmark = [
    pytest.mark.story("M4"),
    pytest.mark.tip("TIP-02"),pytest.mark.api, pytest.mark.extended]


@pytest.mark.extended
def test_empty_token_rejected(router, cashu):
    empty_token = ""
    resp = router.pay_via_header(empty_token)

    # Should not crash — response should be empty or non-success
    assert not resp or '"success":true' not in resp, \
        "Empty token was accepted (unexpected success)"


@pytest.mark.extended
def test_garbage_token_rejected(router, cashu):
    garbage_token = "not-a-token"
    resp = router.pay_via_header(garbage_token)

    # Should not crash — response should not indicate success
    assert '"success":true' not in resp, \
        "Garbage token was ACCEPTED (expected rejection)"


@pytest.mark.extended
def test_malformed_cashu_prefix(router, cashu):
    malformed_token = "cashuA" + "!" * 10  # Not valid base64
    resp = router.pay_via_header(malformed_token)

    # Should not crash — response should not indicate success
    assert '"success":true' not in resp, \
        "Malformed token was ACCEPTED (expected rejection)"


@pytest.mark.extended
def test_duplicate_token_immediate_reuse(router, cashu):
    require_client_identity(router)

    # Degraded-mode guard: backend returns kind 21023 (discovery) when
    # mint is unreachable — cannot verify tokens, so double-spend test
    # is meaningless.
    if is_degraded(router):
        pytest.skip("backend in degraded mode (mint unreachable), cannot test double-spend")

    # Mint token
    token = cashu.mint(4)

    # First payment should succeed
    resp1 = router.pay_direct(token)
    if resp1.get("kind") == 21023:
        pytest.skip("backend entered degraded mode during test (mint unreachable)")
    assert is_session_event(resp1), \
        f"First payment failed: {str(resp1)[:200]}"

    # Second payment with same token should fail (double-spend protection)
    resp2 = router.pay_direct(token)
    assert not is_session_event(resp2), \
        "Duplicate token was ACCEPTED (expected double-spend rejection)"
