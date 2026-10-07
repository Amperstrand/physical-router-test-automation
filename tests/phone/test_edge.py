import time
import pytest
from lib.helpers import (pay_and_wait, assert_internet, wait_expiry_and_verify_cutoff,
                          is_session_event, assert_deauthenticated)
from lib.constants import TOKEN_SMALL, ANDROID_CAPTIVE_PORTAL

pytestmark = [pytest.mark.story("G2"),pytest.mark.phone, pytest.mark.slow, pytest.mark.timeout(120), pytest.mark.extended, pytest.mark.requires_wifi]


def test_spent_token_reuse(router, adb, cashu, connected_wifi, screenshot_raw):
    token = cashu.mint(TOKEN_SMALL)
    pay_and_wait(router, adb, token)

    router.wait_for_session_expiry()
    time.sleep(2)

    adb.wake_and_unlock()
    time.sleep(1)
    resp = router.pay_direct(token)
    time.sleep(5)

    assert resp.get("kind") != 1022 or "error" in str(resp).lower(), \
        f"Spent token not rejected by backend: {str(resp)[:200]}"
    assert_deauthenticated(router)

    screenshot_raw("edge-spent.png")


def test_invalid_token(router, adb, wifi, screenshot_raw):
    router.reset_state(adb=adb)
    router.clear_portal_log()

    assert wifi.reconnect(), "WiFi reconnect failed for invalid token test"

    try:
        adb.wake_and_unlock()
        time.sleep(1)

        router.pay_direct("cashuAinvalid_token_not_real_data_12345")

        time.sleep(5)
        assert_deauthenticated(router)

        screenshot_raw("edge-invalid.png")
    finally:
        router.reset_state(adb=adb)


def test_reauth_after_expiry(router, adb, cashu, connected_wifi, screenshot_raw):
    token1 = cashu.mint(TOKEN_SMALL)
    pay_and_wait(router, adb, token1)

    time.sleep(2)
    assert assert_internet(adb), "No internet after initial auth"

    wait_expiry_and_verify_cutoff(router, adb)

    token2 = cashu.mint(TOKEN_SMALL)
    resp = router.pay_direct(token2)
    assert is_session_event(resp), \
        f"Re-auth payment failed: {str(resp)[:200]}"

    assert router.wait_for_auth(timeout=30), "Not re-authenticated after second payment"
    assert assert_internet(adb), "No internet after re-auth"

    screenshot_raw("edge-post-reauth.png")
