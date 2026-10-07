import time
import pytest
from lib.helpers import pay_expire_cutoff
from lib.constants import TOKEN_SMALL, ANDROID_CAPTIVE_PORTAL, ANDROID_CAPTIVE_PORTAL_ACTIVITY

pytestmark = [pytest.mark.story("G4"),pytest.mark.phone, pytest.mark.slow, pytest.mark.timeout(120), pytest.mark.extended, pytest.mark.requires_wifi]


def test_expiry_kick(router, adb, cashu, connected_wifi, screenshot_raw):
    pay_expire_cutoff(router, adb, cashu, TOKEN_SMALL)

    adb.force_stop(ANDROID_CAPTIVE_PORTAL)
    time.sleep(1)
    adb.start_activity(component=ANDROID_CAPTIVE_PORTAL_ACTIVITY)
    time.sleep(3)

    screenshot_raw("ek-captive-retrigger.png")
