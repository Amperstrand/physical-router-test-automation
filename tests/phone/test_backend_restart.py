# Backend Restart Resilience
#
# nodogsplash maintains firewall rules independently of the Go backend.
# After a backend restart, NDS firewall rules remain in place and sessions
# persist. The backend should come back healthy within ~15-20s on MIPS.

import time
import pytest
from lib.helpers import pay_and_wait, assert_internet
from lib.constants import TOKEN_SMALL

pytestmark = [pytest.mark.story("O3"),pytest.mark.phone, pytest.mark.slow, pytest.mark.timeout(120), pytest.mark.extended, pytest.mark.requires_wifi]

BACKEND_RESTART_TIMEOUT = 45  # MIPS router needs ~20-25s for full backend restart


def test_backend_restart(router, adb, cashu, connected_wifi, screenshot_raw):
    token = cashu.mint(TOKEN_SMALL)

    pay_and_wait(router, adb, token)

    assert assert_internet(adb), "No internet before restart"

    router.restart_backend()

    # Wait for backend to become healthy (MIPS router needs ~15-20s)
    deadline = time.time() + BACKEND_RESTART_TIMEOUT
    health_code = 0
    while time.time() < deadline:
        health_code = router.api_status("/")
        if health_code == 200:
            break
        time.sleep(2)

    assert health_code == 200, \
        f"Backend not responsive after restart within {BACKEND_RESTART_TIMEOUT}s (HTTP {health_code})"

    session_after = router.get_session()

    if session_after.get("session_active"):
        assert assert_internet(adb), "Session active but no internet after restart"
    else:
        state = router.get_nds_state()
        if state == "Authenticated":
            assert assert_internet(adb), \
                "nodogsplash still Authenticated but no internet — session lost after restart"

    screenshot_raw("br-final.png")
