# Backend Delivery via pay_direct
#
# TIP refs:
#   - TIP-03: POST / — payment submission endpoint
#   - TIP-02: Cashu token as bearer asset
#   - TIP-01: Session event (kind=1022) — allotment tag on success

import pytest
from lib.helpers import pay_and_wait, assert_session_active
from lib.constants import TOKEN_DEFAULT

pytestmark = [pytest.mark.story("G5"),pytest.mark.phone, pytest.mark.slow, pytest.mark.timeout(120), pytest.mark.critical, pytest.mark.requires_wifi]


def test_backend_delivery(router, adb, cashu, connected_wifi, screenshot_raw):
    token = cashu.mint(TOKEN_DEFAULT)
    pay_and_wait(router, adb, token)
    assert_session_active(router)
    screenshot_raw("paste-final.png")
