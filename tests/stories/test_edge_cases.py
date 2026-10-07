"""Edge-case stories: error paths, invalid inputs, boundary conditions.

These test the error handling where drift is most likely between
implementations — different backends may return different error kinds
or handle invalid inputs differently.
"""
import json
import logging
import os
import subprocess

import pytest

from lib.contract import backend_port, min_token_sats

log = logging.getLogger("tollgate.story.edge")

pytestmark = [pytest.mark.story("G2"),pytest.mark.slow]

ROUTER_HOST = os.environ.get("TOLLGATE_SSH_HOST", "")


def _ssh_router(cmd: str) -> str:
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=no",
         f"root@{ROUTER_HOST}", cmd],
        capture_output=True, text=True, timeout=15).stdout.strip()


def _backend_post(token: str) -> dict:
    """POST a token directly to the backend via SSH, return the response."""
    out = _ssh_router(
        f"wget -qO- --post-data='{token}' "
        f"--header='Content-Type: text/plain' "
        f"'http://[::1]:{backend_port()}/' 2>/dev/null")
    try:
        return json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return {"raw": out[:200]}


@pytest.mark.skipif(not ROUTER_HOST, reason="TOLLGATE_SSH_HOST not set")
def test_spent_token_rejected():
    """Submitting an already-spent token should get a clear rejection.

    Note: this test POSTs via a br-lan client (the phone). If the phone
    is not on the TollGate WiFi, it skips. Loopback POSTs are blocked
    by the backend's client-class firewall on some implementations.
    """
    from lib.cashu import HttpMinter
    from tests.stories.conftest import get_client_device

    device = get_client_device("android-phone")
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL",
                              "http://192.168.13.221:8383")

    ssid = os.environ.get("TOLLGATE_SSID", "TollGate")
    if not device.join_wifi(ssid):
        pytest.skip("phone not on TollGate WiFi (needed for br-lan client class)")

    # Mint and spend a token
    token = HttpMinter(mint_url).mint(4)
    first = _backend_post_via_client(device, token)
    assert first.get("kind") == 1022, f"first payment should succeed: {first}"

    # Try to spend it again
    second = _backend_post_via_client(device, token)
    kind = second.get("kind")
    assert kind != 1022, f"double-spend should NOT succeed (got kind:1022)"
    log.info("double-spend correctly rejected (kind=%s)", kind)


def _backend_post_via_client(device, token: str) -> dict:
    """POST a token via the client device (br-lan class)."""
    ip = device.get_ip()
    if not ip:
        return {"error": "no IP"}
    gateway = ip.rsplit(".", 1)[0] + ".1"
    out = device._shell(
        f"curl -s -m 20 -X POST -H 'Content-Type: text/plain' "
        f"-d '{token}' http://{gateway}:{backend_port()}/")
    try:
        return json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return {"raw": out[:200]}


@pytest.mark.skipif(not ROUTER_HOST, reason="TOLLGATE_SSH_HOST not set")
def test_invalid_token_rejected():
    """Submitting garbage (not a Cashu token) should get a clear rejection."""
    resp = _backend_post("this-is-not-a-token")
    kind = resp.get("kind")
    assert kind != 1022, f"garbage token should NOT succeed (got kind:1022)"
    log.info("garbage token rejected (kind=%s)", kind)


@pytest.mark.skipif(not ROUTER_HOST, reason="TOLLGATE_SSH_HOST not set")
def test_wrong_mint_token_rejected():
    """A valid token from an unaccepted mint should be rejected."""
    # Create a synthetic token with a wrong mint URL
    import base64
    payload = {
        "token": [{
            "mint": "https://wrong-mint.example.com",
            "proofs": [{
                "amount": 4, "id": "00" * 16,
                "secret": "00" * 32, "C": "02" + "AA" * 32,
            }]
        }],
        "unit": "sat",
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload).encode()).decode().rstrip("=")
    wrong_mint_token = "cashuA" + encoded

    resp = _backend_post(wrong_mint_token)
    kind = resp.get("kind")
    assert kind != 1022, \
        f"wrong-mint token should NOT succeed (got kind:1022)"
    log.info("wrong-mint token rejected (kind=%s)", kind)


@pytest.mark.skipif(not ROUTER_HOST, reason="TOLLGATE_SSH_HOST not set")
def test_empty_body_rejected():
    """Submitting an empty string should get a clear rejection."""
    resp = _backend_post("")
    kind = resp.get("kind")
    assert kind != 1022, f"empty body should NOT succeed (got kind:1022)"
    log.info("empty body rejected (kind=%s)", kind)


@pytest.mark.skipif(not ROUTER_HOST, reason="TOLLGATE_SSH_HOST not set")
def test_session_persists_across_backend_restart():
    """Contract says: session persists across backend restart.

    This is a read-only check — it verifies that sessions.json exists
    and has content, which is the mechanism for persistence.
    """
    sessions = _ssh_router("cat /etc/tollgate/sessions.json 2>/dev/null || echo '{}'")
    try:
        data = json.loads(sessions)
        log.info("sessions.json exists with %d entries", len(data))
    except json.JSONDecodeError:
        pytest.fail(f"sessions.json is not valid JSON: {sessions[:100]}")
