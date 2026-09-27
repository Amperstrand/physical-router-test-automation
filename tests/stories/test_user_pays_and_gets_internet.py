"""User Story: A person connects to TollGate WiFi, pays a Cashu token
at the captive portal, and gets internet access.

Uses the state-as-fixture pattern: the test requests `no_session`
(guaranteed unauthenticated) and verifies the full payment flow.
"""
import logging
import os
import time

import pytest

from lib.contract import min_token_sats, probe_host
from lib.cashu import HttpMinter

log = logging.getLogger("tollgate.story.pay_internet")

pytestmark = [pytest.mark.slow]


def test_user_pays_and_gets_internet(no_session, story_evidence,
                                     rate_limiter):
    device = no_session
    story_evidence.attach(device)

    assert not device.has_internet(), \
        "precondition failed: device should NOT have internet (no_session)"
    log.info("[%s] precondition: unauthenticated", device.name)
    story_evidence.shot("01-portal-visible",
                        f"{device.name} unauthenticated, portal visible")

    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL",
                              "http://192.168.13.221:8383")
    minter = HttpMinter(mint_url)
    token = minter.mint(min_token_sats())
    log.info("[%s] minted %d-char token", device.name, len(token))

    rate_limiter()
    assert device.submit_token(token), \
        f"{device.name}: token submission failed"
    log.info("[%s] token accepted", device.name)
    story_evidence.shot("02-token-accepted",
                        f"{device.name} token accepted by TollGate backend")

    deadline = time.time() + 30
    while time.time() < deadline:
        if device.has_internet(probe_host()):
            break
        time.sleep(2)
    assert device.has_internet(probe_host()), \
        f"{device.name}: no internet after payment"

    story_evidence.shot("03-internet-confirmed",
                        f"{device.name} internet confirmed after payment")
    log.info("[%s] internet confirmed", device.name)
