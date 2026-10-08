# Session Extension (Top-Up)
#
# Verifies that paying a second time extends the existing session's allotment.
# Known issue: as of current main, the backend returns the same allotment on
# repeated payments — it replaces the session instead of adding to it. The test
# uses pytest.xfail to record this as an expected failure without blocking the
# suite. This is a pre-existing behavior, not a regression from PR #120.

import time
import pytest
from lib.helpers import pay_and_wait, assert_internet, is_session_event
from lib.constants import TOKEN_LONG

pytestmark = [pytest.mark.story("G4"),pytest.mark.phone, pytest.mark.slow, pytest.mark.timeout(120), pytest.mark.extended, pytest.mark.requires_wifi]


@pytest.mark.xfail(reason="Backend replaces allotment instead of extending (pre-existing, not PR #120)")
def test_session_extension(router, adb, cashu, connected_wifi, screenshot_raw):
    token1 = cashu.mint(TOKEN_LONG)
    resp1 = pay_and_wait(router, adb, token1)
    allotment1 = 0
    for tag in resp1.get("tags", []):
        if isinstance(tag, list) and tag[0] == "allotment":
            allotment1 = int(tag[1])

    time.sleep(3)

    token2 = cashu.mint(TOKEN_LONG)
    resp2 = router.pay_direct(token2)
    assert is_session_event(resp2), \
        f"Second payment rejected: {str(resp2)[:200]}"

    allotment2 = 0
    for tag in resp2.get("tags", []):
        if isinstance(tag, list) and tag[0] == "allotment":
            allotment2 = int(tag[1])

    assert allotment2 > allotment1, \
        f"Session not extended: allotment {allotment1}ms -> {allotment2}ms"

    state = router.get_nds_state()
    assert state == "Authenticated", f"Client not authenticated after extension: {state}"

    assert assert_internet(adb), "No internet after extension"

    screenshot_raw("ext-final.png")
