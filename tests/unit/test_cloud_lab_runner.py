"""Unit tests for declarative cloud lab runner."""

from __future__ import annotations

from lib.cloud_lab.worker.config import WorkerConfig
from lib.cloud_lab.worker.runner import (
    RunnerSpec,
    _aggregate_exit,
    _build_pytest_cmd,
    build_runners,
    pytest_collect_args,
    runner_scope,
)


import pytest

pytestmark = [pytest.mark.story("P11")]

def _base_config(**overrides) -> WorkerConfig:
    defaults = dict(
        run_id="test-run",
        sut_branch="main",
        sut_commit="abc1234",
        sut_pr="104",
        artifact_run_id="12345",
        artifact_repo="OpenTollGate/tollgate-module-basic-go",
        pr_repo="OpenTollGate/tollgate-module-basic-go",
        suite_ref="deadbeef",
        backend="go",
        reseller_scenarios=False,
        two_router=False,
        secondary_router_host="",
        secondary_router_port="",
        keep_vm_on_failure=True,
        publish=False,
        project="test",
        zone="us-central1-a",
        vm_name="test-vm",
        gh_token="token",
        mint="auto",
        portal="builtin",
        quick=False,
        hwsim_enabled=False,
        vwifi_enabled=False,
        smoke=False,
        complete=False,
        wifi_plane="tap",
        lease_minutes=60,
    )
    defaults.update(overrides)
    return WorkerConfig(**defaults)


def test_build_runners_quick():
    runners = build_runners(_base_config(quick=True))
    assert len(runners) == 1
    assert runners[0].name == "visual"


def test_build_runners_smoke_includes_hwsim_when_enabled():
    runners = build_runners(_base_config(smoke=True, hwsim_enabled=True))
    names = [r.name for r in runners]
    assert names == ["visual", "smoke-api", "hwsim"]


def test_build_runners_full_default():
    runners = build_runners(_base_config())
    names = [r.name for r in runners]
    assert "visual" in names
    assert "api" in names
    assert "vl-scenarios" in names
    assert "scenarios" not in names
    assert "two-router" not in names


def test_build_runners_full_with_optional_suites():
    cfg = _base_config(reseller_scenarios=True, two_router=True, wifi_plane="hwsim-netns")
    names = [r.name for r in build_runners(cfg)]
    assert "scenarios" in names
    assert "two-router" in names
    assert "virtual-wifi" in names


def test_vl_scenarios_paths_cover_scenario_files():
    runners = build_runners(_base_config())
    vl = next(r for r in runners if r.name == "vl-scenarios")
    assert len(vl.paths) == 5
    assert all(p.startswith("tests/scenarios/") for p in vl.paths)


def test_pytest_collect_args_matches_runners():
    cfg = _base_config(smoke=True, hwsim_enabled=True)
    args = pytest_collect_args(cfg)
    assert "visual=raw/visual/junit.xml" in args
    assert "smoke-api=raw/smoke-api/junit.xml" in args
    assert "hwsim=raw/hwsim/junit.xml" in args


def test_runner_scope():
    assert runner_scope(_base_config(quick=True)) == "quick"
    assert runner_scope(_base_config(smoke=True)) == "smoke"
    assert runner_scope(_base_config()) == "full"


def test_aggregate_exit_worst_of():
    assert _aggregate_exit({"visual": 0, "api": 1}) == 1
    assert _aggregate_exit({"visual": 0, "api": 0}) == 0
    assert _aggregate_exit({"a": 2, "b": 1}) == 2


def test_runner_spec_junit_rel():
    spec = RunnerSpec(name="api", paths=("tests/api/",))
    assert spec.junit_rel() == "raw/api/junit.xml"


def test_build_pytest_cmd_activates_venv():
    cfg = _base_config(smoke=True)
    spec = build_runners(cfg)[0]
    cmd = _build_pytest_cmd(cfg, spec, "/tmp/results")
    assert "source /opt/tollgate-venv/bin/activate" in cmd
    assert "pytest" in cmd
