"""User Story: A person connects to TollGate WiFi, pays a Cashu token
at the captive portal, and gets internet access.

This is THE core user story — it runs against every supported device
(phone, VM, emulator) to catch cross-implementation drift.
"""
import logging
import os
import time

import pytest

from tests.stories.conftest import get_client_device

log = logging.getLogger("tollgate.story.pay_internet")

pytestmark = [pytest.mark.slow]

DEVICES = ["android-phone"]  # expand as labgrid places come online
if os.environ.get("TOLLGATE_DEBIAN_HOST"):
    DEVICES.append("debian-vm")


@pytest.mark.parametrize("device_place", DEVICES)
def test_user_pays_and_gets_internet(device_place, request):
    device = get_client_device(device_place)
    ssid = os.environ.get("TOLLGATE_SSID", "TollGate")

    # Step 1: Join the TollGate WiFi
    log.info("[%s] joining %s", device.name, ssid)
    assert device.join_wifi(ssid), f"{device.name}: WiFi join failed"
    ip = device.get_ip()
    assert ip, f"{device.name}: no IP assigned"
    log.info("[%s] got IP %s", device.name, ip)

    # Step 2: Captive portal is detected
    assert device.portal_detected(), f"{device.name}: portal not detected"
    device.screenshot(f"artifacts/{device.name}-portal.png")
    log.info("[%s] portal detected", device.name)

    # Step 3: Submit a valid token (minted host-side)
    from lib.cashu import HttpMinter
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL",
                              "http://192.168.105.2:8383")
    minter = HttpMinter(mint_url)
    token = minter.mint(4)
    log.info("[%s] minted %d-char token", device.name, len(token))

    assert device.submit_token(token), \
        f"{device.name}: token submission failed"
    log.info("[%s] token accepted", device.name)

    # Step 4: Internet is granted
    deadline = time.time() + 30
    while time.time() < deadline:
        if device.has_internet():
            break
        time.sleep(2)
    assert device.has_internet(), \
        f"{device.name}: no internet after payment"

    device.screenshot(f"artifacts/{device.name}-internet.png")
    log.info("[%s] internet confirmed", device.name)
