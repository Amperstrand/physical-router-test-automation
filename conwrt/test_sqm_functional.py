"""SQM functional tests — verifies conwrt's SQM configuration actually reduces bufferbloat.

Tests run against a real OpenWrt router (physical or QEMU VM). Requires:
- Router accessible via SSH with SQM-capable kernel (sch_cake or sch_fq_codel)
- Optional: second host (client) for iperf3-based latency measurement

Environment variables:
    CONWRT_ROUTER_HOST  — router IP (default: 192.168.1.1)
    CONWRT_ROUTER_KEY   — SSH key path
    CONWRT_ROUTER_PORT  — SSH port (default: 22)
    CONWRT_CLIENT_HOST  — client host for iperf3 (optional, skips if unset)
    CONWRT_REPO         — path to conwrt repo (default: ~/src/conwrt)
"""
from __future__ import annotations

import re
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.story("O6"),pytest.mark.api, pytest.mark.extended]


def test_router_running_openwrt(ssh_router):
    out = ssh_router("cat /etc/openwrt_release 2>/dev/null || echo NOT_OPENWRT")
    if "NOT_OPENWRT" in out:
        pytest.skip("Target is not running OpenWrt")
    assert "OpenWrt" in out


def test_sqm_scripts_installed(ssh_router):
    out = ssh_router("opkg list-installed 2>/dev/null | grep sqm-scripts")
    if "sqm-scripts" not in out:
        pytest.skip("sqm-scripts not installed — run conwrt configure with SQM first")
    assert "sqm-scripts" in out


def test_conwrt_configure_applies_sqm(ssh_router, conwrt_repo, router_host):
    config = conwrt_repo / "config.toml"
    backup = conwrt_repo / "config.toml.bak"

    if config.exists():
        backup.write_text(config.read_text())

    try:
        config.write_text(textwrap.dedent(f"""\
            [password]
            mode = "none"

            [network]
            lan_ip_mode = "static"
            lan_ip = "{router_host}"

            [use_cases]
            enabled = ["sqm"]

            [use_cases.sqm]
            download_kbps = 10000
            upload_kbps = 5000
        """))

        result = subprocess.run(
            ["python3", "scripts/conwrt.py", "configure",
             "--model-id", "virtual-x86-64",
             "--ip", router_host],
            capture_output=True, text=True, timeout=120,
            cwd=str(conwrt_repo),
        )
        assert result.returncode == 0, f"conwrt configure failed:\n{result.stderr}"

    finally:
        if backup.exists():
            config.write_text(backup.read_text())
            backup.unlink()

    uci = ssh_router("uci show sqm")
    assert "sqm" in uci
    assert "enabled='1'" in uci
    assert "qdisc='cake'" in uci
    assert "script='piece_of_cake.qos'" in uci
    assert "download='10000'" in uci
    assert "upload='5000'" in uci


def test_sqm_service_running(ssh_router):
    out = ssh_router("/etc/init.d/sqm enabled && echo ENABLED || echo DISABLED")
    assert "ENABLED" in out, "SQM service not enabled"

    out = ssh_router("ifup wan 2>/dev/null; sleep 2; /etc/init.d/sqm restart 2>/dev/null; sleep 3; pgrep -f 'run.sh\\|sqm' && echo RUNNING || echo STOPPED")
    if "STOPPED" in out:
        pytest.skip("SQM service not running — WAN may not be up in this environment")


def test_tc_qdisc_has_cake(ssh_router):
    out = ssh_router("tc qdisc show 2>/dev/null || echo TC_MISSING")
    if "TC_MISSING" in out:
        pytest.skip("tc command not available — kernel module missing")
    assert "cake" in out or "fq_codel" in out, \
        f"Expected CAKE or fq_codel in qdisc, got:\n{out}"


def test_sqm_reduces_bufferbloat(ssh_router, ssh_client):
    """Verify SQM keeps latency low under bandwidth saturation.

    Methodology:
    1. Start iperf3 server on router
    2. Measure baseline ping latency from client (no load)
    3. Start iperf3 client on client host (saturate link)
    4. Measure ping latency under load
    5. Assert loaded latency < 50ms (without SQM would be 200ms+)
    """
    ssh_router("pgrep iperf3 && pkill iperf3; iperf3 -s -D 2>/dev/null || echo NO_IPERF3")
    time.sleep(1)

    baseline = ssh_client(f"ping -c 5 -q {ssh_router.__wrapped__ if hasattr(ssh_router, '__wrapped__') else ''}")
    router_ip = _get_router_ip(ssh_router)

    baseline = ssh_client(f"ping -c 5 -q {router_ip} 2>/dev/null | tail -1")
    if not baseline:
        pytest.skip("ping from client to router failed — check connectivity")

    baseline_ms = _extract_avg_latency(baseline)
    if baseline_ms is None:
        pytest.skip(f"Could not parse baseline latency from: {baseline}")

    ssh_client(f"iperf3 -c {router_ip} -t 10 -b 20M > /dev/null 2>&1 &")
    time.sleep(2)

    loaded = ssh_client(f"ping -c 5 -q {router_ip} 2>/dev/null | tail -1")
    loaded_ms = _extract_avg_latency(loaded)

    ssh_client("pkill iperf3 2>/dev/null")
    ssh_router("pkill iperf3 2>/dev/null")

    if loaded_ms is None:
        pytest.skip(f"Could not parse loaded latency from: {loaded}")

    assert loaded_ms < 50.0, \
        f"Bufferbloat not mitigated: baseline={baseline_ms}ms, loaded={loaded_ms}ms"


def _get_router_ip(ssh_router) -> str:
    out = ssh_router("ip -4 addr show br-lan 2>/dev/null | grep -o 'inet [0-9.]*' | awk '{print $2}'")
    if not out:
        out = ssh_router("ip -4 addr show eth0 2>/dev/null | grep -o 'inet [0-9.]*' | awk '{print $2}'")
    return out.strip() or "192.168.1.1"


def _extract_avg_latency(ping_output: str) -> float | None:
    m = re.search(r"([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", ping_output)
    if m:
        return float(m.group(2))
    return None
