"""Migrated story (phone variant): the bench phone pays a physical
TollGate gateway through the TIP-03 portal, driven entirely by the
phone-adb profile. Rig-agnostic: point TOLLGATE_GW_BASE /
TOLLGATE_CLIENT_BASE / TOLLGATE_PORTAL at any gateway. Coordinates the
bench phone through the labgrid android-test mutex.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from urllib.parse import urlparse

import pytest

from tests.scenario.adapters import register_prta_adapters
from tollgate_lab.scenarios.drivers import build_roles
from tollgate_lab.scenarios.lifecycle import run_lifecycle
from tollgate_lab.scenarios.profile import load_profile

pytestmark = [pytest.mark.slow, pytest.mark.android_only]

HERE = Path(__file__).parent
GW_BASE = os.environ.get("TOLLGATE_GW_BASE", "http://192.168.105.51:2121")

CORE_STEPS = (
    "rig_up",
    "wallet_fresh",
    "actor_mints_token",
    "client_connects",
    "gate_closed_asserted",
    "payment_made",
    "gate_open_asserted",
    "session_asserted",
    "evidence_written",
)


def _gateway_reachable() -> bool:
    host = urlparse(GW_BASE).hostname or ""
    try:
        with socket.create_connection((host, 80), timeout=2):
            return True
    except OSError:
        pass
    try:
        with socket.create_connection((host, 2121), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module", autouse=True)
def registration_and_gate():
    register_prta_adapters()
    if not _gateway_reachable():
        pytest.skip(f"gateway rig not reachable at {GW_BASE} (bench powered down? stock switch off?)")


def test_phone_pays_portal_lifecycle(labgrid_phone_mutex, tmp_path):
    profile = load_profile(HERE / "profiles" / "phone-adb.yaml")
    roles = build_roles(profile)
    out = tmp_path / "scenario" / profile.name

    result = run_lifecycle(roles, profile, out)

    assert result.ok, [(s.name, s.status, s.error) for s in result.steps if s.status == "FAIL"]
    statuses = {s.name: s.status for s in result.steps}
    for step in CORE_STEPS:
        assert statuses.get(step) == "PASS", f"{step}: {statuses.get(step)!r}"
    assert (out / "result.json").is_file()
    assert (out / "timeline.jsonl").is_file()
