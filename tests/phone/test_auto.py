# TIP-01 + TIP-02 + TIP-03: Direct Backend Payment Flow
#
# Flow: TIP-02 Cashu token → TIP-03 POST / → TIP-01 Session (kind=1022)

import pytest
from lib.helpers import assert_internet, is_session_event, assert_session_active
from lib.constants import TOKEN_DEFAULT

pytestmark = [pytest.mark.story("G2"), pytest.mark.tip("TIP-02"),pytest.mark.phone, pytest.mark.slow, pytest.mark.timeout(300), pytest.mark.critical, pytest.mark.requires_wifi]


def test_auto_direct_backend_pay(router, adb, cashu, connected_wifi, screenshot_portal):
    token = cashu.mint(TOKEN_DEFAULT)

    screenshot_portal("auto-start.png")

    resp = router.pay_direct(token)
    assert is_session_event(resp), \
        f"Direct payment failed: {str(resp)[:200]}"

    assert router.wait_for_auth(timeout=30), "Not authenticated after direct payment"

    screenshot_portal("auto-authed.png")

    assert_session_active(router)

    assert assert_internet(adb, "1.1.1.1"), "No internet after auth"

    screenshot_portal("auto-done.png")
