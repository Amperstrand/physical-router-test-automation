# Camera Access in Android Captive Portal WebView
#
# Android's captive portal WebView (CaptivePortalLogin) runs in a restricted
# environment where camera access is blocked by design. Relevant to TIP-04
# (Restrictive OS Compatibility).

import time
import pytest
from lib.constants import ANDROID_CAPTIVE_PORTAL

pytestmark = [pytest.mark.story("G5"),pytest.mark.phone, pytest.mark.android_only, pytest.mark.timeout(120), pytest.mark.extended]


def test_camera_captive_diagnostic(router, adb, wifi, screenshot_raw):
    router.reset_state(adb=adb)
    router.clear_portal_log()

    adb.force_stop(ANDROID_CAPTIVE_PORTAL)

    perms = adb.shell(f"dumpsys package {ANDROID_CAPTIVE_PORTAL} --user 0")
    assert "android.permission.CAMERA" not in perms, "Package unexpectedly has CAMERA permission"

    try:
        wifi.reconnect_no_fallback()

        time.sleep(15)

        beacons = router.ssh("wc -l < /tmp/tollgate-portal.log 2>/dev/null").strip()
        beacon_count = int(beacons) if beacons.isdigit() else 0

        if beacon_count > 1:
            portal_log = router.get_portal_log()
            assert "camera_probe_granted" not in portal_log, \
                "Camera was granted in captive portal WebView — expected blocked (NotAllowedError)"
        else:
            pytest.skip("Captive portal WebView did not execute JS (0-1 beacons) — no camera probe to validate")

        screenshot_raw("cp-camera-diag.png")
    finally:
        router.reset_state(adb=adb)
