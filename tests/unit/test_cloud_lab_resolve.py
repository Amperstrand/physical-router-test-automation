"""Unit tests for cloud lab target resolution."""

import pytest

from lib.cloud_lab.resolve import RunTarget, resolve_target


pytestmark = [pytest.mark.story("P11")]

def test_resolve_branch_go():
    target = resolve_target(branch="main", backend="go")
    assert target.branch == "main"
    assert target.repo == "OpenTollGate/tollgate-module-basic-go"
    assert target.pr is None
    assert target.backend == "go"


def test_resolve_branch_rust():
    target = resolve_target(branch="main", backend="rust")
    assert target.repo == "Amperstrand/tollgate-rs-ai-research-and-experiments"
    assert target.workflow == "Build and Package"


def test_resolve_requires_exactly_one_ref():
    with pytest.raises(ValueError):
        resolve_target(pr="1", branch="main")
    with pytest.raises(ValueError):
        resolve_target()


def test_run_target_workflow_go():
    target = RunTarget(
        repo="OpenTollGate/tollgate-module-basic-go",
        branch="feat/x",
        sut_commit="abc123",
        pr="42",
        backend="go",
    )
    assert target.workflow == "Build and Publish"
