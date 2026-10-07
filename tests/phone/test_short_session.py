import pytest
from lib.helpers import pay_and_wait, assert_internet, wait_expiry_and_verify_cutoff
from lib.constants import TOKEN_SMALL

pytestmark = [pytest.mark.story("G4"),pytest.mark.phone, pytest.mark.slow, pytest.mark.timeout(120), pytest.mark.extended, pytest.mark.requires_wifi]


def test_short_session_lifecycle(router, adb, cashu, connected_wifi, screenshot_raw):
    token = cashu.mint(TOKEN_SMALL)
    pay_and_wait(router, adb, token)
    screenshot_raw("ss-auth-ok.png")

    assert assert_internet(adb), "No internet during active session"
    wait_expiry_and_verify_cutoff(router, adb)
    screenshot_raw("ss-final.png")
