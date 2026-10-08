"""Contract validation: verify the behavior contract matches reality.

These tests check that the TollGate implementation actually behaves as
the contract JSON says it should. If these fail, either the contract
or the implementation needs updating — the diff makes it visible.
"""
import json
import logging
import os
import subprocess

import pytest

from lib.contract import (
    backend_port,
    get_contract,
    portal_port,
    probe_host,
    ssid_prefix,
)

log = logging.getLogger("tollgate.story.contract")

ROUTER_HOST = os.environ.get("TOLLGATE_SSH_HOST", "")


pytestmark = [pytest.mark.story("P7")]

def _ssh(cmd: str) -> str:
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=no",
         f"root@{ROUTER_HOST}", cmd],
        capture_output=True, text=True, timeout=15).stdout.strip()


def _curl(url: str, timeout: int = 10) -> tuple[int, str]:
    r = subprocess.run(
        ["curl", "-s", "-m", str(timeout), "-w", "\n%{http_code}", url],
        capture_output=True, text=True, timeout=timeout + 5)
    lines = r.stdout.rsplit("\n", 1)
    code = int(lines[-1]) if len(lines) > 1 and lines[-1].isdigit() else 0
    body = lines[0] if lines else ""
    return code, body


@pytest.mark.skipif(not ROUTER_HOST, reason="TOLLGATE_SSH_HOST not set")
class TestPortalContract:
    def test_splash_html_served(self):
        """Contract: portal serves HTML at /splash.html."""
        code, body = _curl(f"http://{ROUTER_HOST}:{portal_port()}/splash.html")
        assert code == 200, f"portal returned {code}, contract expects 200"
        assert "<html" in body.lower()

    def test_root_returns_error_or_redirect(self):
        """Contract: portal root (/) does NOT serve the SPA (no index.html)."""
        code, _ = _curl(f"http://{ROUTER_HOST}:{portal_port()}/")
        assert code in (403, 404, 302, 307), \
            f"root returned {code}, contract expects error/redirect"

    def test_no_permanent_redirects(self):
        """Contract: portal uses 302/307 only, never 301."""
        code, _ = _curl(f"http://{ROUTER_HOST}:{portal_port()}/splash.html")
        assert code != 301, "portal must not use 301 (permanently cached)"


@pytest.mark.skipif(not ROUTER_HOST, reason="TOLLGATE_SSH_HOST not set")
class TestBackendContract:
    def test_backend_serves_discovery(self):
        """Contract: backend serves kind:10021 or kind:21023."""
        out = _ssh(f"wget -qO- --timeout=10 'http://[::1]:{backend_port()}/' 2>/dev/null")
        event = json.loads(out)
        assert event.get("kind") in (10021, 21023), \
            f"backend kind={event.get('kind')}, contract expects 10021/21023"

    def test_backend_advertises_pricing(self):
        """Contract: healthy backend advertises price_per_step tags."""
        out = _ssh(f"wget -qO- --timeout=10 'http://[::1]:{backend_port()}/' 2>/dev/null")
        event = json.loads(out)
        if event.get("kind") != 10021:
            pytest.skip("backend in degraded mode (kind != 10021)")
        tags = [t for t in event.get("tags", []) if t[0] == "price_per_step"]
        assert tags, "healthy backend must advertise price_per_step"


@pytest.mark.skipif(not ROUTER_HOST, reason="TOLLGATE_SSH_HOST not set")
class TestWirelessContract:
    def test_ssid_matches_prefix(self):
        """Contract: SSID starts with the contract prefix."""
        ssid = _ssh("iwinfo 2>/dev/null | grep ESSID | head -1 | grep -o '\"[^\"]*\"' | tr -d '\"'")
        assert ssid.startswith(ssid_prefix()), \
            f"SSID '{ssid}' doesn't match prefix '{ssid_prefix()}'"


@pytest.mark.skipif(not ROUTER_HOST, reason="TOLLGATE_SSH_HOST not set")
class TestInfrastructureContract:
    def test_config_at_expected_path(self):
        """Contract: config.json at /etc/tollgate/config.json."""
        assert _ssh("test -f /etc/tollgate/config.json && echo exists") == "exists"

    def test_service_running(self):
        """Contract: tollgate-wrt service is running."""
        assert "running" in _ssh("/etc/init.d/tollgate-wrt status 2>/dev/null || true")

    def test_nds_running(self):
        """Contract: nodogsplash service is running."""
        status = _ssh("/etc/init.d/nodogsplash status 2>/dev/null || true")
        assert "running" in status or "true" in status.lower()
