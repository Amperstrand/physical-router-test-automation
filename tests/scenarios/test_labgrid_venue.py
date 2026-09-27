"""Labgrid venue scenario on real hardware (Phase 3 of poe-venue-roadmap).

Opt-in: ``TOLLGATE_VENUE=labgrid`` AND the gitignored inventory carries a
``place:`` mapping for the selected router (``TOLLGATE_LABGRID_PLACE``,
default ``ap-lan2``).

Proves the full venue path end-to-end: the ``router`` fixture acquires the
place on the shared coordinator (per-DUT lock per the 2026-09-22 locking
model), exports ``TOLLGATE_SSH_HOST/_KEY/_JUMP_HOST`` from the inventory,
and the ``Router`` reaches the DUT over SSH through the jump host; power
flows through the place's NetworkPowerPort — the exporter's conwrt_poe
backend post-verifies every manage underneath, so failures surface loudly
and are never retried blind. Uptime reset + unchanged firmware identity
prove a real cold cycle; the session fixture releases the place on teardown
(an orphaned place lock blocks every other session — seen on the bench).

Physical venue: ``can_publish`` must stay False; results stay in gitignored
``results/`` only.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from lib.lab_inventory import inventory_exists, load_inventory
from lib.router_lock import RouterLock
from lib.labgrid_venue import bench_for_router

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.physical_only,
    pytest.mark.skipif(
        os.environ.get("TOLLGATE_VENUE") != "labgrid" or not inventory_exists(),
        reason=(
            "set TOLLGATE_VENUE=labgrid and fill configs/labgrid/inventory.local.yaml "
            "(place/keyfile/jump_host per router; schema in inventory.local.yaml.example)"
        ),
    ),
]


@pytest.fixture
def venue_lab():
    inv = load_inventory()
    name = os.environ.get("TOLLGATE_LABGRID_PLACE", "ap-lan2")
    binding = bench_for_router(inv, name)
    lock = RouterLock()
    lock.acquire(router_id=name, phase="labgrid-venue-smoke")
    try:
        yield binding.bench, binding.entry
    finally:
        lock.release()


def _wait_ssh(router, timeout_s: float = 240.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if router.ssh_bool("true", timeout=10):
                return
        except Exception:
            pass
        time.sleep(5)
    pytest.fail(f"router SSH not ready within {timeout_s:.0f}s after venue power-on")


def test_venue_place_reaches_dut(router, venue_lab):
    _bench, entry = venue_lab
    assert router is not None, "venue must export TOLLGATE_SSH_HOST from the inventory"
    assert router.host == entry.address
    release = router.ssh("cat /etc/openwrt_release")
    assert "DISTRIB_RELEASE" in release, f"DUT did not serve its release: {release[:80]}"


class TestVenuePowerCycle:
    @pytest.mark.timeout(600)
    def test_cold_cycle_through_place(self, router, venue_lab):
        bench, _entry = venue_lab
        assert router is not None

        _wait_ssh(router)
        pre_release = router.ssh("cat /etc/openwrt_release")

        bench.power(False)
        time.sleep(8)  # min-off: PD renegotiation headroom (BCM59121), as the direct controller enforces
        bench.power(True)

        _wait_ssh(router)

        post_uptime = int(router.ssh("cut -d. -f1 /proc/uptime"))
        assert post_uptime < 300, (
            f"uptime {post_uptime}s after venue cold cycle — DUT did not cold boot"
        )
        post_release = router.ssh("cat /etc/openwrt_release")
        assert post_release == pre_release, "firmware identity changed across cycle"
