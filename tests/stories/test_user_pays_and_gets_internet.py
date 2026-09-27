"""User Story: A person connects to TollGate WiFi, pays a Cashu token
at the captive portal, and gets internet access.

Uses the state-as-fixture pattern: the test requests `no_session`
(guaranteed unauthenticated) and verifies the full payment flow.
"""
import logging
import os
import time

import pytest

from lib.contract import min_token_sats, probe_host, revalidation_policy
from lib.cashu import HttpMinter

log = logging.getLogger("tollgate.story.pay_internet")

pytestmark = [pytest.mark.slow]


def test_user_pays_and_gets_internet(story_video, no_session, tollgate_ssid,
                                     story_evidence, rate_limiter,
                                     story_logs):
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

    # ── Post-auth nudge: OS revalidation (probe-backoff lesson, §9.5) ──
    # Ping proves routing, but Android's NetworkMonitor caches the
    # captive-portal verdict and backs off re-probing — the OS can stay
    # CAPTIVE_PORTAL (un-VALIDATED) while ping works, and a real user's
    # Chrome keeps fleeing the network. The contract claims
    # internet.validation == "Android VALIDATED network capability";
    # nudge, then hold the story to that claim.
    policy = revalidation_policy()
    log.info("[%s] contract revalidation policy: organic=%s forced=%s",
             device.name, policy["organic_browser_activity"],
             policy["forced_reprobe"])
    if not device.os_validated():
        log.info("[%s] OS verdict stale — browser nudge (example.com)",
                 device.name)
        device.open_url("http://example.com/")
        time.sleep(6)

    if not device.os_validated():
        log.info("[%s] browser nudge insufficient — wifi-cycle nudge",
                 device.name)
        assert device.wifi_cycle(tollgate_ssid), \
            f"{device.name}: wifi-cycle nudge lost the association (RF/selector)"

    deadline = time.time() + 45
    while time.time() < deadline:
        if device.os_validated():
            break
        time.sleep(3)

    story_evidence.shot("04-os-validated",
                        f"{device.name} Android VALIDATED capability after nudge")
    assert device.os_validated(), \
        (f"{device.name}: contract falsified (internet.validation) — "
         "TollGate granted internet (payment + ping OK) but Android never "
         "re-validated the network")
    log.info("[%s] OS validation confirmed", device.name)

    device.open_url("http://example.com/")
    time.sleep(6)
    story_evidence.shot("05-browser-internet",
                        f"{device.name} Chrome loads example.com — human truth")
