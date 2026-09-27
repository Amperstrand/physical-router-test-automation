"""User Story: A person connects to TollGate WiFi, pays a Cashu token
at the captive portal, and gets internet access.

This is THE core user story — it runs against every supported device
(phone, VM, emulator) to catch cross-implementation drift.
"""
import logging
import os
import time

import pytest

from lib.contract import min_token_sats, probe_host
from tests.stories.conftest import get_client_device

log = logging.getLogger("tollgate.story.pay_internet")

pytestmark = [pytest.mark.slow]

DEVICES = ["android-phone"]
if os.environ.get("TOLLGATE_DEBIAN_HOST"):
    DEVICES.append("debian-vm")


@pytest.mark.parametrize("device_place", DEVICES)
def test_user_pays_and_gets_internet(device_place, tollgate_ssid,
                                     story_evidence):
    device = get_client_device(device_place)
    story_evidence.attach(device)

    # Step 1: Join the TollGate WiFi
    log.info("[%s] joining %s", device.name, tollgate_ssid)
    assert device.join_wifi(tollgate_ssid), \
        f"{device.name}: WiFi join failed"
    ip = device.get_ip()
    assert ip, f"{device.name}: no IP assigned"
    log.info("[%s] got IP %s", device.name, ip)
    story_evidence.shot("01-connected",
                        f"{device.name} connected to {tollgate_ssid} with IP {ip}")

    # Step 2: Captive portal is detected (or session already active)
    if device.has_internet():
        log.info("[%s] already has internet (session from previous test "
                 "still active) — skipping portal detection + payment",
                 device.name)
        story_evidence.shot("02-already-authed",
                            f"{device.name} already authenticated "
                            "(existing session)")
        return

    assert device.portal_detected(), f"{device.name}: portal not detected"
    story_evidence.shot("02-portal-detected",
                        f"{device.name} captive portal detected (no internet yet)")

    # Step 3: Submit a valid token (minted host-side)
    from lib.cashu import HttpMinter
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL",
                              "http://192.168.13.221:8383")
    minter = HttpMinter(mint_url)
    token = minter.mint(min_token_sats())
    log.info("[%s] minted %d-char token", device.name, len(token))

    assert device.submit_token(token), \
        f"{device.name}: token submission failed"
    log.info("[%s] token accepted", device.name)
    story_evidence.shot("03-token-accepted",
                        f"{device.name} token accepted by TollGate backend")

    # Step 4: Internet is granted
    deadline = time.time() + 30
    while time.time() < deadline:
        if device.has_internet(probe_host()):
            break
        time.sleep(2)
    assert device.has_internet(probe_host()), \
        f"{device.name}: no internet after payment"

    story_evidence.shot("04-internet",
                        f"{device.name} internet confirmed after payment")
    log.info("[%s] internet confirmed", device.name)
