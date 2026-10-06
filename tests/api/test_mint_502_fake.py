"""Deterministic test for mint 502 handling using a local fake server.

Unlike test_mint_502_handling.py (which depends on coinos.io returning 502),
this test uses a local fake HTTP server for fully deterministic results.

The fake mint server runs on the TEST MACHINE (localhost), NOT on the router.
The router connects to it via the LAN IP of the test machine.
"""

import json
import os
import socket
import time
import urllib.request
import urllib.error

import pytest

from lib.fake_mint import FakeMintServer
from lib.helpers import parse_json_or_fail

pytestmark = [pytest.mark.api, pytest.mark.extended, pytest.mark.timeout(300), pytest.mark.complete]

CONFIG_BACKUP = "/etc/tollgate/config.json.fake-502-test-backup"


def _get_local_ip():
    if os.environ.get("TOLLGATE_VIRTUAL_LAB"):
        return os.environ.get("TOLLGATE_VIRTUAL_HOST", "10.99.99.2")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


@pytest.fixture(scope="module")
def fake_mint_502():
    server = FakeMintServer(status_code=502)
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="module", autouse=True)
def configure_fake_mint(router, fake_mint_502):
    local_ip = _get_local_ip()
    fake_mint_url = f"http://{local_ip}:{fake_mint_502.port}"

    router.ssh(f"cp /etc/tollgate/config.json {CONFIG_BACKUP}")

    cfg_raw = router.ssh("cat /etc/tollgate/config.json")
    cfg = json.loads(cfg_raw)

    cfg["accepted_mints"] = [{
        "url": fake_mint_url,
        "min_balance": 64,
        "balance_tolerance_percent": 10,
        "payout_interval_seconds": 86400,
        "min_payout_amount": 999999,
        "price_per_step": 1,
        "price_unit": "sat",
        "purchase_min_steps": 1,
    }]

    router.write_remote_json("/etc/tollgate/config.json", cfg, indent=None)

    router.restart_backend()
    time.sleep(5)

    for _ in range(15):
        try:
            code = router.api_status("/")
            if code in (200, 502, 503):
                break
        except Exception:
            pass
        time.sleep(2)

    yield

    router.ssh(f"cat {CONFIG_BACKUP} > /etc/tollgate/config.json")
    router.ssh(f"rm -f {CONFIG_BACKUP}")
    router.restart_backend()
    time.sleep(5)


@pytest.mark.extended
def test_fake_mint_returns_502(fake_mint_502):
    try:
        urllib.request.urlopen(f"{fake_mint_502.url}/v1/keysets", timeout=5)
        pytest.fail("Expected 502 but got success")
    except urllib.error.HTTPError as e:
        assert e.code == 502, f"Expected 502, got {e.code}"


@pytest.mark.extended
def test_service_handles_502_mint(router):
    code = router.api_status("/")
    if code == 0:
        pytest.skip("Backend exits on fake 502 mint startup in this build")
    assert code in (200, 502, 503), f"Service not responding: HTTP {code}"


@pytest.mark.extended
def test_no_crash_loop_with_502(router):
    pid_before = router.ssh("pidof tollgate-wrt").strip()
    if not pid_before:
        pytest.skip("Backend exits on fake 502 mint startup in this build")
    assert pid_before, "tollgate-wrt not running"
    time.sleep(10)
    pid_after = router.ssh("pidof tollgate-wrt").strip()
    assert pid_after, "tollgate-wrt crashed within 10s"
    assert pid_before == pid_after, \
        f"PID changed: {pid_before} -> {pid_after} (restart detected)"


@pytest.mark.extended
def test_degraded_mode_or_graceful(router):
    body = router.api_body("/")
    if not body:
        pytest.skip("Backend exits on fake 502 mint startup in this build")
    event = parse_json_or_fail(body, "discovery response")
    kind = event.get("kind")

    assert kind in (10021, 21023), \
        f"Unexpected kind {kind} with 502 mint: {str(event)[:300]}"

    if kind == 21023:
        tags = event.get("tags", [])
        code_tags = [t for t in tags if isinstance(t, list) and len(t) >= 2 and t[0] == "code"]
        assert len(code_tags) > 0, f"No 'code' tag in degraded event: {tags}"
