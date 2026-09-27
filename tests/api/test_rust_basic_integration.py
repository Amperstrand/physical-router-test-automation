"""Integration tests for tollgate-module-basic-rust wallet operations.

Tests wallet-level behavior that goes beyond HTTP endpoint structure:
- Config validation via CLI
- Wallet migration detection
- Session lifecycle (create → track → expire)
- Error recovery (mint unreachable, token already spent)
- Concurrent requests

Requires:
- TOLLGATE_BACKEND=rust-basic
- TOLLGATE_BINARY_PATH pointing to tollgate-rs binary
- Network access to testnut.cashu.exchange for token minting
"""

import json
import os
import time

import pytest
import requests

pytestmark = [pytest.mark.rust_basic_only, pytest.mark.api, pytest.mark.extended]


def test_config_validation_via_cli(rust_basic_server):
    """CLI config set validates input and writes to disk."""
    sock = rust_basic_server.get("socket_path")
    if not sock or not os.path.exists(sock):
        pytest.skip("CLI socket not available")

    import socket

    def cli(cmd):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(sock)
        s.sendall((cmd + "\n").encode())
        resp = s.recv(65536).decode()
        s.close()
        return resp

    resp = cli("config set metric milliseconds")
    data = json.loads(resp.strip())
    assert data["success"] is True, f"config set failed: {data}"

    resp2 = cli("config set metric invalid_value")
    data2 = json.loads(resp2.strip())
    assert data2["success"] is False, "Should reject invalid metric"


def test_config_set_invalid_step_size(rust_basic_server):
    """CLI config set rejects non-positive step_size."""
    sock = rust_basic_server.get("socket_path")
    if not sock or not os.path.exists(sock):
        pytest.skip("CLI socket not available")

    import socket

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(sock)
    s.sendall(b"config set step_size 0\n")
    resp = s.recv(65536).decode()
    s.close()

    data = json.loads(resp.strip())
    assert data["success"] is False, "Should reject step_size=0"
    assert "positive" in data.get("error", "").lower(), f"Expected 'positive' in error: {data}"


def test_ln_invoice_creates_real_quote(rust_basic_server):
    """POST /ln-invoice creates a real mint quote via CDK wallet.

    Response schema (rust-basic LightningInvoiceResponse): `quote` (ID) and
    `invoice` (BOLT11). When the mint is unreachable the backend degrades to
    `quote="stub-quote-N"` / `invoice="stub-invoice"` with HTTP 200 — skip.
    """
    base = rust_basic_server["http_url"]

    resp = requests.post(
        f"{base}/ln-invoice",
        json={"amount": 1},
        timeout=15,
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}"

    data = resp.json()
    assert "quote" in data, f"Missing 'quote' field: {data}"
    assert data["quote"] != "", f"Empty quote ID: {data}"

    if data["quote"].startswith("stub") or data.get("invoice", "").startswith("stub"):
        pytest.skip("Wallet not connected to mint (stub response)")
    else:
        assert data.get("invoice", "") != "", f"Empty invoice (BOLT11): {data}"


def test_ln_invoice_status_check(rust_basic_server):
    """GET /ln-invoice?quote=<id> returns status for a created quote."""
    base = rust_basic_server["http_url"]

    create = requests.post(f"{base}/ln-invoice", json={"amount": 1}, timeout=15)
    if create.status_code != 200:
        pytest.skip(f"Could not create invoice: {create.status_code}")

    quote_id = create.json().get("quote", "")
    if not quote_id or quote_id.startswith("stub"):
        pytest.skip("Wallet not connected to mint")

    status = requests.get(f"{base}/ln-invoice", params={"quote": quote_id}, timeout=10)
    assert status.status_code == 200, f"Status check failed: {status.status_code}"

    sdata = status.json()
    assert sdata["quote"] == quote_id
    # rust-basic emits `state` ("paid"/"unpaid"); it has no Go-style `checkState`.
    assert sdata["state"] in ("paid", "unpaid"), f"Unexpected state: {sdata['state']}"


def test_ln_invoice_rejects_zero_amount(rust_basic_server):
    """POST /ln-invoice with amount=0 returns 400."""
    base = rust_basic_server["http_url"]

    resp = requests.post(f"{base}/ln-invoice", json={"amount": 0}, timeout=5)
    assert resp.status_code == 400, f"Expected 400 for zero amount, got {resp.status_code}"


def test_ln_invoice_unknown_quote(rust_basic_server):
    """GET /ln-invoice?quote=nonexistent returns 404."""
    base = rust_basic_server["http_url"]

    resp = requests.get(f"{base}/ln-invoice", params={"quote": "nonexistent-id-12345"}, timeout=5)
    assert resp.status_code == 404, f"Expected 404 for unknown quote, got {resp.status_code}"


def test_health_check_via_cli(rust_basic_server):
    """CLI 'health' command returns service status."""
    sock = rust_basic_server.get("socket_path")
    if not sock or not os.path.exists(sock):
        pytest.skip("CLI socket not available")

    import socket

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(sock)
    s.sendall(b"health\n")
    resp = s.recv(65536).decode()
    s.close()

    data = json.loads(resp.strip())
    assert data["success"] is True, f"health failed: {data}"
    health = json.loads(data["message"])
    assert "http_running" in health, f"Missing http_running: {health}"
    assert "wallet_loaded" in health, f"Missing wallet_loaded: {health}"


def test_concurrent_discovery_requests(rust_basic_server):
    """Multiple concurrent GET / requests all succeed."""
    import concurrent.futures

    base = rust_basic_server["http_url"]

    def fetch():
        r = requests.get(f"{base}/", timeout=5)
        return r.status_code == 200 and r.json().get("kind") == 10021

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: fetch(), range(10)))

    assert all(results), f"Not all requests succeeded: {results}"


def test_session_balance_consistency(rust_basic_server):
    """GET /balance returns consistent schema before and after requests."""
    base = rust_basic_server["http_url"]

    r1 = requests.get(f"{base}/balance", timeout=5)
    assert r1.status_code == 200
    b1 = r1.json()

    assert "session_active" in b1, f"Missing session_active: {b1}"
    assert "remaining" in b1, f"Missing remaining: {b1}"
    assert "allotment" in b1, f"Missing allotment: {b1}"

    if not b1["session_active"]:
        r2 = requests.get(f"{base}/balance", timeout=5)
        b2 = r2.json()
        assert b2["session_active"] == b1["session_active"], "State changed without payment"
