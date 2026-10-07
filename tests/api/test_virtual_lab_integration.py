"""Virtual lab integration tests: full-stack validation from network namespace through OpenWrt VM.

These tests run commands inside the tg-poc-client network namespace on the virtual lab
host (SSH alias "218" by default). They validate the complete path:

    Namespace (tg-poc-client) -> tg-poc-br bridge -> OpenWrt VM (POC_GATEWAY)

Requires:
  - TOLLGATE_VIRTUAL_LAB=1 set in the environment
  - Host 218 reachable via SSH with sudo access
  - tg-poc-client network namespace configured with curl, ping, iproute2
  - OpenWrt VM with nodogsplash (captive portal)
"""

import os
import subprocess

import pytest

from lib.constants import POC_GATEWAY

pytestmark = [pytest.mark.story("P7"),pytest.mark.api, pytest.mark.virtual_lab]

GATEWAY = POC_GATEWAY
CONTAINER = "tg-poc-client"
LAB_HOST = os.environ.get("TOLLGATE_VIRTUAL_LAB_HOST", "218")


def _run_in_container(*args, timeout=15, check=False):
    """Execute a command inside the virtual lab namespace via SSH to the lab host."""
    return subprocess.run(
        ["ssh", LAB_HOST, "sudo", "ip", "netns", "exec", CONTAINER, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _skip_if_no_virtual_lab():
    """Skip all tests unless the virtual lab is running and reachable."""
    if os.environ.get("TOLLGATE_VIRTUAL_LAB") != "1":
        pytest.skip("set TOLLGATE_VIRTUAL_LAB=1 and start the virtual lab first")
    if os.environ.get("TOLLGATE_CLIENT_TYPE") == "container":
        pytest.skip("network namespace tests require local tg-poc-client, not the GCP Debian VM client")

    result = subprocess.run(
        ["ssh", LAB_HOST, "sudo", "ip", "netns", "identify", CONTAINER],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Virtual lab not running (tg-poc-client namespace not found)")


@pytest.mark.extended
def test_container_reaches_gateway():
    """Container can ping the OpenWrt VM gateway at 10.99.99.1."""
    _skip_if_no_virtual_lab()

    result = _run_in_container("ping", "-c", "1", "-W", "2", GATEWAY)
    assert result.returncode == 0, (
        f"Container cannot reach gateway {GATEWAY}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@pytest.mark.extended
def test_container_curl_luci():
    """Container can reach the LuCI admin interface on the OpenWrt VM."""
    _skip_if_no_virtual_lab()

    result = _run_in_container(
        "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
        "--connect-timeout", "5", f"http://{GATEWAY}/cgi-bin/luci/",
    )
    assert result.returncode == 0, (
        f"curl to LuCI failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    http_code = result.stdout.strip()
    assert http_code in ("200", "302", "301"), (
        f"Unexpected HTTP status from LuCI: {http_code}"
    )


@pytest.mark.extended
def test_container_dns_resolution():
    """DNS resolution works through the OpenWrt VM (dnsmasq forwarding)."""
    _skip_if_no_virtual_lab()

    result = _run_in_container(
        "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
        "--connect-timeout", "10", "http://example.com/",
    )
    assert result.returncode == 0, (
        f"DNS resolution / HTTP request failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    http_code = result.stdout.strip()
    assert http_code in ("200", "301", "302"), (
        f"Unexpected HTTP status resolving example.com: {http_code}"
    )


@pytest.mark.extended
def test_captive_portal_detection():
    """Captive portal detection returns nodogsplash splash page or redirect."""
    _skip_if_no_virtual_lab()

    result = _run_in_container(
        # nodogsplash intercepts HTTP on port 80 via iptables, returns splash (200) or redirect (302)
        "curl", "-s", "-w",
        "\n---CURL_META---\nhttp_code:%{http_code}\nredirect_url:%{redirect_url}\nsize_download:%{size_download}",
        "--connect-timeout", "5",
        "http://captiveportal.example.com/",
    )
    assert result.returncode == 0, (
        f"Captive portal detection request failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    parts = result.stdout.split("---CURL_META---\n")
    meta = parts[1] if len(parts) > 1 else ""
    body = parts[0] if len(parts) > 1 else result.stdout

    http_code = ""
    redirect_url = ""
    for line in meta.strip().splitlines():
        if line.startswith("http_code:"):
            http_code = line.split(":", 1)[1].strip()
        elif line.startswith("redirect_url:"):
            redirect_url = line.split(":", 1)[1].strip()

    is_splash = "nodogsplash" in body.lower() or "tollgate" in body.lower()
    is_redirect = http_code in ("302", "301") and redirect_url
    is_intercepted = http_code == "200" and is_splash

    assert is_intercepted or is_redirect, (
        f"Expected nodogsplash interception but got HTTP {http_code}\n"
        f"redirect_url: {redirect_url}\n"
        f"body (first 500 chars): {body[:500]}"
    )


@pytest.mark.extended
def test_container_network_config():
    """Container has an IP in 10.99.99.0/24 and default route via the gateway."""
    _skip_if_no_virtual_lab()

    ip_result = _run_in_container("ip", "-4", "addr", "show", "tg-poc-vc")
    assert ip_result.returncode == 0, (
        f"Could not query namespace IP on tg-poc-vc\n"
        f"stdout:\n{ip_result.stdout}\nstderr:\n{ip_result.stderr}"
    )
    assert "10.99.99." in ip_result.stdout, (
        f"Namespace tg-poc-vc has no IP in 10.99.99.0/24\n"
        f"stdout:\n{ip_result.stdout}"
    )

    route_result = _run_in_container("ip", "route", "show", "default")
    assert route_result.returncode == 0, (
        f"Could not query namespace default route\n"
        f"stdout:\n{route_result.stdout}\nstderr:\n{route_result.stderr}"
    )
    assert f"via {GATEWAY}" in route_result.stdout, (
        f"No default route via gateway {GATEWAY}\n"
        f"stdout:\n{route_result.stdout}"
    )


@pytest.mark.extended
def test_container_curl_tollgate_api():
    """Container can reach TollGate API endpoints (conditional on TollGate being installed)."""
    _skip_if_no_virtual_lab()

    probe = _run_in_container(
        "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
        "--connect-timeout", "3", f"http://{GATEWAY}:2121/",
    )
    if probe.returncode != 0 or probe.stdout.strip() not in ("200", "401", "403"):
        pytest.skip("TollGate API not available on the virtual lab router")

    result = _run_in_container(
        "curl", "-s", "--connect-timeout", "5", f"http://{GATEWAY}:2121/health",
    )
    assert result.returncode == 0, (
        f"TollGate /health request failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "200" in result.stdout or "ok" in result.stdout.lower() or "healthy" in result.stdout.lower(), (
        f"TollGate /health did not return expected healthy response\n"
        f"stdout:\n{result.stdout}"
    )
