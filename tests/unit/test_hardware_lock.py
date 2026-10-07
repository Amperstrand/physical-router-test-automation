"""Unit tests for lib/hardware_lock.py — file-based hardware mutex.

These tests exercise the standalone fallback implementation (JSON lock
files, matching acquire_hardware_lock's writer) and pin its contract.
When tollgate_lab is installed, the canonical implementation from
tollgate_lab.hardware.lock is used instead, and these tests skip.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import pytest

try:
    import tollgate_lab
    _HAS_TOLLGATE_LAB = True
except ImportError:
    _HAS_TOLLGATE_LAB = False

pytestmark = pytest.mark.skipif(_HAS_TOLLGATE_LAB,
    reason="tollgate_lab installed — standalone fallback not active")

from lib.hardware_lock import (
    HARDWARE_LOCK,
    _is_stale,
    read_hardware_lock,
    is_hardware_locked,
    acquire_hardware_lock,
    release_hardware_lock,
)


pytestmark = [pytest.mark.story("P11")]

def _write_lock(path, locked="true", session="user@host", ts=None):
    ts = ts or datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps({
        "locked": locked,
        "session": session,
        "timestamp": ts,
        "phase": "test",
    }))


class TestReadHardwareLock:
    def test_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("lib.hardware_lock.HARDWARE_LOCK", tmp_path / "no.lock")
        assert read_hardware_lock() is None

    def test_valid(self, tmp_path, monkeypatch):
        lock = tmp_path / "hw.lock"
        _write_lock(lock, session="alice@host1")
        monkeypatch.setattr("lib.hardware_lock.HARDWARE_LOCK", lock)
        result = read_hardware_lock()
        assert result["session"] == "alice@host1"
        assert result["locked"] == "true"

    def test_corrupt_json_returns_none(self, tmp_path, monkeypatch):
        lock = tmp_path / "bad.lock"
        lock.write_text("this is not json {{{")
        monkeypatch.setattr("lib.hardware_lock.HARDWARE_LOCK", lock)
        assert read_hardware_lock() is None


class TestIsStale:
    def test_fresh(self):
        ts = datetime.now(timezone.utc).isoformat()
        assert _is_stale({"timestamp": ts}) is False

    def test_old(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        assert _is_stale({"timestamp": ts}) is True

    def test_missing(self):
        assert _is_stale({}) is True

    def test_bad_format_is_stale_not_crash(self):
        # a malformed timestamp must fail CLOSED (stale), never raise —
        # lock checks run in test setup paths where a crash is worse
        # than a stale verdict
        assert _is_stale({"timestamp": "garbage"}) is True
        assert _is_stale({"timestamp": None}) is True


class TestIsHardwareLocked:
    def test_no_lock(self, tmp_path, monkeypatch):
        monkeypatch.setattr("lib.hardware_lock.HARDWARE_LOCK", tmp_path / "no.lock")
        assert is_hardware_locked() is False

    def test_locked_by_other(self, tmp_path, monkeypatch):
        lock = tmp_path / "hw.lock"
        _write_lock(lock, session="other@host")
        monkeypatch.setattr("lib.hardware_lock.HARDWARE_LOCK", lock)
        monkeypatch.setattr("lib.hardware_lock._session_id", lambda: "me@host")
        assert is_hardware_locked() is True

    def test_unlocked_flag(self, tmp_path, monkeypatch):
        lock = tmp_path / "hw.lock"
        _write_lock(lock, locked="false", session="me@host")
        monkeypatch.setattr("lib.hardware_lock.HARDWARE_LOCK", lock)
        assert is_hardware_locked() is False

    def test_stale_lock_treated_as_unlocked(self, tmp_path, monkeypatch):
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        lock = tmp_path / "hw.lock"
        _write_lock(lock, session="old@host", ts=old_ts)
        monkeypatch.setattr("lib.hardware_lock.HARDWARE_LOCK", lock)
        assert is_hardware_locked() is False


class TestAcquireRelease:
    def test_acquire_creates(self, tmp_path, monkeypatch):
        lock = tmp_path / "hw.lock"
        monkeypatch.setattr("lib.hardware_lock.HARDWARE_LOCK", lock)
        monkeypatch.setattr("lib.hardware_lock._session_id", lambda: "test@host")
        monkeypatch.setattr("lib.hardware_lock._git_branch", lambda: "test-branch")
        monkeypatch.setattr("lib.hardware_lock._PROJECT_ROOT", tmp_path)
        acquire_hardware_lock("test-phase")
        assert lock.exists()
        data = read_hardware_lock()
        assert data["phase"] == "test-phase"
        assert data["session_id"] == "test@host"
        assert data["git_branch"] == "test-branch"
        # fresh acquire implies locked
        assert is_hardware_locked() is True

    def test_release_removes(self, tmp_path, monkeypatch):
        lock = tmp_path / "hw.lock"
        _write_lock(lock)
        monkeypatch.setattr("lib.hardware_lock.HARDWARE_LOCK", lock)
        release_hardware_lock()
        assert not lock.exists()

    def test_release_nonexistent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("lib.hardware_lock.HARDWARE_LOCK", tmp_path / "no.lock")
        release_hardware_lock()
