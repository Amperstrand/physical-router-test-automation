# Token Input via Portal UI
#
# Golden path: user connects WiFi, opens portal, types token, gets authenticated.
# Also verifies invalid tokens are handled gracefully (no crash, no auth).

import time
import logging

import pytest

from lib.helpers import assert_session_active, assert_internet
from lib.constants import TOKEN_DEFAULT

log = logging.getLogger("tollgate.test_token_input")

pytestmark = [pytest.mark.story("G5"),pytest.mark.phone, pytest.mark.slow, pytest.mark.timeout(120), pytest.mark.critical, pytest.mark.requires_wifi]


def test_token_input_happy_path(router, adb, cashu, wifi, connected_wifi, screenshot_portal):
    token = cashu.mint(TOKEN_DEFAULT)

    screenshot_portal("token-input-start.png")

    assert wifi._type_token_in_portal(token, timeout=60), "Token typing failed"

    assert router.wait_for_auth(timeout=30), "Not authenticated after token input"

    screenshot_portal("token-input-authed.png")

    assert_session_active(router)

    assert assert_internet(adb, "1.1.1.1"), "No internet after auth"

    screenshot_portal("token-input-done.png")


def test_token_input_invalid_token(router, wifi, connected_wifi, screenshot_portal):
    screenshot_portal("token-invalid-start.png")

    wifi._type_token_in_portal("INVALID_TOKEN_NOT_CASHU_12345", timeout=30)

    time.sleep(5)

    screenshot_portal("token-invalid-after.png")

    nds_state = router.get_nds_state()
    assert nds_state != "Authenticated", \
        f"Client should NOT be authenticated after invalid token (state={nds_state})"
