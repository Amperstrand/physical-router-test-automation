"""Unit tests for scripts/run-pr-vm-smoke.sh.

The ssh/docker/git/go/curl/python3/kill layers are mocked with PATH shims
(every external command the script uses resolves via PATH, and the script
invokes ``kill`` as ``env kill`` exactly so a shim can observe it). The
shims coordinate through files in a temp dir and append one line per
invocation to a shared call log, which lets these tests assert:

  * step ordering (fetch → build → lab → mint → config/deploy → register
    → token → pay → ndsctl → assert-cmd),
  * the teardown PID discipline (only QEMU PIDs the script itself started
    are killed; ``stop-poc`` is never invoked; pre-existing lab PIDs are
    protected),
  * config backup/restore semantics (restored on exit, kept with
    --keep-lab),
  * the JSON summary file and exit codes, including the loud
    go-build-failure path that caught PR #549's root-module break.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "run-pr-vm-smoke.sh"

ROUTER_IP = "10.99.99.1"
CLIENT_IP = "10.99.99.100"
HOST_IP = "10.99.99.2"


class SmokeResult:
    def __init__(self, proc: subprocess.CompletedProcess[str], log_path: Path, out_dir: Path):
        self.rc = proc.returncode
        self.stdout = proc.stdout or ""
        self.stderr = proc.stderr or ""
        self.log_path = log_path
        self.out_dir = out_dir

    @property
    def log(self) -> str:
        return self.log_path.read_text()

    @property
    def summary(self) -> dict:
        summaries = list(self.out_dir.glob("pr619-*.json"))
        assert len(summaries) == 1, f"expected exactly one summary JSON, got {summaries}"
        return json.loads(summaries[0].read_text())

    def first_index(self, needle: str) -> int:
        for i, line in enumerate(self.log.splitlines()):
            if needle in line:
                return i
        raise AssertionError(f"needle not found in call log: {needle!r}\nlog:\n{self.log}")

    def kill_lines(self) -> list[str]:
        """All `kill` invocations that are NOT liveness probes (kill -0)."""
        return [
            line
            for line in self.log.splitlines()
            if line.startswith("kill ") and not line.startswith("kill -0")
        ]


def _write_shim(bin_dir: Path, name: str, preamble: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(
        "#!/usr/bin/env bash\n"
        + textwrap.dedent(preamble)
        + textwrap.dedent(body).strip()
        + "\n",
    )
    path.chmod(0o755)


def run_smoke(
    tmp_path: Path,
    *,
    lab_up: bool = False,
    go_fail: bool = False,
    assert_fail: bool = False,
    keep_lab: bool = False,
    assert_cmd: str | None = None,
) -> SmokeResult:
    """Install shims and run the smoke script against them."""
    bin_dir = tmp_path / "bin"
    run_dir = tmp_path / "run"
    out_dir = tmp_path / "out"
    bin_dir.mkdir()
    run_dir.mkdir()
    out_dir.mkdir()
    log_file = tmp_path / "calls.log"
    live_file = tmp_path / "live-pids"
    lab_state = tmp_path / "lab-state"
    go_fail_file = tmp_path / "go-fail"
    assert_fail_file = tmp_path / "assert-fail"
    live_file.write_text("")
    if lab_up:
        lab_state.write_text("up")
        (run_dir / "tollgate.pid").write_text("4242\n")
        (run_dir / "debian-client.pid").write_text("4243\n")
        live_file.write_text("4242\n4243\n")

    paths = {
        "LOG": str(log_file),
        "LIVE": str(live_file),
        "LAB_STATE": str(lab_state),
        "GO_FAIL": str(go_fail_file),
        "ASSERT_FAIL": str(assert_fail_file),
        "RUN_DIR": str(run_dir),
    }
    preamble = "".join(f'{k}="{v}"\n' for k, v in paths.items())
    shim = lambda name, body: _write_shim(bin_dir, name, preamble, body)  # noqa: E731

    shim(
        "git",
        """
        log_call() { printf '%s\\n' "$*" >> "$LOG"; }
        log_call "git $*"
        dir=""
        prev=""
        for a in "$@"; do
          if [ "$prev" = "-C" ]; then dir="$a"; fi
          prev="$a"
        done
        case "$*" in
          *"rev-parse HEAD"*) echo "abc1234def5678deadbeef" ;;
          *checkout*)
            # Materialize the PR tree: module dir + cloud-lab docker context.
            mkdir -p "$dir/src" "$dir/tests/cloud-lab"
            ;;
        esac
        exit 0
        """,
    )
    shim(
        "go",
        """
        log_call() { printf '%s\\n' "$*" >> "$LOG"; }
        log_call "go $*"
        if [ -f "$GO_FAIL" ]; then
          echo "compile error: startingMerchant missing CheckTokenSpendable" >&2
          exit 1
        fi
        prev=""
        for a in "$@"; do
          if [ "$prev" = "-o" ]; then : > "$a"; fi
          prev="$a"
        done
        exit 0
        """,
    )
    shim(
        "python3",
        """
        log_call() { printf '%s\\n' "$*" >> "$LOG"; }
        log_call "python3 $*"
        case "$*" in
          *status-poc*)
            if [ "$(cat "$LAB_STATE" 2>/dev/null)" = "up" ]; then
              echo "== OpenWrt QEMU =="
              echo "running pid=$(cat "$RUN_DIR/tollgate.pid" 2>/dev/null)"
            else
              echo "not running"
            fi
            ;;
          *start-poc*)
            echo 555 > "$RUN_DIR/tollgate.pid"
            echo 666 > "$RUN_DIR/debian-client.pid"
            printf '555\\n666\\n' >> "$LIVE"
            echo "started"
            ;;
        esac
        exit 0
        """,
    )
    shim(
        "docker",
        """
        log_call() { printf '%s\\n' "$*" >> "$LOG"; }
        log_call "docker $*"
        case "$*" in
          *"image inspect"*) exit 0 ;;
          *"inspect -f {{.State.Running}} lab-smoke-mint"*) echo false ;;
          *"run -d --name lab-smoke-mint"*) echo "deadbeefcontainer" ;;
          *"run --rm --network host cloud-lab-client"*)
            echo "quote settled"
            echo "cashuAeyJwcm9vZnMiOiJbeyJpZCI6IjoiMDAiLCJhbW91bnQiOjEyfV19"
            ;;
        esac
        exit 0
        """,
    )
    shim(
        "curl",
        """
        log_call() { printf '%s\\n' "$*" >> "$LOG"; }
        log_call "curl $*"
        case "$*" in
          *":8383/v1/keys"*) echo '{"keysets":[]}' ;;
          *"10.99.99.1:2121"*) echo '{"kind":10021,"content":"TollGate advertisement"}' ;;
        esac
        exit 0
        """,
    )
    shim(
        "kill",
        """
        log_call() { printf '%s\\n' "$*" >> "$LOG"; }
        log_call "kill $*"
        if [ "${1:-}" = "-0" ]; then
          grep -qx "$2" "$LIVE" 2>/dev/null && exit 0
          exit 1
        fi
        exit 0
        """,
    )
    shim(
        "scp",
        """
        log_call() { printf '%s\\n' "$*" >> "$LOG"; }
        log_call "scp $*"
        exit 0
        """,
    )
    shim(
        "sshpass",
        """
        log_call() { printf '%s\\n' "$*" >> "$LOG"; }
        log_call "sshpass $*"
        if [ "$1" = "-p" ]; then shift 2; fi
        exec ssh "$@"
        """,
    )
    shim(
        "ssh",
        """
        log_call() { printf '%s\\n' "$*" >> "$LOG"; }
        log_call "ssh $*"
        cmd="$*"
        case "$cmd" in
          *"echo ssh-ok"*) echo "ssh-ok" ;;
          *"logread"*)
            i=1
            while [ "$i" -le 25 ]; do echo "tollgate-wrt[$i]: sample service log line"; i=$((i+1)); done
            ;;
          *"ndsctl clients"*)
            printf 'Client 0\\nMAC: de:54:4e:91:49:da\\nIP: 10.99.99.100\\nstate=Authenticated\\n'
            ;;
          *"pidof tollgate-wrt | wc -w"*) echo "1" ;;
          *"sessions.json"*)
            if [ -f "$ASSERT_FAIL" ]; then echo "0" >&2; exit 1; fi
            echo "3"
            ;;
          *"example.com"*) : ;;
          *"2121"*) printf '{"kind":1022,"tags":[["allotment","720000"]]}\\n' ;;
        esac
        exit 0
        """,
    )

    if go_fail:
        go_fail_file.write_text("1")
    if assert_fail:
        assert_fail_file.write_text("1")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["VMSMOKE_RUN_DIR"] = str(run_dir)
    env["VMSMOKE_OUT_DIR"] = str(out_dir)
    env["VMSMOKE_ROUTER_PASSWORD"] = "testpw"

    args = ["bash", str(SCRIPT), "--pr", "619"]
    if keep_lab:
        args.append("--keep-lab")
    if assert_cmd is not None:
        args += ["--assert-cmd", assert_cmd]

    proc = subprocess.run(
        args, env=env, capture_output=True, text=True, timeout=120
    )
    return SmokeResult(proc, log_file, out_dir)


# Ordered markers of the marathon sequence (step → observable call).
HAPPY_PATH_ORDER = [
    "fetch --depth=1 origin pull/619/head",          # 1. fetch PR head
    "checkout -q FETCH_HEAD",
    "go build -o",                                  # 1. build
    "virtual-lab.py status-poc",                    # 2. lab status
    "virtual-lab.py start-poc",                     # 2. lab start
    "inspect -f {{.State.Running}} lab-smoke-mint",  # 3. mint container
    "run -d --name lab-smoke-mint",                 # 3. mint run
    "curl -sf --max-time 3 http://10.99.99.2:8383/v1/keys",  # 3. mint health
    "|| cp /etc/tollgate/config.json /etc/tollgate/config.json.vm-smoke-orig",  # 4. backup
    "accepted_mints",                               # 4. point at lab mint
    ":/tmp/tollgate-wrt",                           # 4. deploy binary (scp)
    "/etc/init.d/tollgate-wrt stop",                # 4. clean restart
    "/etc/init.d/tollgate-wrt start",
    "curl -sf --max-time 3 http://10.99.99.1:2121/",  # 4. advertisement poll
    "example.com",                                  # 5. NDS registration
    "run --rm --network host cloud-lab-client",     # 5. mint token
    "Content-Type: text/plain",                     # 5. pay (raw token POST)
    "ndsctl clients",                               # 5. assert Authenticated
]


class TestHappyPath:
    def test_step_ordering_and_exit_code(self, tmp_path):
        result = run_smoke(tmp_path)
        assert result.rc == 0, result.stderr
        indexes = [result.first_index(m) for m in HAPPY_PATH_ORDER]
        assert indexes == sorted(indexes), "marathon step order violated"

    def test_summary_json_all_pass(self, tmp_path):
        result = run_smoke(tmp_path)
        summary = result.summary
        assert summary["overall"] == "PASS"
        assert summary["pr"] == 619
        assert summary["pr_head"] == "abc1234def5678deadbeef"
        assert summary["mint_url"] == f"http://{HOST_IP}:8383"
        assert [s["status"] for s in summary["steps"]] == ["PASS"] * 6
        assert [s["name"] for s in summary["steps"]] == [
            "fetch-and-build",
            "virtual-lab-up",
            "lab-mint",
            "router-deploy",
            "client-pay",
            "assert-cmd",
        ]

    def test_teardown_kills_only_pids_this_run_started(self, tmp_path):
        result = run_smoke(tmp_path)
        # start-poc (shim) recorded 555 (OpenWrt) + 666 (Debian) as ours.
        assert sorted(result.summary["owned_pids"]) == ["555", "666"]
        killed = sorted(line.split()[-1] for line in result.kill_lines())
        # plain kill + -9 fallback for each owned pid, nothing else
        assert killed == ["555", "555", "666", "666"]

    def test_stop_poc_never_invoked(self, tmp_path):
        result = run_smoke(tmp_path)
        assert "stop-poc" not in result.log

    def test_config_restored_on_exit(self, tmp_path):
        result = run_smoke(tmp_path)
        assert "&& cp /etc/tollgate/config.json.vm-smoke-orig /etc/tollgate/config.json" in result.log


class TestTeardownPidDiscipline:
    def test_preexisting_lab_is_never_killed_and_never_stopped(self, tmp_path):
        result = run_smoke(tmp_path, lab_up=True)
        assert result.rc == 0, result.stderr
        # Lab already up: no start-poc, no owned pids, no kills at all.
        assert "virtual-lab.py start-poc" not in result.log
        assert "stop-poc" not in result.log
        assert result.summary["owned_pids"] == []
        assert result.kill_lines() == []
        # Liveness probes on the pre-existing pids are allowed (read-only).
        assert "kill -0 4242" in result.log
        # Config is still restored: the router belongs to another session.
        assert "&& cp /etc/tollgate/config.json.vm-smoke-orig" in result.log


class TestFailurePaths:
    def test_build_failure_fails_loudly_and_aborts(self, tmp_path):
        result = run_smoke(tmp_path, go_fail=True)
        assert result.rc != 0
        # The compiler error is surfaced (this is how #549 was caught).
        assert "compile error: startingMerchant missing CheckTokenSpendable" in result.stderr
        # Nothing after the build may touch the lab/router/mint.
        assert "virtual-lab.py start-poc" not in result.log
        assert "docker" not in result.log
        assert "scp" not in result.log
        assert "Content-Type: text/plain" not in result.log
        summary = result.summary
        assert summary["overall"] == "FAIL"
        statuses = {s["name"]: s["status"] for s in summary["steps"]}
        assert statuses["fetch-and-build"] == "FAIL"
        assert statuses["virtual-lab-up"] == "SKIP"
        assert statuses["client-pay"] == "SKIP"

    def test_assert_cmd_failure_fails_the_run_and_collects_service_log(
        self, tmp_path
    ):
        result = run_smoke(
            tmp_path,
            assert_cmd="grep -c session /tmp/sessions.json",
            assert_fail=True,
        )
        assert result.rc != 0
        assert "logread" in result.log  # last-20 service log lines collected
        summary = result.summary
        assert summary["overall"] == "FAIL"
        statuses = {s["name"]: s["status"] for s in summary["steps"]}
        assert statuses["assert-cmd"] == "FAIL"
        assert statuses["client-pay"] == "PASS"
        assert len(summary["service_log_tail"]) > 0


class TestAssertCmd:
    def test_assert_cmd_runs_on_router_after_payment(self, tmp_path):
        result = run_smoke(
            tmp_path, assert_cmd="grep -c session /tmp/sessions.json"
        )
        assert result.rc == 0, result.stderr
        # PR-specific check runs after the NDS Authenticated assertion.
        assert (
            result.first_index("ndsctl clients")
            < result.first_index("sessions.json")
        )
        statuses = {s["name"]: s["status"] for s in result.summary["steps"]}
        assert statuses["assert-cmd"] == "PASS"


class TestKeepLab:
    def test_keep_lab_skips_config_restore_and_pid_kills(self, tmp_path):
        result = run_smoke(tmp_path, keep_lab=True)
        assert result.rc == 0, result.stderr
        # Backup happened (one vm-smoke-orig ssh call) but no restore.
        assert "vm-smoke-orig" in result.log
        assert "&& cp /etc/tollgate/config.json.vm-smoke-orig" not in result.log
        assert result.kill_lines() == []
        assert result.summary["keep_lab"] is True
