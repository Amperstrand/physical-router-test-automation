"""User Story: A person's paid session expires and they must re-pay
to regain internet access.

Uses the state-as-fixture pattern: the test requests `fresh_session`
(guaranteed authenticated) and tests the expiry → re-payment cycle.
"""
import logging
import os
import subprocess
import time

import pytest

from lib.contract import min_token_sats
from lib.cashu import HttpMinter

log = logging.getLogger("tollgate.story.session_expiry")

pytestmark = [pytest.mark.slow]

PHONE_MAC = "24:46:c8:a9:de:bb"


def _deauth():
    host = os.environ.get("TOLLGATE_SSH_HOST", "")
    if not host:
        return
    subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=no",
         f"root@{host}", f"ndsctl deauth {PHONE_MAC} 2>/dev/null"],
        capture_output=True, timeout=10)


def test_session_expiry_and_repayment(fresh_session, rate_limiter):
    device = fresh_session

    # Precondition: authenticated (guaranteed by fresh_session fixture)
    assert device.has_internet(), \
        "precondition failed: device should have internet (fresh_session)"
    log.info("precondition: authenticated with internet")

    # Act: force session expiry
    _deauth()
    time.sleep(3)

    # Assert: internet is gone
    assert not device.has_internet(), \
        "internet should be gone after session expiry"
    log.info("session expired, internet blocked")

    # Act: re-pay
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL",
                              "http://192.168.13.221:8383")
    token = HttpMinter(mint_url).mint(min_token_sats())

    rate_limiter()
    assert device.submit_token(token), "re-payment failed"
    log.info("re-payment submitted")

    deadline = time.time() + 30
    while time.time() < deadline:
        if device.has_internet():
            break
        time.sleep(2)
    assert device.has_internet(), "no internet after re-payment"
    log.info("re-payment successful, internet restored")
