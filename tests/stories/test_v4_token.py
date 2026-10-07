"""User Story: A person pays with a V4/CBOR Cashu token (cashuB prefix).

Tests that the backend accepts the newer token format alongside V3.
"""
import logging
import os
import subprocess

import pytest

from lib.contract import backend_port, min_token_sats
from tests.stories.conftest import (
    get_client_device, story_client_name, _deauth_device,
)

log = logging.getLogger("tollgate.story.v4_token")

pytestmark = [pytest.mark.story("R2"),pytest.mark.slow]


def _mint_v4_token(mint_url: str, amount: int) -> str:
    """Mint a V4 (cashuB) token using cdk-cli."""
    import tempfile
    last_err = ""
    for attempt in range(2):
        work = tempfile.mkdtemp(prefix="cdk-v4-")
        m = subprocess.run(
            ["/opt/cdk-mintd/cdk-cli", "-w", work, "mint", mint_url, str(amount)],
            capture_output=True, text=True, timeout=120)
        r = subprocess.run(
            ["/opt/cdk-mintd/cdk-cli", "-w", work, "send", "--mint-url", mint_url],
            input=str(amount), capture_output=True, text=True, timeout=60)
        for line in r.stdout.split("\n"):
            if line.strip().startswith("cashuB"):
                return line.strip()
        last_err = (f"mint(rc={m.returncode}): {m.stderr[-150:]} "
                    f"send(rc={r.returncode}): {r.stderr[-150:]}")
        log.warning("V4 mint attempt %d/2 failed: %s", attempt + 1, last_err)
    raise RuntimeError(f"cdk-cli produced no V4 token: {last_err}")


@pytest.mark.skipif(
    not os.environ.get("TOLLGATE_TEST_MINT_URL"),
    reason="TOLLGATE_TEST_MINT_URL not set")
@pytest.mark.parametrize("device_place", [story_client_name()])
def test_v4_token_accepted(device_place, tollgate_ssid):
    """Backend accepts a V4/CBOR (cashuB) token for payment."""
    device = get_client_device(device_place)
    mint_url = os.environ["TOLLGATE_TEST_MINT_URL"]

    # Ensure connected
    assert device.join_wifi(tollgate_ssid), "WiFi join failed"
    ip = device.get_ip()
    assert ip, "no IP"

    # If already authenticated, deauth to test payment
    if device.has_internet():
        _deauth_device(device)

    # Mint and submit a V4 token
    token = _mint_v4_token(mint_url, min_token_sats())
    assert token.startswith("cashuB"), f"not a V4 token: {token[:20]}"
    log.info("minted V4 token (%d chars, cashuB prefix)", len(token))

    assert device.submit_token(token), \
        "V4 token submission failed — backend may not support cashuB format"
    log.info("V4 token accepted by backend")

    assert device.has_internet(), "no internet after V4 token payment"
    log.info("internet confirmed after V4 token payment")
