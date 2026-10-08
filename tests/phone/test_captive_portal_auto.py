import pytest
import time
import logging
import re
from lib.constants import ANDROID_CAPTIVE_PORTAL, ANDROID_CAPTIVE_PORTAL_ACTIVITY

pytestmark = [
    pytest.mark.story("G5"),
    pytest.mark.phone,
    pytest.mark.slow,
    pytest.mark.timeout(120),
    pytest.mark.extended,
    pytest.mark.pay_via("skip"),
    pytest.mark.requires_wifi,
]


def test_android_detects_captive_portal(router, adb, wifi, connected_wifi, screenshot_portal):
    log = logging.getLogger("tollgate.test_captive_portal_auto")

    log.info("Waiting for Android to detect captive portal (up to 30s)...")
    start = time.time()
    timeout = 30

    while time.time() - start < timeout:
        xml = adb.ui_xml()
        if ANDROID_CAPTIVE_PORTAL_ACTIVITY in xml:
            log.info(
                f"Captive portal detected by Android after {int(time.time() - start)}s"
            )
            break
        time.sleep(3)
    else:
        log.warning("Captive portal not detected within 30s")
        screenshot_portal("captive-notification-shade.png")
        pytest.fail(
            "Android did not detect captive portal within 30s. Check notification shade manually."
        )

    screenshot_portal("captive-before-shade.png")

    log.info("Opening notification shade...")
    adb.swipe(540, 0, 540, 500, 300)
    time.sleep(2)

    screenshot_portal("captive-notification-shade.png")

    xml = adb.ui_xml()
    log.debug(f"Notification shade UI XML length: {len(xml)}")

    sign_in_patterns = [
        r'text="Sign in to network[^"]*"',
        r'text="Sign in to [^"]*"',
        r'text="Login to [^"]*"',
        r'text="Connect to network"',
    ]

    notification_found = False
    for pattern in sign_in_patterns:
        match = re.search(pattern, xml, re.IGNORECASE)
        if match:
            log.info(f"Found notification with pattern: {pattern[:50]}...")
            notification_found = True
            break

    if not notification_found:
        log.warning("No 'Sign in' notification found in shade")
        log.info("Searching for any notification related to captive portal...")
        if re.search(
            r"com\.android\.captiveportallogin", xml, re.IGNORECASE
        ) or re.search(r"captive", xml, re.IGNORECASE):
            log.info("Captive portal notification detected but text doesn't match expected patterns")
        else:
            log.info("No captive portal notification found")
        screenshot_portal("captive-no-notification.png")
        pytest.fail(
            "Captive portal notification not found in notification shade. Check manually."
        )

    log.info("Tapping captive portal notification...")
    xml = adb.ui_xml()

    for pattern in sign_in_patterns:
        match = re.search(rf'{pattern}[^>]*bounds="\[([^]]*)\]\[([^]]*)\]"', xml)
        if match:
            bounds_str = f"[{match.group(1)}][{match.group(2)}]"
            adb.tap_bounds(bounds_str)
            time.sleep(2)
            log.info("Tapped notification")
            break
    else:
        match = re.search(r'text="Sign in"[^>]*bounds="\[([^]]*)\]\[([^]]*)\]"', xml)
        if match:
            adb.tap_bounds(f"[{match.group(1)}][{match.group(2)}]")
            time.sleep(2)
            log.info("Tapped 'Sign in' button")
        else:
            log.warning("Could not find tapable notification element")
            screenshot_portal("captive-tap-failed.png")
            pytest.fail("Could not find tapable notification element")

    log.info("Waiting for portal to render...")
    start = time.time()
    timeout = 15

    while time.time() - start < timeout:
        xml = adb.ui_xml()
        if re.search(r'data-sm="portal_ready"', xml, re.IGNORECASE):
            log.info(f"Portal opened after {int(time.time() - start)}s")
            break
        time.sleep(3)
    else:
        log.warning("Portal did not open within 15s")
        screenshot_portal("captive-portal-not-opened.png")
        pytest.fail(
            "Captive portal did not open after tapping notification within 15s. Check manually."
        )

    screenshot_portal("captive-portal-opened.png")

    log.info("Test passed: Android auto-detected captive portal and portal opened successfully")
