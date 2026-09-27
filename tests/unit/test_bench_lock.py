"""Unit tests for :mod:`lib.bench_lock`.

Covers the two things the bench-lock card actually asks for:

* the negative control — while one holder holds the lock, a *second process*
  must refuse, and the error must name the holder;
* **canonical interop** — the holder line this module writes is byte-shape
  compatible with ``scripts/mt3000-bench/bench-lock.sh`` (``<profile> pid=…
  purpose=… since=… task=… host=…``), the child environment it exports
  satisfies ``bench-lock require``, and a stale holder line refuses by default
  exactly like the shell helper does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lib import bench_lock as _bench_lock
from lib.bench_lock import (
    HOLDER_LINE_RE,
    BenchBusy,
    BenchLock,
    BenchNotHeld,
    BenchStale,
    bench_lock_cli,
    bench_state,
    deploy_apk_arguments,
    holder_line,
    main,
    parse_holder,
    read_holder,
    require,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def lock_path(tmp_path):
    return str(tmp_path / "bench-mt3000.lock")


# ---------------------------------------------------------------------------
# holder-line parsing / writing
# ---------------------------------------------------------------------------


def test_parse_holder_reads_the_canonical_shell_line():
    """The exact line the sanctioned helper writes (measured 2026-09-25)."""
    holder = parse_holder(
        "manager pid=2584919 purpose=curl|bash-pre16-validation "
        "since=2026-09-25T00:19:40+02:00 task=t_a05094ad host=lexar"
    )
    assert holder.profile == "manager"
    assert holder.pid == "2584919"
    assert holder.purpose == "curl|bash-pre16-validation"
    assert holder.since.startswith("2026-09-25")
    assert holder.task == "t_a05094ad"
    assert holder.host == "lexar"
    assert holder.is_live_metadata


def test_parse_holder_reads_the_legacy_key_value_line():
    """Back-compat: an older revision wrote `profile=<p> pid=<n> …`."""
    holder = parse_holder(
        "profile=manager pid=1 task=t_x purpose=p since=2026-09-25T00:19:40+02:00"
    )
    assert holder.profile == "manager"
    assert holder.pid == "1"
    assert holder.purpose == "p"


def test_parse_holder_treats_task_dash_as_absent():
    holder = parse_holder("manager pid=1 purpose=ad-hoc since=2026-01-01T00:00:00+00:00 task=-")
    assert holder.task == ""
    assert holder.is_live_metadata


def test_parse_holder_of_empty_file_is_empty():
    assert parse_holder("").is_empty
    assert not parse_holder("").is_live_metadata


def test_holder_line_is_canonical_and_names_profile_pid_purpose_and_task():
    line = holder_line("prta install-path e2e", task_id="t_a05094ad", profile="manager")
    match = HOLDER_LINE_RE.match(line)
    assert match, f"holder line is not canonical: {line!r}"
    assert f"pid={os.getpid()}" in line
    assert "task=t_a05094ad" in line
    # spaces are folded so the line stays parseable (canonical rule)
    assert "purpose=prta_install-path_e2e" in line
    assert " since=" in line and " host=" in line
    parsed = parse_holder(line)
    assert parsed.profile == "manager"
    assert parsed.purpose == "prta_install-path_e2e"
    assert parsed.task == "t_a05094ad"
    assert parsed.host


def test_holder_line_defaults_task_to_dash():
    assert "task=-" in holder_line("ad-hoc", profile="p", pid=1)


# ---------------------------------------------------------------------------
# acquire / hold / release
# ---------------------------------------------------------------------------


def test_acquire_hold_release_roundtrip(lock_path):
    lock = BenchLock(purpose="unit test", task_id="t_unit", path=lock_path)
    free, _ = lock.status()
    assert free
    assert lock.state == "free"

    with lock:
        assert lock.held
        assert lock.state == "held"
        assert "purpose=unit_test" in read_holder(lock_path).raw
        assert "task=t_unit" in read_holder(lock_path).raw
        assert lock.status()[0] is False

    assert not lock.held
    assert lock.status()[0] is True
    # releasing CLEARS the line: a leftover line would make the next window
    # refuse as stale (the shell helper's rule).
    assert read_holder(lock_path).is_empty
    assert lock.state == "free"


def test_second_holder_is_refused_and_message_names_the_holder(lock_path):
    first = BenchLock(purpose="first-window", task_id="t_first", path=lock_path).acquire()
    try:
        second = BenchLock(purpose="second-window", task_id="t_second", path=lock_path)
        with pytest.raises(BenchBusy) as excinfo:
            second.acquire()
        message = str(excinfo.value)
        assert "BENCH BUSY" in message
        assert "t_first" in message  # the holder identity is in the error
        assert "purpose=first-window" in message or "purpose=first-window" in read_holder(lock_path).raw
        assert not second.held
    finally:
        first.release()


def test_release_is_idempotent_and_safe_when_never_held(lock_path):
    lock = BenchLock(purpose="x", path=lock_path)
    lock.release()  # never held
    lock.acquire()
    lock.release()
    lock.release()


def test_release_does_not_clear_another_windows_holder_line(lock_path):
    """Never steal another window's line, even if our instance is stale."""
    other = BenchLock(purpose="other", task_id="t_other", path=lock_path).acquire()
    stale = BenchLock(purpose="ours", task_id="t_ours", path=lock_path)
    try:
        stale.release()  # never held here
        assert read_holder(lock_path).task == "t_other"
    finally:
        other.release()


def test_real_second_process_is_refused(lock_path):
    """The negative control: another *process* cannot take the bench."""
    script = (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from lib.bench_lock import BenchLock, BenchBusy\n"
        "try:\n"
        f"    BenchLock(purpose='other-process', task_id='t_other', path={lock_path!r}).acquire()\n"
        "except BenchBusy as exc:\n"
        "    print('REFUSED:', exc)\n"
        "    sys.exit(3)\n"
        "print('ACQUIRED'); sys.exit(0)\n"
    )
    with BenchLock(purpose="holder-window", task_id="t_holder", path=lock_path):
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
        )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "REFUSED" in result.stdout
    assert "t_holder" in result.stdout

    # once released, the same process can take it
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ACQUIRED" in result.stdout


# ---------------------------------------------------------------------------
# crash / stale-metadata semantics (canonical: free by flock, refused by default)
# ---------------------------------------------------------------------------


def test_lock_survives_a_killed_holder_because_flock_is_released_by_the_kernel(lock_path):
    """A dead worker must never wedge the bench (the incident's root cause)."""
    script = (
        f"import sys, time; sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from lib.bench_lock import BenchLock\n"
        f"lock = BenchLock(purpose='crashed-holder', task_id='t_crashed', path={lock_path!r})\n"
        "lock.acquire()\n"
        "print('HELD', flush=True)\n"
        "time.sleep(300)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "HELD"
        with pytest.raises(BenchBusy):
            BenchLock(purpose="probe", path=lock_path).acquire()
    finally:
        proc.kill()
        proc.wait(timeout=30)

    # the kernel dropped the flock with the process
    assert BenchLock(purpose="after-crash", path=lock_path).status()[0] is True
    # ... but the dead owner's line is still there: STALE, refuse by default
    assert bench_state(lock_path) == "stale"
    assert read_holder(lock_path).task == "t_crashed"
    with pytest.raises(BenchStale) as excinfo:
        BenchLock(purpose="after-crash", path=lock_path).acquire()
    assert "explicit" in str(excinfo.value)
    assert "t_crashed" in str(excinfo.value)

    # and the explicit recovery path works
    takeover = BenchLock(purpose="after-crash", path=lock_path).acquire(reclaim_stale=True)
    assert takeover.held
    takeover.release()
    assert bench_state(lock_path) == "free"


def test_state_reports_free_when_the_line_was_cleared(lock_path):
    with BenchLock(purpose="clean-window", path=lock_path):
        assert bench_state(lock_path) == "held"
    assert bench_state(lock_path) == "free"


def test_state_ignores_the_legacy_release_marker(lock_path):
    """An older revision left "released by …" behind; that must not read as stale."""
    Path(lock_path).write_text("released by profile=manager pid=1 purpose=x\n")
    assert bench_state(lock_path) == "free"
    with BenchLock(purpose="after-legacy", path=lock_path) as lock:
        assert lock.held


# ---------------------------------------------------------------------------
# require() + child environment (what bench-deploy-apk checks)
# ---------------------------------------------------------------------------


def test_require_refuses_outside_a_window(lock_path, monkeypatch):
    for var in ("BENCH_LOCK_HELD", "BENCH_LOCK_HOLDER_PID"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(BenchNotHeld) as excinfo:
        require(BenchLock(purpose="probe", path=lock_path))
    message = str(excinfo.value)
    assert "BENCH NOT LOCKED" in message
    assert "Nothing was touched" in message


def test_child_env_satisfies_require_inside_the_window(lock_path, monkeypatch):
    """The env a child needs for `bench-lock require` / `bench-deploy-apk`."""
    for var in ("BENCH_LOCK_HELD", "BENCH_LOCK_HOLDER_PID"):
        monkeypatch.delenv(var, raising=False)
    with BenchLock(purpose="child-env", task_id="t_child", path=lock_path) as lock:
        env = lock.child_env()
        assert env["BENCH_LOCK_HELD"] == "1"
        assert env["BENCH_LOCK_HOLDER_PID"] == str(os.getpid())
        assert env["BENCH_LOCK_PATH"] == lock_path

        script = (
            f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "from lib.bench_lock import BenchLock, require, BenchNotHeld\n"
            f"lock = BenchLock(purpose='child', path={lock_path!r})\n"
            "try:\n"
            "    holder = require(lock)\n"
            "except BenchNotHeld as exc:\n"
            "    print('NOTHELD:', exc); sys.exit(4)\n"
            "print('HELD-OK', holder.purpose, holder.pid); sys.exit(0)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, env=env
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "HELD-OK child-env" in result.stdout


def test_require_refuses_when_the_holder_line_names_another_pid(lock_path, monkeypatch):
    monkeypatch.setenv("BENCH_LOCK_HELD", "1")
    monkeypatch.setenv("BENCH_LOCK_HOLDER_PID", "999999")
    with BenchLock(purpose="mismatch", path=lock_path):
        with pytest.raises(BenchNotHeld) as excinfo:
            require(BenchLock(purpose="probe", path=lock_path))
        assert "another window replaced" in str(excinfo.value).lower()


def test_lock_require_on_a_non_held_instance_refuses(lock_path):
    with pytest.raises(BenchNotHeld):
        BenchLock(purpose="not-held", path=lock_path).require()


# ---------------------------------------------------------------------------
# canonical shell-helper interop
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not bench_lock_cli(), reason="scripts/mt3000-bench/bench-lock.sh not installed")
def test_canonical_helper_parses_our_holder_line(lock_path, monkeypatch):
    """`bench-lock status` must name OUR holder when we hold the lock."""
    monkeypatch.setenv("BENCH_LOCK_PATH", lock_path)
    with BenchLock(purpose="python-window", task_id="t_py", path=lock_path) as lock:
        output = lock.canonical_status()
    assert "HELD" in output, output
    assert "python-window" in output, output
    assert "t_py" in output, output


@pytest.mark.skipif(not bench_lock_cli(), reason="scripts/mt3000-bench/bench-lock.sh not installed")
def test_canonical_helper_require_accepts_our_child_env(lock_path, monkeypatch):
    """The sanctioned `require` gate must accept a Python-held window."""
    monkeypatch.setenv("BENCH_LOCK_PATH", lock_path)
    with BenchLock(purpose="python-window", task_id="t_py", path=lock_path) as lock:
        result = subprocess.run(
            [bench_lock_cli(), "require"],
            capture_output=True,
            text=True,
            timeout=60,
            env=lock.child_env(),
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LOCK-HELD" in (result.stdout + result.stderr)


@pytest.mark.skipif(not bench_lock_cli(), reason="scripts/mt3000-bench/bench-lock.sh not installed")
def test_canonical_helper_sees_free_after_our_release(lock_path, monkeypatch):
    monkeypatch.setenv("BENCH_LOCK_PATH", lock_path)
    lock = BenchLock(purpose="python-window", path=lock_path)
    with lock:
        pass
    status = lock.canonical_status()
    assert "FREE" in status, status


def test_deploy_apk_arguments_are_none_without_the_helper(monkeypatch):
    monkeypatch.setenv("BENCH_DEPLOY_APK_CLI", "/nonexistent/bench-deploy-apk")
    monkeypatch.setenv("PATH", "")
    assert deploy_apk_arguments(apk="/x/a.apk", sha256="0" * 64) is None


def test_deploy_apk_arguments_requires_a_named_artifact(monkeypatch, tmp_path):
    fake = tmp_path / "bench-deploy-apk"
    fake.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("BENCH_DEPLOY_APK_CLI", str(fake))
    argv = deploy_apk_arguments(apk="/x/a.apk", sha256="a" * 64, router="192.168.1.1", task="t_x")
    assert argv is not None
    assert argv[0] == str(fake)
    # a deploy that cannot name its artifact is refused by the helper
    assert argv[argv.index("--sha256") + 1] == "a" * 64
    assert argv[argv.index("--apk") + 1] == "/x/a.apk"


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


def test_env_override_for_the_lock_path(monkeypatch, tmp_path):
    custom = tmp_path / "custom.lock"
    monkeypatch.setenv("TOLLGATE_BENCH_LOCK", str(custom))
    lock = BenchLock(purpose="env")
    assert lock.path == str(custom)
    with lock:
        assert custom.exists()


def test_canonical_bench_lock_path_env_is_honoured(monkeypatch, tmp_path):
    """BENCH_LOCK_PATH is the shell helper's override; honour it too."""
    custom = tmp_path / "canonical.lock"
    monkeypatch.delenv("TOLLGATE_BENCH_LOCK", raising=False)
    monkeypatch.setenv("BENCH_LOCK_PATH", str(custom))
    assert BenchLock(purpose="env").path == str(custom)


def test_one_liner_acquire_keeps_the_lock_alive(lock_path):
    """`BenchLock(...).acquire()` must not be garbage-collected into a release."""
    BenchLock(purpose="one-liner", task_id="t_one_liner", path=lock_path).acquire()
    try:
        assert BenchLock(purpose="probe", path=lock_path).status()[0] is False
    finally:
        for live in list(_bench_lock._LIVE_LOCKS):
            if live.path == lock_path:
                live.release()
    assert BenchLock(purpose="probe", path=lock_path).status()[0] is True


def test_cli_status_check_and_acquire(lock_path, capsys, monkeypatch):
    monkeypatch.setenv("TOLLGATE_BENCH_LOCK", lock_path)

    assert main(["status", "--path", lock_path]) == 0
    out = capsys.readouterr().out
    assert "state  : FREE" in out
    assert "free   : True" in out
    assert "holder : <none>" in out

    holder = BenchLock(purpose="cli-held", task_id="t_cli", path=lock_path).acquire()
    try:
        assert main(["check", "--path", lock_path]) == 1
        out = capsys.readouterr().out
        assert "state  : HELD" in out
        assert "free   : False" in out
        assert "t_cli" in out
    finally:
        holder.release()

    assert main(["check", "--path", lock_path]) == 0
    assert main(["acquire", "--path", lock_path, "--purpose", "cli", "--task", "t_cli2"]) == 0


def test_cli_require_refuses_outside_a_window(lock_path, capsys, monkeypatch):
    monkeypatch.delenv("BENCH_LOCK_HELD", raising=False)
    assert main(["require", "--path", lock_path]) == 4
    assert "BENCH NOT LOCKED" in capsys.readouterr().err


def test_cli_acquire_refuses_a_stale_line_without_reclaim(lock_path, capsys):
    Path(lock_path).write_text(holder_line("dead-window", task_id="t_dead", profile="ghost", pid=999999) + "\n")
    assert main(["acquire", "--path", lock_path]) == 5
    err = capsys.readouterr().err
    assert "STALE" in err and "t_dead" in err
    assert main(["acquire", "--path", lock_path, "--reclaim-stale"]) == 0
