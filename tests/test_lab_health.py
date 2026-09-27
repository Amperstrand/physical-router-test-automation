"""Labgrid deployment health test — asserts the bench deployment is UP.

Covers the deployment stack bottom-up:
  1. coordinator (ai-legion:20408) reachable, core places present
  2. exporters live (ai-legion bench power exports + ai-legion-small serial)
  3. bench infrastructure intact (ap-lan2 serial-bridge hold)
  4. PoE control round-trip on the SAFE EMPTY port lan7 via ZyxelPoEDriver
     (exercises manage + poll-verify + wedge detection end-to-end)
  5. stock GS1900-8HP #2 management up (SSH + web)
  6. O-series soak (bench_watch) alive

Run:
  TOLLGATE_SSH_HOST= ROUTER_IP= TOLLGATE_VIRTUAL_LAB= \
  ~/venvs/rig-labgrid/bin/python -m pytest \
    --lg-env=configs/labgrid/lab-health.yaml --no-deploy \
    tests/test_lab_health.py -v
"""

import os
import socket
import subprocess
import sys

import pytest

import tollgate_lab.drivers.zyxel_poe  # noqa: F401  registers driver+resource
from labgrid.protocol.powerprotocol import PowerProtocol

pytestmark = [pytest.mark.timeout(300), pytest.mark.smoke]

COORDINATOR = os.environ.get("BENCH_COORDINATOR", "192.168.13.208:20408")
STOCK_HOSTS = ["192.168.13.106", "192.168.13.3"]

CORE_PLACES = {
    "ap-lan2", "ap-lan3", "ap-lan4", "ap-lan5", "ap-lan6", "ap-lan7",
    "ap-lan8", "gs108t-lan8", "bolty-rig", "microfips-bench",
}
CORE_RESOURCES = (
    "ai-legion/ap-lan2/NetworkPowerPort",
    "ai-legion/ap-lan5/NetworkPowerPort",
    "ai-legion-small/m5stick-serial/NetworkSerialPort",
)


def lg(*args: str) -> str:
    client = os.path.join(os.path.dirname(sys.executable), "labgrid-client")
    return subprocess.run(
        [client, "-x", COORDINATOR, *args],
        capture_output=True, text=True, timeout=30, check=True).stdout


def test_coordinator_up_and_core_places():
    listed = {line.split()[0] for line in lg("places").splitlines()
              if line.split()}
    missing = CORE_PLACES - listed
    assert not missing, f"coordinator missing core places: {sorted(missing)}"


def test_exporters_live():
    resources = lg("resources")
    for res in CORE_RESOURCES:
        assert res in resources, f"exporter resource missing: {res}"


def test_bench_infrastructure_hold():
    who = lg("who")
    assert "ap-lan2" in who, (
        "ap-lan2 serial-bridge infrastructure hold is gone — "
        "anyone could acquire and power-cycle the reference unit")


def test_poe_control_roundtrip_safe_port(target):
    power = target.get_driver(PowerProtocol)
    baseline = power.get_status()
    assert baseline.lower() in ("searching", "delivering power"), baseline

    power.off()
    assert power.get_status().lower() in ("disabled", "off", ""), (
        "empty port did not verify disabled after off()")
    power.on()
    assert power.get() is True, "empty port did not verify enabled after on()"


def test_stock_switch_management_up():
    up = []
    for host in STOCK_HOSTS:
        for port in (22, 80):
            try:
                with socket.create_connection((host, port), timeout=4):
                    up.append(f"{host}:{port}")
            except OSError:
                pass
    assert any(":80" in u for u in up), (
        f"stock switch web UI unreachable on {STOCK_HOSTS} (got: {up})")
    assert any(":22" in u for u in up), (
        f"stock switch SSH unreachable on {STOCK_HOSTS} (got: {up})")


def test_bench_soak_alive():
    r = subprocess.run(["pgrep", "-fc", "bench_watch.py"],
                       capture_output=True, text=True)
    assert int(r.stdout.strip() or 0) >= 1, (
        "bench_watch soak is not running "
        "(restart: cd ~/src/conwrt-bench && setsid nohup python3 "
        "scripts/bench_watch.py --config data/bench/watch.json "
        ">> data/bench/watch/soak-host.log 2>&1 &)")
