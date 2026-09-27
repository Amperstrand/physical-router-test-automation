"""End-to-end tests: tollgate-installer wizard installs TollGate on a fresh OpenWrt VM.

Proven flow (2026-09-19 spike): the ARP-based /api/scan discovers the fresh
QEMU router, POST /api/deploy completes all 12 steps (~90 s), and the router
comes up branded tollgate-XXXX with tollgate-wrt from the FreedomTechFeed.
"""
from __future__ import annotations

import json
import re

import pytest

from lab_vm import VM_IP

LN_ADDRESS = "tollgate@minibits.cash"

pytestmark = [pytest.mark.installer, pytest.mark.virtual_lab, pytest.mark.slow]


@pytest.mark.timeout(240)
def test_scan_discovers_fresh_router(installer_service):
    scan = installer_service.get_json("/api/scan", timeout=180)
    routers = scan.get("routers", [])
    match = [r for r in routers if r.get("ip") == VM_IP]
    assert match, (
        f"fresh VM {VM_IP} not in scan results; "
        f"discovered: {[r.get('ip') for r in routers]}"
    )
    info = match[0]
    assert info.get("ssh_open") is True, f"SSH not detected open: {info}"
    assert "OpenWrt" in info.get("firmware", ""), f"firmware not probed: {info}"
    assert "24.10" in info.get("firmware", ""), f"unexpected firmware: {info}"


@pytest.mark.timeout(600)
def test_deploy_completes_all_steps(installer_service, installer_lab):
    status = installer_service.deploy(VM_IP, installer_lab.password, LN_ADDRESS,
                                      mode="wan", timeout=480)
    assert status.get("status") == "done", f"deploy not done: {json.dumps(status)[:800]}"
    steps = status.get("steps", [])
    assert len(steps) == 12, f"expected 12 deploy steps, got {len(steps)}"
    # "warn" is a completed step with a warning (e.g. install when the feed
    # offers no independently-anchored digest); only "failed" fails the run.
    bad = [s.get("name") for s in steps if s.get("status") not in ("done", "warn")]
    assert not bad, f"steps not done/warn: {bad}; full: {json.dumps(steps)[:800]}"
    install = next(s for s in steps if s.get("name") == "install")
    assert "tollgate-wrt" in install.get("detail", ""), (
        f"install step detail missing tollgate-wrt: {install}"
    )


@pytest.mark.timeout(180)
def test_router_branded_and_healthy(installer_lab):
    host = installer_lab.ssh("uci get system.@system[0].hostname").strip()
    assert re.fullmatch(r"[tT]ollgate-[A-Z0-9]{4}", host), f"hostname not branded: {host!r}"

    pkgs = installer_lab.ssh("opkg list-installed 2>/dev/null | grep tollgate || true")
    assert "tollgate-wrt" in pkgs, f"tollgate-wrt not installed: {pkgs!r}"

    ports = installer_lab.ssh("netstat -tln 2>/dev/null | grep -E ':(2050|2121) ' || true")
    assert ":2050" in ports and ":2121" in ports, f"portal/backend ports down: {ports!r}"

    health = installer_lab.ssh("wget -qO- --timeout=5 http://127.0.0.1:2121/ 2>&1 || true")
    assert '"kind":10021' in health.replace(" ", ""), f"health ad missing: {health[:200]}"

    dns = installer_lab.ssh("nslookup tollgate.lan 127.0.0.1 2>&1 || true")
    assert "Address" in dns, f"tollgate.lan DNS failed: {dns[:200]}"

    ident = installer_lab.ssh("cat /etc/tollgate/identities.json 2>/dev/null || true")
    assert LN_ADDRESS in ident, f"LN address not configured: {ident[:200]}"
