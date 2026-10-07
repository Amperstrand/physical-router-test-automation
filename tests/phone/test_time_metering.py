import time
import pytest
from lib.helpers import (pay_and_wait, assert_internet, wait_expiry_and_verify_cutoff,
                          metering_test_setup)
from lib.constants import TOKEN_SMALL, PRODUCTION_STEP_SIZE_MS

pytestmark = [pytest.mark.story("G6"),pytest.mark.phone, pytest.mark.slow, pytest.mark.timeout(120), pytest.mark.config, pytest.mark.extended]


def test_time_metering(router, adb, cashu, wifi, test_pricing, screenshot_portal, screenshot_raw):
    step_ms = PRODUCTION_STEP_SIZE_MS
    amount = TOKEN_SMALL
    expected_sec = ((amount - 1) * step_ms) // 1000

    try:
        session, _ = metering_test_setup(router, adb, wifi, cashu, test_pricing,
                                         amount, step_ms, "milliseconds")
        screenshot_portal("tm-authed.png")

        elapsed = wait_expiry_and_verify_cutoff(router, adb)

        drift = abs(elapsed - expected_sec)
        assert drift <= 8, \
            f"Session duration {elapsed}s too far from expected {expected_sec}s (drift={drift}s)"
    finally:
        router.reset_state(adb=adb)
