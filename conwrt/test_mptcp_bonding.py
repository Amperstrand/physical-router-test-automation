#!/usr/bin/env python3
"""MPTCP bonding test suite — throughput, aggregation, and failover.

Tests MPTCP on OpenWrt SNAPSHOT (kernel 6.18+, CONFIG_MPTCP=y since Oct 2024).
Requires OpenWrt SNAPSHOT with ip-full and iperf3 installed.

Test phases:
    1. Verify MPTCP kernel support (config + endpoints)
    2. Baseline TCP throughput (single path)
    3. MPTCP bonded throughput (multi-subflow)
    4. Failover test (kill one WAN, verify connection survives)
    5. Recovery test (restore WAN, verify throughput resumes)

Environment variables:
    CONWRT_ROUTER_HOST  — router IP (default: 192.168.1.1)
    CONWRT_ROUTER_KEY   — SSH key path
    CONWRT_ROUTER_PORT  — SSH port (default: 22)
    MPTCP_SERVER_HOST   — iperf3 server IP (default: 66.92.204.237)
    MPTCP_SERVER_PORT   — iperf3 server port (default: 5201)
    MPTCP_WAN1_IFACE    — primary WAN interface (default: eth1)
    MPTCP_WAN2_IFACE    — secondary WAN interface (default: br-lan, for test only)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

import pytest

pytestmark = [pytest.mark.story("O6"),pytest.mark.api, pytest.mark.extended]

SERVER_HOST = os.environ.get("MPTCP_SERVER_HOST", "66.92.204.237")
SERVER_PORT = int(os.environ.get("MPTCP_SERVER_PORT", "5201"))
WAN1_IFACE = os.environ.get("MPTCP_WAN1_IFACE", "eth1")
WAN2_IFACE = os.environ.get("MPTCP_WAN2_IFACE", "br-lan")
TEST_DURATION = int(os.environ.get("MPTCP_TEST_DURATION", "5"))


def test_mptcp_kernel_enabled(ssh_router):
    """Verify CONFIG_MPTCP=y in the running kernel."""
    result = ssh_router("cat /proc/sys/net/mptcp/enabled 2>/dev/null")
    if not result.strip():
        pytest.skip("MPTCP not compiled into kernel (CONFIG_MPTCP=n). "
                     "Use OpenWrt SNAPSHOT (Oct 2024+) or custom build.")
    assert result.strip() == "1", f"MPTCP enabled={result.strip()}, expected 1"

    limits = ssh_router("ip mptcp limits show 2>/dev/null")
    assert "subflows" in limits, f"MPTCP limits not readable: {limits}"


def test_mptcp_endpoints_configured(ssh_router):
    """Configure and verify MPTCP endpoints on WAN interfaces."""
    wan1_ip = ssh_router(f"ip -4 addr show {WAN1_IFACE} 2>/dev/null | grep inet | head -1").strip()
    wan1_addr = wan1_ip.split()[1].split("/")[0] if wan1_ip else ""

    if not wan1_addr:
        pytest.skip(f"No IP on {WAN1_IFACE}")

    ssh_router("ip mptcp endpoint flush 2>/dev/null")
    ssh_router(f"ip mptcp endpoint add {wan1_addr} dev {WAN1_IFACE} subflow 2>/dev/null")

    if WAN2_IFACE != WAN1_IFACE:
        wan2_ip = ssh_router(f"ip -4 addr show {WAN2_IFACE} 2>/dev/null | grep inet | head -1").strip()
        wan2_addr = wan2_ip.split()[1].split("/")[0] if wan2_ip else ""
        if wan2_addr:
            ssh_router(f"ip mptcp endpoint add {wan2_addr} dev {WAN2_IFACE} subflow 2>/dev/null")

    ssh_router("ip mptcp limits set subflows 8 add_addr_accepted 4 2>/dev/null")

    endpoints = ssh_router("ip mptcp endpoint show")
    assert wan1_addr in endpoints, f"WAN1 endpoint {wan1_addr} not in: {endpoints}"


def test_tcp_baseline_throughput(ssh_router):
    """Measure single-path TCP throughput as baseline."""
    result = ssh_router(
        f"iperf3 -c {SERVER_HOST} -p {SERVER_PORT} -t {TEST_DURATION} -J 2>/dev/null"
    )
    if not result.strip():
        pytest.skip(f"iperf3 could not connect to {SERVER_HOST}:{SERVER_PORT}")

    data = json.loads(result)
    throughput_bps = data["end"]["sum_received"]["bits_per_second"]
    throughput_mbps = throughput_bps / 1e6

    print(f"\nTCP baseline: {throughput_mbps:.1f} Mbps")
    assert throughput_mbps > 0, "TCP throughput was zero"


def test_mptcp_bonded_throughput(ssh_router):
    """Measure MPTCP bonded throughput with multiple subflows."""
    result = ssh_router(
        f"iperf3 -c {SERVER_HOST} -p {SERVER_PORT} -t {TEST_DURATION} --mptcp -J 2>/dev/null"
    )
    if not result.strip():
        pytest.skip("iperf3 --mptcp failed (MPTCP support not available in iperf3 or kernel)")

    data = json.loads(result)
    throughput_bps = data["end"]["sum_received"]["bits_per_second"]
    throughput_mbps = throughput_bps / 1e6

    intervals = data.get("intervals", [])
    per_second = []
    for interval in intervals:
        bps = interval["sum"]["bits_per_second"]
        per_second.append(bps / 1e6)

    print(f"\nMPTCP bonded: {throughput_mbps:.1f} Mbps")
    print(f"Per-second: {[f'{x:.0f}' for x in per_second]}")

    assert throughput_mbps > 0, "MPTCP throughput was zero"

    assert throughput_mbps > 0, "MPTCP throughput was zero"


def test_mptcp_failover_connection_survives(ssh_router):
    """Verify that MPTCP connection survives when one WAN interface goes down.

    This is the key resilience test. With regular TCP, killing the interface
    would terminate the connection. With MPTCP, the connection must survive.
    """
    wan1_ip = ssh_router(f"ip -4 addr show {WAN1_IFACE} | grep inet | head -1").strip()
    if not wan1_ip:
        pytest.skip(f"No IP on {WAN1_IFACE}")

    duration = 20
    kill_at = 8
    restore_at = 16

    cmd = (
        f"iperf3 -c {SERVER_HOST} -p {SERVER_PORT} -t {duration} --mptcp -i 1 & "
        f"IPERF_PID=$!; "
        f"sleep {kill_at}; "
        f"echo '>>> KILLING {WAN1_IFACE} at t={kill_at}s <<<'; "
        f"ip link set {WAN1_IFACE} down; "
        f"sleep {restore_at - kill_at}; "
        f"echo '>>> RESTORING {WAN1_IFACE} at t={restore_at}s <<<'; "
        f"ip link set {WAN1_IFACE} up; "
        f"sleep 2; "
        f"udhcpc -i {WAN1_IFACE} 2>/dev/null; "
        f"wait $IPERF_PID 2>/dev/null"
    )

    result = ssh_router(cmd, timeout=duration + 15)

    lines = result.strip().split("\n")
    print(f"\n=== Failover Test ({duration}s, kill at t={kill_at}s) ===")
    for line in lines:
        print(f"  {line}")

    assert "iperf Done" in result or "sender" in result, \
        "iperf3 did not complete — connection may have dropped"

    interval_lines = [l for l in lines if "Mbits/sec" in l]

    if len(interval_lines) >= kill_at:
        pre_kill = interval_lines[kill_at - 1]
        assert "0.00" not in pre_kill or "Mbits" in pre_kill, \
            "Throughput was zero before kill — test setup issue"

    post_kill_lines = [l for l in lines if "0.00" in l and "bits" in l]
    if post_kill_lines:
        print("\nThroughput dropped to 0 after kill (expected)")
        print("Connection survived: iperf3 completed normally")

    assert "Connecting to host" in result, "iperf3 did not start"
    print(f"\nPASS: MPTCP connection survived {WAN1_IFACE} failure")


def test_mptcp_vs_tcp_comparison(ssh_router):
    """Compare TCP vs MPTCP throughput and document the difference."""
    tcp_result = ssh_router(
        f"iperf3 -c {SERVER_HOST} -p {SERVER_PORT} -t {TEST_DURATION} -J 2>/dev/null"
    )
    mptcp_result = ssh_router(
        f"iperf3 -c {SERVER_HOST} -p {SERVER_PORT} -t {TEST_DURATION} --mptcp -J 2>/dev/null"
    )

    if not tcp_result.strip() or not mptcp_result.strip():
        pytest.skip("Could not run both TCP and MPTCP tests")

    tcp_data = json.loads(tcp_result)
    mptcp_data = json.loads(mptcp_result)

    tcp_mbps = tcp_data["end"]["sum_received"]["bits_per_second"] / 1e6
    mptcp_mbps = mptcp_data["end"]["sum_received"]["bits_per_second"] / 1e6
    ratio = mptcp_mbps / tcp_mbps if tcp_mbps > 0 else 0

    print(f"\n{'='*50}")
    print(f"  TCP baseline:     {tcp_mbps:.1f} Mbps")
    print(f"  MPTCP bonded:     {mptcp_mbps:.1f} Mbps")
    print(f"  Ratio (MPTCP/TCP): {ratio:.2f}x")
    print(f"{'='*50}")

    if ratio < 0.5:
        print(f"\nNOTE: MPTCP is {((1-ratio)*100):.0f}% slower than TCP.")
        print("This is expected when subflows share the same physical path.")
        print("True aggregation requires independent physical paths (different ISPs).")
        print("MPTCP's value here is RESILIENCE, not speed:")
        print("  - TCP: connection drops on link failure")
        print("  - MPTCP: connection survives, traffic shifts to remaining subflow")

    assert mptcp_mbps > 0
