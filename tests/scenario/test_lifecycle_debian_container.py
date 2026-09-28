"""Migrated story: 'user pays and gets internet' as a scenario lifecycle.

BEFORE (hand-written per rail): tests/stories/test_user_pays_and_gets_
internet.py for the phone, plus the container payment rails wired inside
scripts/run-local-tests.sh — the same fund → connect → gate-closed →
pay → gate-open → session assertions duplicated per client.

AFTER: one profile (profiles/debian-container-real.yaml) names the four
driver roles; build_roles composes them and run_lifecycle executes the
canonical steps, writing result.json + timeline.jsonl evidence.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.scenario.adapters import register_prta_adapters
from tollgate_lab.scenarios.drivers import build_roles
from tollgate_lab.scenarios.lifecycle import run_lifecycle
from tollgate_lab.scenarios.profile import load_profile

pytestmark = [pytest.mark.api, pytest.mark.slow]

HERE = Path(__file__).parent
ROUTER = os.environ.get("TOLLGATE_SSH_HOST", "10.99.99.1")
CLIENT_MAC = os.environ.get("TOLLGATE_CLIENT_MAC", "de:54:4e:91:49:da")

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


def _router_ssh(cmd: str) -> str:
    proc = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            f"root@{ROUTER}",
            cmd,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    return proc.stdout


@pytest.fixture(scope="module", autouse=True)
def gate_reset():
    """Guarantee the client starts deauthenticated (gate closed)."""
    register_prta_adapters()
    from lib.router import remove_nds_auth_mark_rules

    _router_ssh(f"ndsctl deauth {CLIENT_MAC} 2>/dev/null || true")
    remove_nds_auth_mark_rules(_router_ssh, CLIENT_MAC)


def test_user_pays_and_gets_internet(tmp_path):
    profile = load_profile(HERE / "profiles" / "debian-container-real.yaml")
    roles = build_roles(profile)
    out = tmp_path / "scenario" / profile.name

    result = run_lifecycle(roles, profile, out)

    assert result.ok, [(s.name, s.status, s.error) for s in result.steps if s.status == "FAIL"]
    statuses = {s.name: s.status for s in result.steps}
    for step in CORE_STEPS:
        assert statuses.get(step) == "PASS", f"{step}: {statuses.get(step)!r}"
    assert (out / "result.json").is_file()
    assert (out / "timeline.jsonl").is_file()
