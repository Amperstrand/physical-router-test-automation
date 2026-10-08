"""Unit tests for the artifact-wait fail-fast in lib/deploy.py.

Born from the 2026-09-27 PR-review marathon: two cloud-lab submits polled a
dead GitHub Actions lane for ~30 minutes each because the repo's build of
record had moved to ngit/Blossom and no kind-1063 artifact existed for the
PR head. The wait must fail fast when the lane is dead or dormant instead of
burning the full timeout.
"""

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from lib import deploy


pytestmark = [pytest.mark.story("P11")]

def _run(created: str | None = None) -> dict:
    out = {"databaseId": 1, "status": "completed", "conclusion": "success",
           "headBranch": "main", "headSha": "a" * 40}
    if created is not None:
        out["createdAt"] = created
    return out


def test_probe_never_ran():
    with mock.patch.object(deploy, "_list_workflow_runs", return_value=[]):
        reason = deploy._probe_workflow_lane_dead("org/repo", "Build")
    assert reason is not None
    assert "never run" in reason


def test_probe_dormant():
    old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    with mock.patch.object(deploy, "_list_workflow_runs",
                           return_value=[_run(old)]):
        reason = deploy._probe_workflow_lane_dead("org/repo", "Build")
    assert reason is not None
    assert "dormant" in reason


def test_probe_alive_recent_run():
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with mock.patch.object(deploy, "_list_workflow_runs",
                           return_value=[_run(fresh)]):
        assert deploy._probe_workflow_lane_dead("org/repo", "Build") is None


def test_probe_alive_unparseable_timestamp():
    with mock.patch.object(deploy, "_list_workflow_runs",
                           return_value=[_run("not-a-date")]):
        # Bad data must not produce a false dead verdict.
        assert deploy._probe_workflow_lane_dead("org/repo", "Build") is None


def test_ensure_artifact_fails_fast_on_dead_lane(monkeypatch):
    monkeypatch.setattr(deploy, "_resolve_blossom_binary", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "_list_workflow_runs", lambda *a, **k: [])
    monkeypatch.setattr(deploy.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError) as exc:
        deploy.ensure_artifact(
            branch="feat/x", arch="x86_64", repo="org/repo",
            workflow="Build", timeout_s=60, probe_after_polls=2,
        )
    assert "failed fast" in str(exc.value)
    assert "ngit/Blossom" in str(exc.value)


def test_ensure_artifact_keeps_waiting_on_alive_lane(monkeypatch):
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(deploy, "_resolve_blossom_binary", lambda *a, **k: None)
    # Branch-scoped polls are empty, repo-wide probe sees a fresh run.
    def runs(repo, workflow, *, branch=None, commit=None, limit=10):
        return [_run(fresh)] if branch is None and commit is None else []
    monkeypatch.setattr(deploy, "_list_workflow_runs", runs)
    monkeypatch.setattr(deploy.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError) as exc:
        deploy.ensure_artifact(
            branch="feat/x", arch="x86_64", repo="org/repo",
            workflow="Build", timeout_s=5, probe_after_polls=1,
        )
    # Must exhaust the timeout normally, not fail fast.
    assert "failed fast" not in str(exc.value)
