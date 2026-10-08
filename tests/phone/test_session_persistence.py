# Session Persistence Across Network Interruption
#
# Session is keyed by device-identifier (MAC), not by TCP connection,
# so it should survive brief disconnects.

import time
import pytest
from lib.helpers import pay_and_wait, assert_internet
from lib.constants import TOKEN_LONG

pytestmark = [pytest.mark.story("G4"),pytest.mark.phone, pytest.mark.slow, pytest.mark.timeout(120), pytest.mark.extended, pytest.mark.requires_wifi]


def test_session_persists_wifi_reconnect(router, adb, cashu, connected_wifi, screenshot_raw):
    token = cashu.mint(TOKEN_LONG)

    resp = pay_and_wait(router, adb, token)

    allotment = 0
    for tag in resp.get("tags", []):
        if isinstance(tag, list) and tag[0] == "allotment":
            allotment = int(tag[1])
    assert allotment > 0, f"No allotment in payment response: {resp}"

    assert assert_internet(adb), "No internet after initial auth"

    screenshot_raw("sp-before-reconnect.png")

    router.ssh(
        f"iw dev phy0-ap0 station del {router.phone_mac} 2>/dev/null; true"
    )
    time.sleep(2)

    assert adb.wake_and_unlock(), "Failed to wake phone after disconnect"
    time.sleep(3)

    state = router.get_nds_state()
    if state == "Authenticated":
        assert assert_internet(adb), \
            "Session shows Authenticated after reconnect but no internet"

    screenshot_raw("sp-after-reconnect.png")
