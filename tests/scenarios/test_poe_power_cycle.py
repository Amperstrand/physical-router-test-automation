"""Gated PoE power-control smoke against the physical lab (NR7101 on lan2).

Opt-in: runs only when ``TOLLGATE_POE_SMOKE=1`` AND the gitignored
inventory exists (copy ``configs/labgrid/inventory.local.yaml.example``).
Two layers:

1. ``test_disable_enable_only_touches_target_port`` — verifies the
   tollgate_lab PoePowerController against real hardware: disable ->
   DISABLED, enable -> leaves DISABLED, every OTHER port untouched.
   Works with no PD attached (Searching is a correct admin-on) and is
   the only part that passes while the device is physically absent.

2. ``test_cold_cycle_reboots_device`` — full cold cycle: pre-reboot
   snapshot, PoE cycle, assert DELIVERING, wait for SSH, verify uptime
   reset (proves cold boot, not SSH reboot), post-boot snapshot.
   FAILS LOUDLY if the device is absent: SEARCHING at deadline means
   cable unplugged, dead PD, or PoE negotiation failure.

Locking (per-resource model, 2026-09-22): per-DUT work locks only the
resource it touches — the PRTA-venue flock (prta-poe-bench) serializes
this suite's own scenarios, RouterLock holds the gate router. The GLOBAL
amperstrand-bench flock is reserved for switch-wide mutations (VLAN
changes, reboots, daemon restarts) and is never needed for per-port
power control.

Privacy: physical venue — ``can_publish`` must be False; results stay in
gitignored ``results/`` only.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from lib.lab_inventory import inventory_exists, load_inventory, poe_controller_config
from lib.router_lock import RouterLock
from tollgate_lab.hardware.bench_lock import BenchLockHeldError, acquire_bench_lock
from tollgate_lab.hardware.poe import PoePowerController, PoeStatus

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GATE_ROUTER = os.environ.get("TOLLGATE_POE_GATE", "ap-lan2")
SMOKE_ENV = "TOLLGATE_POE_SMOKE"

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.physical_only,
    pytest.mark.skipif(
        os.environ.get(SMOKE_ENV) != "1" or not inventory_exists(),
        reason=(
            f"set {SMOKE_ENV}=1 and create configs/labgrid/inventory.local.yaml "
            "(schema in inventory.local.yaml.example)"
        ),
    ),
]


def _router_ssh(keyfile: str | None, address: str, cmd: str, timeout: float = 20.0) -> str:
    args = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
    ]
    if keyfile:
        args += ["-i", os.path.expanduser(keyfile), "-o", "IdentitiesOnly=yes"]
    args += [f"root@{address}", cmd]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"router ssh rc={proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def _wait_ssh(keyfile: str | None, address: str, timeout_s: float = 240.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_err = ""
    while time.monotonic() < deadline:
        try:
            _router_ssh(keyfile, address, "true", timeout=10.0)
            return
        except (AssertionError, subprocess.TimeoutExpired) as e:
            last_err = str(e)[:120]
            time.sleep(5)
    pytest.fail(f"router SSH not ready within {timeout_s:.0f}s after power-on ({last_err})")


@pytest.fixture
def poe_lab():
    inv = load_inventory()
    router = inv.router(GATE_ROUTER)
    try:
        bench = acquire_bench_lock(
            name="prta-poe-bench",
            project="physical-router-test-automation",
            cwd=str(REPO_ROOT),
        )
    except BenchLockHeldError as e:
        pytest.skip(f"bench busy: {e}")
    with bench:
        lock = RouterLock()
        lock.acquire(router_id=GATE_ROUTER, phase="poe-power-cycle-smoke")
        try:
            yield PoePowerController(poe_controller_config(inv)), inv, router
        finally:
            lock.release()


def test_physical_provider_never_publishes():
    """Physical venue must never publish — enforced by the provider gate."""
    provider = REPO_ROOT / "scripts" / "provider.py"
    proc = subprocess.run(
        ["python3", str(provider), "can-publish", "-p", "physical"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode != 0, "physical provider MUST NOT publish (can_publish=False)"


class TestPoeAdminControl:
    @pytest.mark.timeout(180)
    def test_disable_enable_only_touches_target_port(self, poe_lab):
        ctl, _inv, router = poe_lab
        port = router.poe_port

        before = ctl.info()
        assert port in before.ports, f"{port} missing from switch PoE ports"

        try:
            off = ctl.off(port)
            assert off.status is PoeStatus.DISABLED

            time.sleep(2)  # BCM59121 renegotiation headroom on re-enable
            on = ctl.on(port)
            assert on.status is not PoeStatus.DISABLED
        finally:
            with contextlib.suppress(Exception):
                ctl.on(port)

        after = ctl.info()
        for other, entry in after.ports.items():
            if other == port:
                continue
            assert entry.status is before.ports[other].status, (
                f"port {other} changed {before.ports[other].status.name} -> "
                f"{entry.status.name}: power control leaked outside {port}"
            )


class TestPoeColdCycle:
    @pytest.mark.timeout(600)
    def test_cold_cycle_reboots_device(self, poe_lab, tmp_path):
        ctl, inv, router = poe_lab
        port = router.poe_port
        keyfile = inv.switch.keyfile

        if not router.address:
            pytest.fail(
                f"inventory router {GATE_ROUTER}.address is empty — power the "
                "device once, discover its address, and fill in "
                "configs/labgrid/inventory.local.yaml"
            )

        ctl.assert_delivering(port, timeout_s=30)

        # A preceding admin-control run (off/on) leaves the DUT mid-boot:
        # PoE Delivering only proves the port, not userspace readiness.
        _wait_ssh(keyfile, router.address)
        pre_release = _router_ssh(keyfile, router.address, "cat /etc/openwrt_release")
        (tmp_path / "pre-reboot.log").write_text(
            _router_ssh(keyfile, router.address, "logread | tail -40")
        )

        ctl.cycle(port, min_off_s=8.0, timeout_s=30)
        ctl.assert_delivering(port, timeout_s=90)

        _wait_ssh(keyfile, router.address)

        post_uptime = int(_router_ssh(keyfile, router.address, "cut -d. -f1 /proc/uptime"))
        assert post_uptime < 300, f"uptime {post_uptime}s after cold cycle — device did not cold boot"

        post_release = _router_ssh(keyfile, router.address, "cat /etc/openwrt_release")
        assert post_release == pre_release, "firmware identity changed across cycle"
        (tmp_path / "post-boot.log").write_text(
            _router_ssh(keyfile, router.address, "logread | tail -40")
        )
