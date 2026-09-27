"""User Story: A person's paid session expires and they must re-pay
to regain internet access.

Tests the session lifecycle: pay → verify internet → wait for expiry
(or force-deauth) → verify internet is gone → re-pay → verify restored.
"""
import logging
import os
import subprocess
import time

import pytest

from tests.stories.conftest import get_client_device

log = logging.getLogger("tollgate.story.session_expiry")

pytestmark = [pytest.mark.slow]

PHONE_MAC = "24:46:c8:a9:de:bb"
ROUTER_HOST = os.environ.get("TOLLGATE_SSH_HOST", "192.168.13.124")


def _ssh_router(cmd: str) -> str:
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=no",
         f"root@{ROUTER_HOST}", cmd],
        capture_output=True, text=True, timeout=10).stdout.strip()


@pytest.mark.parametrize("device_place", ["android-phone"])
def test_session_expiry_and_repayment(device_place, tollgate_ssid):
    device = get_client_device(device_place)

    # Ensure connected and authenticated
    assert device.join_wifi(tollgate_ssid), "WiFi join failed"
    ip = device.get_ip()
    assert ip, "no IP"

    if not device.has_internet():
        # Need to pay first
        from lib.cashu import HttpMinter
        mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL",
                                  "http://192.168.13.221:8383")
        token = HttpMinter(mint_url).mint(4)
        assert device.submit_token(token), "initial payment failed"
        assert device.has_internet(), "no internet after initial payment"
        log.info("initial payment successful")

    # Force session expiry (deauth simulates timeout)
    _ssh_router(f"ndsctl deauth {PHONE_MAC} 2>/dev/null")
    time.sleep(3)

    # Verify internet is gone
    assert not device.has_internet(), \
        "internet should be gone after session expiry (deauth)"
    log.info("session expired, internet blocked")

    # Re-pay and verify internet restored
    from lib.cashu import HttpMinter
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL",
                              "http://192.168.13.221:8383")
    token = HttpMinter(mint_url).mint(4)
    assert device.submit_token(token), "re-payment failed"
    assert device.has_internet(), "no internet after re-payment"
    log.info("re-payment successful, internet restored")
