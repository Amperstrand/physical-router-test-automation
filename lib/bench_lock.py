"""Bench lock — the shared GL-MT3000 bench is a single-owner resource.

The sanctioned implementation lives in ``scripts/mt3000-bench/`` (branch
``pr/bench-mt3000-single-owner``) and is installed on ``PATH`` as ``bench-lock``
/ ``bench-with-lock`` / ``bench-deploy-apk`` from the checkout.  This module is
the *Python-side* client of that convention: it takes the **same** ``flock`` on
the **same** file and writes the **same** holder line, so a Python scenario,
``bench-lock status`` and ``bench-deploy-apk`` all agree on who owns the bench.

Convention (kanban ``t_aa94ad3b``, incident of 2026-09-24):
``~/.hermes/state/bench-mt3000.lock`` is held with ``flock`` for the whole
duration of a router-touching run, and carries a human-readable holder line so
the *next* run can say who to talk to::

    <profile> pid=<pid> purpose=<purpose> since=<iso8601> task=<id|-> host=<hostname>

Why: a stray deploy loop on the shared bench re-installed an old build three
times in one evening and silently reverted the #566 admin-board nft guard
mid-smoke-test.  An advisory lock plus a named holder is the cheap defence.

Interop rules this module honours (so the shell tooling accepts our window):

* the flock is the authority — the kernel drops it when the holder dies, so a
  crashed worker can never wedge the bench;
* ``purpose`` is whitespace-free (spaces folded to ``_``);
* a holder line with **no flock behind it** is STALE metadata: the bench is
  free by flock but we refuse by default and require an explicit
  ``reclaim_stale``;
* releasing **clears** the holder line (a leftover line is exactly what makes
  the next window refuse);
* a child process may run ``bench-lock require`` / ``bench-deploy-apk`` only if
  ``BENCH_LOCK_HELD=1`` and ``BENCH_LOCK_HOLDER_PID`` matches the holder line —
  :meth:`BenchLock.child_env` provides both.

Usage::

    from lib.bench_lock import BenchLock, BenchBusy

    with BenchLock(purpose="prta install-path e2e", task_id="t_a05094ad") as lock:
        subprocess.run(cmd, env=lock.child_env())

or from a shell::

    bench-with-lock --purpose "<what>" -- <your script>
"""

from __future__ import annotations

import argparse
import fcntl
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_LOCK_PATH = os.path.expanduser("~/.hermes/state/bench-mt3000.lock")
ENV_LOCK_PATH = "TOLLGATE_BENCH_LOCK"
#: canonical override honoured by scripts/mt3000-bench/bench-lock.sh
ENV_CANONICAL_LOCK_PATH = "BENCH_LOCK_PATH"
ENV_RECLAIM_STALE = "TOLLGATE_BENCH_RECLAIM_STALE"
ENV_LOCK_CLI = "BENCH_LOCK_CLI"
ENV_DEPLOY_APK_CLI = "BENCH_DEPLOY_APK_CLI"

#: legacy marker: an older revision of this module wrote "released by ..." into
#: the lock file instead of clearing it.  Tolerated on read, never written.
RELEASED_MARKER = "released"

#: canonical holder-line shape (profile first, then key=value tokens)
HOLDER_LINE_RE = re.compile(
    r"^(?P<profile>\S+)\s+pid=(?P<pid>\d+)"
    r"\s+purpose=(?P<purpose>\S+)"
    r"\s+since=(?P<since>\S+)"
    r"(?:\s+task=(?P<task>\S+))?"
    r"(?:\s+host=(?P<host>\S+))?"
)

#: Acquired locks keep themselves alive here.  A one-liner
#: ``BenchLock(purpose=...).acquire()`` would otherwise be garbage-collected
#: (closing the fd and silently dropping the flock) — exactly the class of
#: silent-release bug this module exists to prevent.  Entries are removed on
#: :meth:`BenchLock.release`.
_LIVE_LOCKS: list[BenchLock] = []


class BenchBusy(RuntimeError):
    """The bench lock is held by someone else; the message names the holder."""


class BenchStale(BenchBusy):
    """A holder line is present but no flock backs it (its owner died).

    The bench is *free* by flock, but recovery is explicit only: pass
    ``reclaim_stale=True`` (CLI/scripts: ``--reclaim-stale`` or
    ``TOLLGATE_BENCH_RECLAIM_STALE=1``).
    """


class BenchNotHeld(RuntimeError):
    """We are not inside a bench lock window (``require`` failed)."""


@dataclass(frozen=True)
class Holder:
    """Parsed holder identity line."""

    raw: str = ""
    profile: str = ""
    pid: str = ""
    task: str = ""
    purpose: str = ""
    since: str = ""
    host: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.raw.strip()

    @property
    def is_release_marker(self) -> bool:
        """True for the legacy "released by …" line (not a live holder)."""
        return self.raw.strip().lower().startswith(RELEASED_MARKER)

    @property
    def is_live_metadata(self) -> bool:
        """True when the line claims an owner (not empty, not a release marker)."""
        return not self.is_empty and not self.is_release_marker

    def describe(self) -> str:
        if self.is_empty:
            return "<none>"
        return (
            f"profile={self.profile or '?'} pid={self.pid or '?'} "
            f"purpose={self.purpose or '?'} since={self.since or '?'} "
            f"task={self.task or '-'} host={self.host or '?'}"
        )


def lock_path(path: str | None = None) -> str:
    if path:
        return os.path.expanduser(path)
    return os.path.expanduser(
        os.environ.get(ENV_LOCK_PATH)
        or os.environ.get(ENV_CANONICAL_LOCK_PATH)
        or DEFAULT_LOCK_PATH
    )


def parse_holder(text: str) -> Holder:
    """Parse a holder line.

    Handles the canonical shape (bare profile token first, then ``key=value``)
    *and* the older all-``key=value`` shape, plus a bare profile with no pid.
    """
    raw = (text or "").strip()
    if not raw:
        return Holder()
    fields: dict[str, str] = {}
    tokens = raw.replace("\n", " ").split()
    for token in tokens:
        if "=" in token:
            key, _, value = token.partition("=")
            fields[key.strip().lower()] = value.strip()
    match = HOLDER_LINE_RE.match(raw)
    profile = match.group("profile") if match else fields.get("profile", "")
    if not profile and tokens and "=" not in tokens[0]:
        # bare first token e.g. "manager pid=1 purpose=x since=..."
        profile = tokens[0]
    return Holder(
        raw=raw,
        profile=profile,
        pid=fields.get("pid", ""),
        task="" if fields.get("task", "") in ("", "-") else fields["task"],
        purpose=fields.get("purpose", ""),
        since=fields.get("since", ""),
        host=fields.get("host", ""),
    )


def clean_field(value: str, *, default: str = "-") -> str:
    """Fold whitespace so the holder line stays parseable (canonical rule)."""
    folded = re.sub(r"\s+", "_", (value or "").strip())
    return folded or default


def canonical_profile() -> str:
    return os.environ.get("BENCH_PROFILE") or os.environ.get("HERMES_PROFILE") or os.environ.get(
        "USER", "unknown"
    )


def holder_line(
    purpose: str,
    *,
    task_id: str | None = None,
    profile: str | None = None,
    pid: int | None = None,
) -> str:
    """Build the canonical holder line written into the lock file while held.

    ``<profile> pid=<pid> purpose=<purpose> since=<iso8601> task=<id|-> host=<hostname>``
    — the same shape ``scripts/mt3000-bench/bench-lock.sh`` writes.
    """
    who = clean_field(profile or canonical_profile(), default="unknown")
    task = task_id or os.environ.get("HERMES_TASK_ID") or ""
    return (
        f"{who} pid={os.getpid() if pid is None else pid}"
        f" purpose={clean_field(purpose, default='ad-hoc')}"
        f" since={datetime.now().astimezone().isoformat(timespec='seconds')}"
        f" task={clean_field(task)}"
        f" host={clean_field(platform.node(), default='unknown')}"
    )


def read_holder(path: str | None = None) -> Holder:
    try:
        with open(lock_path(path), encoding="utf-8") as handle:
            return parse_holder(handle.readline())
    except FileNotFoundError:
        return Holder()


# ---------------------------------------------------------------------------
# flock primitives
# ---------------------------------------------------------------------------


def _flock_held(path: str | None = None) -> bool:
    """Is the flock held by *anyone* (fresh open file description, like the shell)?"""
    target = lock_path(path)
    if not os.path.exists(target):
        return False
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = open(target, "a+", encoding="utf-8")
    except OSError:
        return False
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True  # someone else holds it: a fresh description cannot take it
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def bench_state(path: str | None = None) -> str:
    """``"held"`` | ``"stale"`` | ``"free"`` — the canonical state machine."""
    if _flock_held(path):
        return "held"
    holder = read_holder(path)
    return "stale" if holder.is_live_metadata else "free"


# ---------------------------------------------------------------------------
# canonical tooling discovery (the sanctioned shell helpers)
# ---------------------------------------------------------------------------


def _script_in_repo(name: str) -> str:
    root = Path(__file__).resolve().parents[1] / "scripts" / "mt3000-bench"
    candidate = root / name
    return str(candidate) if candidate.is_file() else ""


def canonical_cli(name: str, env_var: str) -> str:
    """Locate a ``scripts/mt3000-bench`` helper: env override, PATH, then repo.

    Empty string means "not installed on this host" — callers must report that
    and fall back to the in-process implementation rather than guess.
    """
    explicit = os.environ.get(env_var, "")
    if explicit:
        return explicit if os.path.isfile(explicit) else ""
    found = shutil.which(name)
    if found:
        return found
    return _script_in_repo(name + ".sh")


def bench_lock_cli() -> str:
    return canonical_cli("bench-lock", ENV_LOCK_CLI)


def bench_deploy_apk_cli() -> str:
    return canonical_cli("bench-deploy-apk", ENV_DEPLOY_APK_CLI)


def require(lock: BenchLock | None = None) -> Holder:
    """Assert we are inside a bench lock window (canonical ``bench-lock require``).

    Mirrors the shell's checks: ``BENCH_LOCK_HELD=1``, ``BENCH_LOCK_HOLDER_PID``
    equal to the holder line's ``pid=``, and the flock still held.  Every
    router-touching helper invoked by this harness calls it.
    """
    if os.environ.get("BENCH_LOCK_HELD") != "1":
        raise BenchNotHeld(
            "BENCH NOT LOCKED: this process is not running inside a bench lock window. "
            "Take the lock first (lib.bench_lock.BenchLock, `bench-with-lock --purpose <p> -- "
            "<cmd>`, or `bench-lock exec --purpose <p> -- <cmd>`). Nothing was touched."
        )
    path = lock_path(lock.path if lock is not None else None)
    holder = read_holder(path)
    claimed = os.environ.get("BENCH_LOCK_HOLDER_PID", "")
    if holder.pid and claimed and holder.pid != claimed:
        raise BenchNotHeld(
            f"BENCH NOT LOCKED: the holder line names pid={holder.pid} but our lock window is "
            f"pid={claimed} ({holder.describe()}). Another window replaced the holder line; "
            "nothing was touched."
        )
    if not _flock_held(path):
        raise BenchNotHeld(
            f"BENCH NOT LOCKED: the flock on {path} is not held (the window closed?). "
            f"Holder line: {holder.describe()}"
        )
    return holder


class BenchLock:
    """``flock``-based single-owner lock for the bench router.

    The flock IS the authority (released automatically when the process dies, so
    a dead worker can never wedge the bench); the holder line is written for
    humans *and* for the shell tooling's staleness check.
    """

    def __init__(
        self,
        *,
        purpose: str,
        task_id: str | None = None,
        profile: str | None = None,
        path: str | None = None,
        reclaim_stale: bool | None = None,
    ) -> None:
        self.path = lock_path(path)
        self.purpose = purpose
        self.task_id = task_id
        self.profile = profile
        self.reclaim_stale = (
            os.environ.get(ENV_RECLAIM_STALE, "").lower() in ("1", "true", "yes")
            if reclaim_stale is None
            else reclaim_stale
        )
        self._handle = None
        self._holder = Holder()

    # -- api ---------------------------------------------------------------

    @property
    def held(self) -> bool:
        return self._handle is not None

    @property
    def state(self) -> str:
        return bench_state(self.path)

    def _open_locked(self):
        """Open the lock file and take the flock, or raise :class:`BenchBusy`."""
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")  # kept open while held
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.seek(0)
            current = parse_holder(handle.read())
            handle.close()
            raise BenchBusy(
                "BENCH BUSY: the MT3000 bench is locked by "
                f"{current.describe() if current.is_live_metadata else '<unknown holder>'} "
                f"[{current.raw or 'no holder line'}] ({self.path}). "
                "Wait for that run to finish, or "
                f"kill its tree (pgrep -af {current.task or current.pid or '<task-id>'}) "
                f"and retry. [`bench-lock status` shows the current owner.] "
                f"[{'/'.join(str(exc).splitlines())}]"
            ) from exc
        return handle

    def acquire(self, *, write_holder: bool = True, reclaim_stale: bool | None = None) -> BenchLock:
        """Take the lock or raise :class:`BenchBusy` / :class:`BenchStale`.

        A stale holder line (no flock behind it — the previous owner died)
        refuses by default; pass ``reclaim_stale=True`` to take it over
        explicitly.
        """
        if self.held:
            return self
        reclaim = self.reclaim_stale if reclaim_stale is None else reclaim_stale
        handle = self._open_locked()
        if not write_holder:
            handle.close()
            return self
        handle.seek(0)
        previous = parse_holder(handle.read())
        if previous.is_live_metadata and not reclaim:
            handle.close()
            raise BenchStale(
                "BENCH STALE: a holder line is present with no flock behind it — its owner is "
                f"gone: {previous.describe()} [{previous.raw}] ({self.path}). The bench IS free, "
                "but recovery is explicit only. Re-check that no other window is live, then "
                "re-run with --reclaim-stale (or TOLLGATE_BENCH_RECLAIM_STALE=1)."
            )
        handle.seek(0)
        handle.truncate()
        handle.write(
            holder_line(self.purpose, task_id=self.task_id, profile=self.profile) + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        self._holder = read_holder(self.path)
        _LIVE_LOCKS.append(self)
        return self

    def release(self) -> None:
        """Drop the flock and clear our holder line (never another window's)."""
        if self._handle is None:
            return
        try:
            handle = self._handle
            handle.seek(0)
            current = parse_holder(handle.readline())
            # only clear the line if it is still ours
            if not current.pid or current.pid == str(os.getpid()):
                handle.seek(0)
                handle.truncate()
                handle.flush()
        finally:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
            if self in _LIVE_LOCKS:
                _LIVE_LOCKS.remove(self)

    @property
    def holder(self) -> Holder:
        return self._holder if self.held else read_holder(self.path)

    def status(self) -> tuple[bool, Holder]:
        """Return ``(free, holder)`` — free by *flock*, without stealing the lock."""
        if _flock_held(self.path):
            return False, read_holder(self.path)
        return True, read_holder(self.path)

    def child_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Environment for child processes so ``bench-lock require`` accepts them."""
        env = {**os.environ, **(extra or {})}
        if self.held:
            env["BENCH_LOCK_HELD"] = "1"
            env["BENCH_LOCK_HOLDER_PID"] = str(os.getpid())
            env["BENCH_LOCK_PATH"] = self.path
            env["BENCH_LOCK_PURPOSE"] = clean_field(self.purpose, default="ad-hoc")
            env["BENCH_LOCK_TASK"] = clean_field(self.task_id or "-")
        return env

    def require(self) -> Holder:
        """Assert this window is still live (see module-level :func:`require`)."""
        if not self.held:
            raise BenchNotHeld(
                "BENCH NOT LOCKED: this BenchLock instance is not held; acquire it first."
            )
        return require(self)

    def canonical_status(self) -> str:
        """Ask the sanctioned shell helper for the state, when it is installed."""
        cli = bench_lock_cli()
        if not cli:
            return ""
        try:
            result = subprocess.run(
                [cli, "status"], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
            return ""
        return (result.stdout + result.stderr).strip()

    def __enter__(self) -> BenchLock:
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


def deploy_apk_arguments(
    *,
    apk: str,
    sha256: str,
    name: str | None = None,
    router: str | None = None,
    task: str | None = None,
    extra: list[str] | None = None,
) -> list[str] | None:
    """argv for the sanctioned ``bench-deploy-apk`` helper, or ``None``.

    The helper refuses to run outside a lock window, names its artifact with a
    mandatory ``--sha256``, rotates foreign staged apks, refuses substituted
    ones and *verifies the installed binary against the payload of the artifact
    it was told to install*.  When it is not installed on this host the caller
    must fall back to a plain install **plus this repo's own identity gate** —
    never to an unverified install.
    """
    cli = bench_deploy_apk_cli()
    if not cli:
        return None
    argv = [cli, "--apk", apk, "--sha256", sha256]
    if name:
        argv += ["--name", name]
    if router:
        argv += ["--router", router]
    if task:
        argv += ["--task", task]
    return argv + list(extra or [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bench lock helper for the shared MT3000")
    parser.add_argument(
        "action", choices=["status", "check", "acquire", "require"], nargs="?", default="status"
    )
    parser.add_argument("--path", default=None, help="lock file (default ~/.hermes/state/bench-mt3000.lock)")
    parser.add_argument("--purpose", default="manual", help="purpose recorded in the holder line")
    parser.add_argument("--task", default=None, help="kanban task id recorded in the holder line")
    parser.add_argument(
        "--reclaim-stale",
        dest="reclaim_stale",
        action="store_true",
        help="take over a holder line whose owner died (prints a warning)",
    )
    args = parser.parse_args(argv)

    lock = BenchLock(
        purpose=args.purpose, task_id=args.task, path=args.path, reclaim_stale=args.reclaim_stale
    )
    if args.action == "acquire":
        try:
            with lock:
                print(f"holding {lock.path} as: {lock.holder.raw}")
                return 0
        except BenchStale as exc:
            print(str(exc), file=sys.stderr)
            return 5
    if args.action == "require":
        try:
            holder = require(lock)
        except BenchNotHeld as exc:
            print(str(exc), file=sys.stderr)
            return 4
        print(f"LOCK-HELD {holder.raw}")
        return 0

    free, holder = lock.status()
    state = lock.state
    print(f"path   : {lock.path}")
    print(f"state  : {state.upper()}")
    print(f"free   : {free}")
    print(f"holder : {holder.raw or '<none>'}")
    if state == "stale":
        print("         -> stale metadata; take over with --reclaim-stale")
    cli = bench_lock_cli()
    print(f"helper : {cli or '<scripts/mt3000-bench/bench-lock.sh not installed>'}")
    if args.action == "check":
        return 0 if state == "free" else 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
