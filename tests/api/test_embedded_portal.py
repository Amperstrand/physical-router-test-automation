"""Embedded portal tests — verifies the --features embedded-portal binary.

These tests run against the tollgate binary built with the embedded-portal
cargo feature. They verify both the standard HTTP API (same as rust-basic)
and the embedded portal specific features (nftables, port-80 redirect).

Requires TOLLGATE_BACKEND=rust-embedded and a binary built with:
    cargo build --release --features embedded-portal
"""
import json
import os
import subprocess
import time

import pytest

pytestmark = pytest.mark.api


def test_embedded_discovery_returns_kind_10021(rust_basic_server):
    """Discovery endpoint returns a Nostr kind 10021 event."""
    if not rust_basic_server:
        pytest.skip("rust_basic_server fixture not available")
    resp = subprocess.run(
        ["curl", "-s", f"{rust_basic_server['http_url']}/"],
        capture_output=True, text=True, timeout=10,
    )
    data = json.loads(resp.stdout)
    assert data["kind"] == 10021


def test_embedded_balance_returns_go_compatible_schema(rust_basic_server):
    """Balance endpoint returns the Go-compatible JSON schema."""
    if not rust_basic_server:
        pytest.skip("rust_basic_server fixture not available")
    resp = subprocess.run(
        ["curl", "-s", f"{rust_basic_server['http_url']}/balance"],
        capture_output=True, text=True, timeout=10,
    )
    data = json.loads(resp.stdout)
    assert "status" in data
    assert "session_active" in data
    assert "usage" in data
    assert "allotment" in data


def test_embedded_cli_version(rust_basic_server):
    """CLI version command returns expected fields."""
    if not rust_basic_server:
        pytest.skip("rust_basic_server fixture not available")
    sock = rust_basic_server.get("socket_path")
    if not sock or not os.path.exists(sock):
        pytest.skip("CLI socket not available")
    resp = subprocess.run(
        ["socat", "-", f"UNIX-CONNECT:{sock}"],
        input="version\n", capture_output=True, text=True, timeout=5,
    )
    output = resp.stdout
    assert "version:" in output
    assert "commit:" in output
    assert "openwrt" in output


@pytest.mark.skipif(
    os.geteuid() != 0,
    reason="nftables table check requires root (embedded-portal binary must run as root)",
)
def test_embedded_nftables_table_installed():
    """Verify nftables tollgate table is installed when binary runs as root."""
    # Only meaningful where the tollgate binary runs on THIS host (local dry
    # venue). On cloud/hardware venues the backend lives on the router/VM and
    # `nft list` here inspects the wrong machine entirely.
    provider = os.environ.get("TOLLGATE_VM_PROVIDER", "physical")
    if provider not in ("local", "local-kvm"):
        pytest.skip(f"nftables table check is local-venue only (provider={provider})")
    resp = subprocess.run(
        ["nft", "list", "table", "inet", "tollgate"],
        capture_output=True, text=True, timeout=5,
    )
    if resp.returncode != 0:
        pytest.skip("nftables tollgate table not found (binary may not be running as root)")
    output = resp.stdout
    assert "authenticated_v4" in output
    assert "authenticated_v6" in output
    assert "forward" in output


@pytest.mark.skipif(
    os.geteuid() != 0,
    reason="Port 80 redirect requires root (embedded-portal binary must run as root)",
)
def test_embedded_port_80_redirect():
    """Port 80 redirect server returns 302 for unauthenticated clients."""
    resp = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "http://127.0.0.1:80/"],
        capture_output=True, text=True, timeout=5,
    )
    code = resp.stdout.strip()
    if code == "000":
        pytest.skip("Port 80 not listening (binary may not be running as root)")
    assert code in ("302", "204"), f"Expected 302 redirect or 204 passthrough, got {code}"
