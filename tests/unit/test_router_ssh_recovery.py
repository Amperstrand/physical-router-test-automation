import subprocess

import pytest

from lib.router import Router


@pytest.fixture
def unit_router(monkeypatch):
    monkeypatch.setenv("TOLLGATE_SSH_PASSWORD", "unit-test-pw")
    return Router("10.99.99.1", "10.99.99.100", "de:54:4e:91:49:da", "")


class FakeRun:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


def _ok(stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_ssh_recovers_after_master_teardown(unit_router, monkeypatch):
    fake = FakeRun([
        subprocess.TimeoutExpired(cmd="ssh", timeout=10),
        _ok(),
        _ok(stdout="recovered\n"),
    ])
    monkeypatch.setattr("lib.router.subprocess.run", fake)
    assert unit_router.ssh("echo hi") == "recovered"
    assert len(fake.calls) == 3
    assert "-O" in fake.calls[1] and "exit" in fake.calls[1]
    assert "echo hi" in fake.calls[2]


def test_ssh_raises_when_retry_also_times_out(unit_router, monkeypatch):
    fake = FakeRun([
        subprocess.TimeoutExpired(cmd="ssh", timeout=10),
        _ok(),
        subprocess.TimeoutExpired(cmd="ssh", timeout=10),
    ])
    monkeypatch.setattr("lib.router.subprocess.run", fake)
    with pytest.raises(subprocess.TimeoutExpired):
        unit_router.ssh("echo hi")
    assert len(fake.calls) == 3


def test_ssh_stdin_recovers_after_master_teardown(unit_router, monkeypatch):
    fake = FakeRun([
        subprocess.TimeoutExpired(cmd="ssh", timeout=10),
        _ok(),
        _ok(stdout="written"),
    ])
    monkeypatch.setattr("lib.router.subprocess.run", fake)
    result = unit_router.ssh_stdin("cat > /tmp/x", "payload")
    assert result.returncode == 0
    assert len(fake.calls) == 3
    assert "-O" in fake.calls[1]


def test_ssh_no_teardown_on_success(unit_router, monkeypatch):
    fake = FakeRun([_ok(stdout="fine")])
    monkeypatch.setattr("lib.router.subprocess.run", fake)
    assert unit_router.ssh("echo fine") == "fine"
    assert len(fake.calls) == 1
