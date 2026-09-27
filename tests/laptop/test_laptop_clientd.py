"""tests/laptop/test_laptop_clientd.py — the laptop lane.

Runs scripts/tollgate-clientd.py from THIS host (the "laptop") against a
real TollGate router — real ARP resolution, real NoDogSplash MAC
registration via the port-80 workaround, real ndsctl session state. This
is the one class the module repo's docker cloud-lab cannot test
(fake ndsctl there): here the router itself resolves our IP to a MAC and
NDS authenticates it.

Provisioning (see docs/tollgate-clientd.md "The laptop lane" for the full
recipe): a router reachable at LAPTOP_GATEWAY whose accepted mint answers
reachable from both sides, SSH access to the router for verification, and
a funded cdk-cli wallet at LAPTOP_WALLET_DIR (cdk-cli must resolve in
PATH — a docker shim is documented).

Environment:
  LAPTOP_GATEWAY     router IP (required; module skips without it)
  LAPTOP_IFACE       interface toward the router (default: route-derived)
  LAPTOP_SSH_HOST    router SSH host (default: LAPTOP_GATEWAY)
  LAPTOP_SSH_PASSWORD  router root password
  LAPTOP_WALLET_DIR  funded cdk-cli wallet dir (default /tmp/laptop-wallet)

Run:  make test-laptop-clientd   (or pytest tests/laptop/ -v directly)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request

import pytest

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
CLIENTD = os.path.join(REPO, "scripts", "tollgate-clientd.py")

GATEWAY = os.environ.get("LAPTOP_GATEWAY", "")
IFACE = os.environ.get("LAPTOP_IFACE", "")
SSH_HOST = os.environ.get("LAPTOP_SSH_HOST", GATEWAY)
SSH_PASSWORD = os.environ.get("LAPTOP_SSH_PASSWORD", "tollgate")
WALLET_DIR = os.environ.get("LAPTOP_WALLET_DIR", "/tmp/laptop-wallet")

pytestmark = pytest.mark.skipif(
    not GATEWAY, reason="LAPTOP_GATEWAY not set — laptop lane not provisioned")


def run_clientd(*args, timeout=120):
    return subprocess.run(
        ["python3", CLIENTD, "--gateway", GATEWAY, *args],
        capture_output=True, text=True, timeout=timeout, check=False)


def ssh_router(cmd, timeout=30):
    proc = subprocess.run(
        ["sshpass", "-p", SSH_PASSWORD, "ssh", "-o", "StrictHostKeyChecking=no",
         f"root@{SSH_HOST}", cmd],
        capture_output=True, text=True, timeout=timeout, check=False)
    assert proc.returncode == 0, f"router ssh failed: {proc.stderr}"
    return proc.stdout


def http_get(path="/"):
    with urllib.request.urlopen(f"http://{GATEWAY}:2121{path}", timeout=5) as r:
        return r.read().decode()


def client_mac() -> tuple[str, str]:
    """(source_ip, mac) the router will see for this host."""
    route = subprocess.run(
        ["ip", "route", "get", GATEWAY], capture_output=True, text=True,
        check=False).stdout
    m = re.search(r"dev (\S+).*src (\d+\.\d+\.\d+\.\d+)", route)
    if not m:
        pytest.skip(f"cannot derive route to {GATEWAY}: {route!r}")
    iface, src_ip = m.group(1), m.group(2)
    with open(f"/sys/class/net/{IFACE or iface}/address") as f:
        return src_ip, f.read().strip()


def usage(src_ip):
    try:
        body = http_get("/usage").strip()
    except OSError:
        return None  # service restarting — let wait_for retry
    m = re.fullmatch(r"(-?\d+)/(-?\d+)", body)
    assert m, f"unexpected /usage: {body!r}"
    used, allotment = int(m.group(1)), int(m.group(2))
    if used < 0 or allotment < 0:
        return None
    return used, allotment


def allotment_of(src_ip):
    u = usage(src_ip)
    return u[1] if u else 0


def wait_for(predicate, timeout, what):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(2)
    pytest.fail(f"timed out waiting for {what} (last={last!r})")


@pytest.fixture(scope="module")
def provisioned():
    if not shutil.which("cdk-cli"):
        pytest.skip("cdk-cli not in PATH (see docs/tollgate-clientd.md for the "
                    "docker shim)")
    wait_for(lambda: json.loads(http_get())["kind"] == 10021,
             60, "gateway advertisement")
    return client_mac()


@pytest.fixture()
def fresh_session(provisioned):
    """No-session start: deauth our MAC and clear in-memory sessions."""
    src_ip, mac = provisioned
    ssh_router(f"ndsctl deauth {mac} || true; /etc/init.d/tollgate-wrt restart")
    wait_for(lambda: usage(src_ip) is None, 60, "session reset")
    return provisioned


def test_discovery_parses_real_advertisement(provisioned):
    r = run_clientd("--list-offers")
    assert r.returncode == 0, r.stderr
    assert "metric=" in r.stdout
    assert "/step" in r.stdout and "mint=" in r.stdout


def test_gateway_arp_resolves_our_mac(provisioned):
    """The router's own ARP entry for our source IP must equal the MAC
    clientd sends — the resolution the docker lab fakes via dhcp.leases."""
    src_ip, mac = provisioned
    neigh = ssh_router(f"ip neigh | grep '{src_ip} ' || true")
    assert mac in neigh, f"router ARP for {src_ip} lacks {mac}: {neigh!r}"
    st = json.loads(run_clientd("--status", "--json").stdout)
    assert st["mac"] == mac


def test_payment_registers_mac_with_nds(fresh_session):
    """A real payment must leave our MAC Authenticated in NoDogSplash."""
    src_ip, mac = fresh_session
    proc = subprocess.Popen(
        ["python3", CLIENTD, "--gateway", GATEWAY, "--wallet-dir", WALLET_DIR,
         "--steps", "1", "--renew-below", "25MB", "--interval", "2"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        allotment = wait_for(lambda: allotment_of(src_ip) > 0,
                             120, "initial payment")
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    nds = json.loads(ssh_router("ndsctl json"))
    clients = nds.get("clients", {})
    assert mac in clients, f"{mac} not in ndsctl clients: {list(clients)}"
    assert clients[mac]["state"] == "Authenticated"
    assert allotment >= 1


def test_threshold_renewal_doubles_allotment(fresh_session):
    src_ip, _ = fresh_session
    proc = subprocess.Popen(
        ["python3", CLIENTD, "--gateway", GATEWAY, "--wallet-dir", WALLET_DIR,
         "--steps", "1", "--renew-below", "25MB", "--interval", "2"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_for(lambda: allotment_of(src_ip) > 0, 120, "initial payment")
        first = allotment_of(src_ip)
        wait_for(lambda: allotment_of(src_ip) >= 2 * first,
                 120, "threshold renewal")
        doubled = allotment_of(src_ip)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    assert doubled >= 2 * first
