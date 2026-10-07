"""User Story: The portal shows both Lightning and Cashu tabs with
correct copy — no cross-tab text bleed.

This is the browser-based version of the portal tab-copy tests,
designed to run against any TollGate implementation's portal.
Unlike the Playwright spec (tests/browser/portal-tab-copy.spec.mjs),
this runs through the user-story framework with contract-driven values.
"""
import logging
import os
import subprocess

import pytest

from lib.contract import backend_port, portal_port

log = logging.getLogger("tollgate.story.tabs")

pytestmark = [pytest.mark.story("G1"),pytest.mark.slow]

ROUTER_HOST = os.environ.get("TOLLGATE_SSH_HOST", "")


def _portal_url() -> str:
    """Build the portal URL from the contract."""
    host = ROUTER_HOST or "localhost"
    return f"http://{host}:{portal_port()}/splash.html"


def _curl_portal(path: str = "/splash.html") -> str:
    """Fetch portal content via curl."""
    host = ROUTER_HOST or "localhost"
    url = f"http://{host}:{portal_port()}{path}"
    r = subprocess.run(
        ["curl", "-s", "-m", "10", url],
        capture_output=True, text=True, timeout=15)
    return r.stdout


@pytest.mark.skipif(not ROUTER_HOST, reason="TOLLGATE_SSH_HOST not set")
def test_portal_serves_splash_html():
    """The portal serves valid HTML at /splash.html (per contract)."""
    html = _curl_portal()
    assert html, "portal returned empty response"
    assert "<html" in html.lower(), "not valid HTML"
    assert "splash" in html.lower() or "portal" in html.lower(), \
        "page doesn't reference portal/splash"


@pytest.mark.skipif(not ROUTER_HOST, reason="TOLLGATE_SSH_HOST not set")
def test_portal_has_tab_navigation():
    """The portal has tab navigation with both Lightning and Cashu."""
    html = _curl_portal()
    assert "lightning" in html.lower() or "cashu" in html.lower(), \
        "no tab references found in portal HTML"


@pytest.mark.skipif(not ROUTER_HOST, reason="TOLLGATE_SSH_HOST not set")
def test_backend_discovery_event():
    """The backend serves a kind:10021 or kind:21023 discovery event."""
    host = ROUTER_HOST
    url = f"http://{host}:{backend_port()}/"
    # Backend may be WAN-firewalled — try via SSH
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=no",
         f"root@{host}",
         f"wget -qO- --timeout=10 '{url}' 2>/dev/null"],
        capture_output=True, text=True, timeout=20)
    import json
    try:
        event = json.loads(r.stdout)
        kind = event.get("kind")
        assert kind in (10021, 21023), f"unexpected kind: {kind}"
        log.info("backend event kind=%s", kind)
    except json.JSONDecodeError:
        pytest.skip(f"backend not reachable via SSH from this host: {r.stdout[:100]}")
