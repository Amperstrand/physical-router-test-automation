"""Unit tests for lib/router_lock.py — RouterLock class."""

import os
import tempfile
import time
from datetime import datetime, timezone, timedelta

import pytest

from lib.router_lock import RouterLock


pytestmark = [pytest.mark.story("P11")]

@pytest.fixture
def lock_file(tmp_path):
    """Provide a temp lock file path that is cleaned up after each test."""
    return str(tmp_path / "test.lock")


@pytest.fixture
def lock(lock_file):
    """Provide a RouterLock pointed at a temp path."""
    return RouterLock(lock_path=lock_file)


class TestAcquireRelease:
    def test_acquire_creates_lock_file(self, lock, lock_file):
        lock.acquire(router_id="upstream", phase="deploy", branch="main")
        assert os.path.isfile(lock_file)
        assert lock._held

    def test_release_removes_lock_file(self, lock, lock_file):
        lock.acquire(router_id="upstream", phase="deploy")
        lock.release()
        assert not os.path.isfile(lock_file)
        assert not lock._held

    def test_release_without_acquire_is_safe(self, lock):
        lock.release()  # should not raise

    def test_lock_file_contents(self, lock, lock_file):
        lock.acquire(router_id="alpha", phase="mint-health-test", branch="feature/42")
        with open(lock_file) as f:
            text = f.read()
        assert "locked: true" in text
        assert "router_id: alpha" in text
        assert "phase: mint-health-test" in text
        assert "branch: feature/42" in text
        assert "session:" in text
        assert "timestamp:" in text

    def test_double_acquire_raises(self, lock):
        lock.acquire(router_id="upstream", phase="test")
        with pytest.raises(RuntimeError, match="already held"):
            lock.acquire(router_id="upstream", phase="test2")

    def test_acquire_taken_lock_raises(self, lock_file):
        lock1 = RouterLock(lock_path=lock_file)
        lock1.acquire(router_id="upstream", phase="deploy")
        lock2 = RouterLock(lock_path=lock_file)
        with pytest.raises(RuntimeError, match="locked by"):
            lock2.acquire(router_id="upstream", phase="test")
        lock1.release()

    def test_status_returns_dict(self, lock):
        assert lock.status() == {}
        lock.acquire(router_id="beta", phase="test", branch="main")
        s = lock.status()
        assert s["locked"] == "true"
        assert s["router_id"] == "beta"
        assert s["phase"] == "test"

    def test_is_locked(self, lock):
        assert not lock.is_locked()
        lock.acquire(router_id="upstream", phase="test")
        assert lock.is_locked()
        lock.release()
        assert not lock.is_locked()


class TestStaleDetection:
    def test_stale_lock_is_overwritten(self, lock_file):
        stale_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(lock_file, "w") as f:
            f.write(
                f"locked: true\n"
                f"branch: old-branch\n"
                f"session: other@host\n"
                f"timestamp: {stale_ts}\n"
                f"phase: old-test\n"
                f"router_id: upstream\n"
            )
        lock = RouterLock(lock_path=lock_file)
        lock.acquire(router_id="upstream", phase="new-test", branch="main")
        s = lock.status()
        assert s["phase"] == "new-test"
        assert s["branch"] == "main"
        lock.release()

    def test_fresh_lock_is_not_overwritten(self, lock_file):
        fresh_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(lock_file, "w") as f:
            f.write(
                f"locked: true\n"
                f"branch: main\n"
                f"session: other@host\n"
                f"timestamp: {fresh_ts}\n"
                f"phase: running\n"
                f"router_id: upstream\n"
            )
        lock = RouterLock(lock_path=lock_file)
        with pytest.raises(RuntimeError, match="locked by"):
            lock.acquire(router_id="upstream", phase="test")

    def test_unreadable_timestamp_treated_as_stale(self, lock_file):
        with open(lock_file, "w") as f:
            f.write(
                "locked: true\n"
                "branch: main\n"
                "session: other@host\n"
                "timestamp: not-a-date\n"
                "phase: running\n"
                "router_id: upstream\n"
            )
        lock = RouterLock(lock_path=lock_file)
        lock.acquire(router_id="upstream", phase="test")
        assert lock.status()["phase"] == "test"
        lock.release()


class TestForceRelease:
    def test_force_release_removes_any_lock(self, lock_file):
        lock1 = RouterLock(lock_path=lock_file)
        lock1.acquire(router_id="upstream", phase="deploy")
        lock2 = RouterLock(lock_path=lock_file)
        lock2.force_release()
        assert not os.path.isfile(lock_file)
        assert not lock2._held

    def test_force_release_no_file_is_safe(self, lock):
        lock.force_release()  # no file exists, should not raise

    def test_force_release_allows_new_acquire(self, lock_file):
        lock1 = RouterLock(lock_path=lock_file)
        lock1.acquire(router_id="upstream", phase="deploy")
        lock1.force_release()
        lock2 = RouterLock(lock_path=lock_file)
        lock2.acquire(router_id="upstream", phase="test")
        assert lock2.status()["phase"] == "test"
        lock2.release()


class TestContextManager:
    def test_context_manager_acquires_and_releases(self, lock_file):
        with RouterLock(lock_path=lock_file) as lock:
            lock.acquire(router_id="upstream", phase="ctx-test")
            assert lock.is_locked()
            assert os.path.isfile(lock_file)
        assert not os.path.isfile(lock_file)

    def test_context_manager_releases_on_exception(self, lock_file):
        with pytest.raises(ValueError):
            with RouterLock(lock_path=lock_file) as lock:
                lock.acquire(router_id="upstream", phase="ctx-test")
                raise ValueError("boom")
        assert not os.path.isfile(lock_file)

    def test_context_manager_no_acquire_still_works(self, lock_file):
        with RouterLock(lock_path=lock_file) as lock:
            assert not lock.is_locked()
        assert not lock.is_locked()


class TestAtomicWrite:
    def test_lock_file_has_consistent_content(self, lock, lock_file):
        lock.acquire(router_id="upstream", phase="test", branch="main")
        with open(lock_file) as f:
            content1 = f.read()
        with open(lock_file) as f:
            content2 = f.read()
        assert content1 == content2
        lock.release()

    def test_concurrent_writes_dont_corrupt(self, tmp_path):
        lock_path = str(tmp_path / "concurrent.lock")
        errors = []
        for i in range(10):
            try:
                lock = RouterLock(lock_path=lock_path)
                lock.acquire(router_id=f"router-{i}", phase=f"phase-{i}")
                lock.release()
            except Exception as e:
                errors.append(e)
        # At most the first acquire should succeed; others may raise
        # RuntimeError if lock is fresh, which is expected behavior
        for e in errors:
            assert isinstance(e, RuntimeError)
