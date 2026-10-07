import pytest
from lib.helpers import pay_and_wait, assert_session_active, is_session_event
from lib.constants import TOKEN_DEFAULT

pytestmark = [pytest.mark.story("R2"), pytest.mark.tip("TIP-02"),pytest.mark.phone, pytest.mark.slow, pytest.mark.timeout(120), pytest.mark.critical, pytest.mark.requires_wifi]


@pytest.mark.parametrize("legacy,prefix", [
    (True, "cashuA"),
    (False, "cashuB"),
], ids=["v3", "v4"])
def test_token_payment(router, adb, cashu, connected_wifi, screenshot_raw, legacy, prefix):
    token = cashu.mint(TOKEN_DEFAULT, legacy=legacy)
    assert token.startswith(prefix), f"Expected {prefix} prefix, got {token[:20]}"

    resp = router.pay_direct(token)
    assert is_session_event(resp), f"{prefix} token rejected: {str(resp)[:200]}"
    assert router.wait_for_auth(timeout=30), f"Not authenticated after {prefix} payment"
    assert_session_active(router)

    screenshot_raw(f"{prefix}-token-pass.png")
