"""Labgrid-mode M5Mint: port parsing, acquire/release, console transport.

The hardware is faked entirely — subprocess ssh/labgrid-client calls are
monkeypatched, so these tests pin the integration contract without a
coordinator, exporter, or stick.
"""
import subprocess

import pytest

import lib.m5 as m5
from lib.m5 import M5Mint, M5MintUnavailable


class FakeProc:
    def __init__(self, responses: list[bytes]):
        self._responses = list(responses)
        self.stdin_writes = []
        self.stdin = self
        self.stdout = self
        self._rc = None

    def write(self, data):
        self.stdin_writes.append(data)

    def flush(self):
        pass

    def fileno(self):
        return 0

    def poll(self):
        return self._rc

    def terminate(self):
        self._rc = 0

    def wait(self, timeout=None):
        return self._rc

    def read_chunk(self):
        return self._responses.pop(0) if self._responses else b""


def fake_drain(monkeypatch, proc, chunks: list[bytes]):
    queue = list(chunks)

    def fake_select(rlist, wlist, xlist, timeout=None):
        return ([rlist[0]] if queue else [], [], [])

    monkeypatch.setattr(m5.select, "select", fake_select)
    monkeypatch.setattr(m5.os, "read", lambda fd, n: queue.pop(0) if queue else b"")
    monkeypatch.setattr(m5.time, "sleep", lambda s: None)
    monkeypatch.setattr(m5._LabgridConsole, "_ensure_proc", lambda self: proc)


def test_labgrid_port_spec_sets_place(monkeypatch):
    monkeypatch.setattr(m5, "labgrid_acquire", lambda p, h: None)
    mint = M5Mint("labgrid://m5stick")
    assert mint.labgrid_place == "m5stick"
    assert mint._lg is not None


def test_local_port_untouched_by_labgrid_fields(tmp_path):
    dev = tmp_path / "ttyUSB0"
    dev.write_text("")
    mint = M5Mint(str(dev))
    assert mint.labgrid_place is None
    assert mint._lg is None


def test_missing_local_port_raises():
    with pytest.raises(M5MintUnavailable, match="does not exist"):
        M5Mint("/dev/nonexistent-m5")


def test_from_env_prefers_labgrid_place(monkeypatch):
    monkeypatch.setenv("TOLLGATE_M5_LABGRID_PLACE", "m5stick")
    monkeypatch.delenv("TOLLGATE_M5_PORT", raising=False)
    monkeypatch.setattr(m5, "labgrid_acquire", lambda p, h: None)
    mint = M5Mint.from_env()
    assert mint.labgrid_place == "m5stick"


def test_from_env_accepts_labgrid_url_in_m5_port(monkeypatch):
    monkeypatch.delenv("TOLLGATE_M5_LABGRID_PLACE", raising=False)
    monkeypatch.setenv("TOLLGATE_M5_PORT", "labgrid://cyd-wallet-take")
    monkeypatch.setattr(m5, "labgrid_acquire", lambda p, h: None)
    assert M5Mint.from_env().labgrid_place == "cyd-wallet-take"


def test_acquire_failure_names_the_problem(monkeypatch):
    def failing_acquire(place, host):
        raise M5MintUnavailable(
            f"labgrid place '{place}' not acquired: acquired by other-lane"
        )

    monkeypatch.setattr(m5, "labgrid_acquire", failing_acquire)
    with pytest.raises(M5MintUnavailable, match="other-lane"):
        M5Mint("labgrid://m5stick")


def test_cli_roundtrip_over_labgrid_console(monkeypatch):
    proc = FakeProc([])
    fake_drain(monkeypatch, proc, [b"wifi=up ssid=x ip=10.0.0.5 st=3 "
                                   b"keyset=00aa url=http://10.0.0.5:3338\r\n"])
    mint = M5Mint.__new__(M5Mint)
    mint.port = "labgrid://m5stick"
    mint._minter = None
    mint.labgrid_place = "m5stick"
    mint._lg = m5._LabgridConsole("m5stick", "ai-legion")
    monkeypatch.setattr(m5, "labgrid_acquire", lambda p, h: None)

    out = mint.cli("status")
    assert "wifi=up" in out
    assert proc.stdin_writes == [b"status\r\n"]


def test_release_closes_console_and_releases_place(monkeypatch):
    released = []
    monkeypatch.setattr(m5, "labgrid_release", lambda p, h: released.append(p))
    mint = M5Mint.__new__(M5Mint)
    mint.port = "labgrid://m5stick"
    mint._minter = None
    mint.labgrid_place = "m5stick"
    mint._lg = m5._LabgridConsole("m5stick", "ai-legion")
    mint._lg._proc = FakeProc([])

    mint.release()
    assert released == ["m5stick"]
    assert mint.labgrid_place is None
    assert mint._lg is None


def test_release_noop_for_local(tmp_path):
    dev = tmp_path / "ttyUSB0"
    dev.write_text("")
    mint = M5Mint(str(dev))
    mint.release()
    assert mint.labgrid_place is None


def test_labgrid_acquire_command_shape(monkeypatch):
    calls = []

    class Ok:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Ok()

    monkeypatch.setattr(subprocess, "run", fake_run)
    m5.labgrid_acquire("m5stick", "ai-legion")
    assert calls[0][:4] == ["ssh", "ai-legion", "labgrid-client", "-p"]
    assert "acquire" in calls[0]


def test_labgrid_acquire_failure_raises_with_detail(monkeypatch):
    class Fail:
        returncode = 1
        stderr = "place already acquired"
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: Fail())
    with pytest.raises(M5MintUnavailable, match="already acquired"):
        m5.labgrid_acquire("m5stick", "ai-legion")
