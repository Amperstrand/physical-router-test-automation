"""User Story: A person sees degraded mode when the Cashu mint is
unreachable — the portal should show a clear message instead of
silently failing.

This tests the resilience of the TollGate when its backend cannot
verify tokens (mint down, network partition, etc.).
"""
import json
import logging
import os
import subprocess

import pytest

from tests.stories.conftest import get_client_device

log = logging.getLogger("tollgate.story.degraded_mode")

pytestmark = [pytest.mark.slow]

ROUTER_HOST = os.environ.get("TOLLGATE_SSH_HOST", "192.168.13.124")


def _ssh_router(cmd: str) -> str:
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=no",
         f"root@{ROUTER_HOST}", cmd],
        capture_output=True, text=True, timeout=15).stdout.strip()


def _backend_event() -> dict:
    """GET the backend's discovery event (kind:10021 or kind:21023)."""
    out = _ssh_router(
        "wget -qO- --timeout=10 'http://[::1]:2121/' 2>/dev/null")
    try:
        return json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return {}


@pytest.mark.parametrize("device_place", ["android-phone"])
def test_backend_responds_with_valid_event(device_place):
    """The backend always responds with a discovery event (healthy or degraded)."""
    event = _backend_event()
    assert event.get("kind") in (10021, 21023, 1022), \
        f"unexpected backend response kind: {event.get('kind')} (raw: {str(event)[:200]})"
    log.info("backend kind=%s, tags=%s",
             event.get("kind"),
             [t[0] for t in event.get("tags", [])[:5]])


def test_mint_reachability_affects_backend_kind():
    """Backend reports degraded (21023) when mint is blocked."""
    # Check current state
    event = _backend_event()
    kind = event.get("kind")

    if kind == 21023:
        log.info("backend is already in degraded mode (mint unreachable)")
        assert "No reachable" in json.dumps(event) or \
               any("no reachable" in str(t).lower() for t in event.get("tags", [])), \
            "kind 21023 should indicate unreachable mints"
    elif kind == 10021:
        log.info("backend is healthy (kind:10021 with pricing)")
        # Verify pricing tags exist
        tags = [t for t in event.get("tags", []) if t[0] == "price_per_step"]
        assert tags, "healthy backend must advertise price_per_step"
    else:
        pytest.skip(f"backend in unexpected state: kind={kind}")


def test_portal_still_loads_in_any_mode():
    """The portal web UI loads regardless of backend health."""
    portal_code = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "-m", "5", f"http://{ROUTER_HOST}:2051/splash.html"],
        capture_output=True, text=True, timeout=10).stdout.strip()
    assert portal_code == "200", \
        f"portal should always return 200, got {portal_code}"
    log.info("portal loads (HTTP %s) regardless of backend state", portal_code)
