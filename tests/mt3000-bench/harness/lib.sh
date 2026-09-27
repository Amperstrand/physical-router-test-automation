#!/usr/bin/env bash
# Shared helpers for the offline bench-harness tests (tests/mt3000-bench/run-tests.sh).
#
# Nothing here touches a router: the harness is a throw-away directory that stands in for
# the router's filesystem, plus PATH doubles for ssh/scp/apk (harness/bin/).
#
# HERMETICITY OF THE BENCH LOCK (why this file owns a "production lock" concept at all)
#   The suite takes and releases bench locks to assert ownership semantics, so it must NEVER do
#   that on the production lock file (~/.hermes/state/bench-mt3000.lock). A live bench run holds
#   exactly that file, and rewriting or clearing its holder line invalidates the run under it.
#   Two consequences:
#     * 2026-09-26 the suite was SIGTERMed with no verdict while the bench was busy — a suite that
#       cannot run whenever the bench is in use is a trap for every worker and for CI;
#     * 2026-09-27, reproduced in a sandbox: pointed at the production lock shape with a live flock
#       held, the pre-fix suite DELETED the lock file from its first case — the holder line was
#       GONE and the path was takeable again (flock FREE) while the live run's flock sat on an
#       unlinked inode, i.e. a second window could take the bench under a run that believed it
#       owned it. The pre-fix suite did not BLOCK in that experiment (it re-creates the file after
#       unlinking, so it never contends): the reachable harm is the destroyed holder metadata and
#       the defeated flock.
#   So: the suite owns a private lock INSIDE its workdir, `bench_assert_private_lock` refuses to
#   start against the production path (exit 90, loud), every command it runs is BOUNDED
#   (`run_cmd`), and `prod_lock_fingerprint` lets the end of the run PROVE the production file —
#   holder line included — is exactly what it was at the start.
set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HARNESS_DIR/../../.." && pwd)"
BENCH_DIR="$REPO_ROOT/scripts/mt3000-bench"
BENCH_LOCK="$BENCH_DIR/bench-lock.sh"
BENCH_WITH_LOCK="$BENCH_DIR/bench-with-lock.sh"
BENCH_DEPLOY="$BENCH_DIR/bench-deploy-apk.sh"
ROUTER_TOUCHER="$HARNESS_DIR/router-touching-script.sh"
PRIVATE_LOCK_PROBE="$HARNESS_DIR/private-lock-probe.sh"

TESTS_RUN=0; TESTS_FAILED=0; TESTS_SKIPPED=0; TESTS_TIMEOUTS=0
_CUR="(no test)"

t_begin() { _CUR="$1"; TESTS_RUN=$((TESTS_RUN + 1)); printf '\n== %s %s\n' "$(printf '%02d' "$TESTS_RUN")" "$1"; }
pass() { printf '   ok   - %s\n' "$1"; }
fail() { TESTS_FAILED=$((TESTS_FAILED + 1)); printf '   FAIL - %s\n' "$1"; }
skip() { TESTS_SKIPPED=$((TESTS_SKIPPED + 1)); printf '   SKIP - %s\n' "$1"; }

check_rc() {   # $1 desc, $2 expected rc, $3 actual rc
  if [ "$2" = "$3" ]; then pass "$1 (rc=$3)"; else fail "$1: expected rc=$2, got rc=$3"; fi
}

# check_contains / check_not_contains are pure-bash literal substring matches ON PURPOSE.
#
# They used to be `printf '%s' "$haystack" | grep -qF -- "$needle"`, which is a RACE under
# `set -o pipefail`: `grep -q` exits at the FIRST match, the writer is then killed by SIGPIPE
# (rc 141), and pipefail reports the pipeline as FAILED even though the match succeeded — so the
# failure message prints a haystack that visibly CONTAINS the needle. Measured on the frozen
# suite: 30 spurious failures in 20000 iterations of that exact pipeline (0.15% per check), i.e.
# roughly one spurious FAIL per five suite runs across this suite's ~150 assertions. That is the
# same "cannot trust the suite" trap as an unbounded lock wait, one layer down. The `case` forms
# below fork nothing, cannot be raced, and are 0/20000. (They are also stricter for a multi-line
# needle: grep -F would treat it as several alternative patterns.)
check_contains() {   # $1 desc, $2 needle, $3 haystack
  case "$3" in
    *"$2"*) pass "$1" ;;
    *)
      fail "$1: output does not contain '$2'"
      printf '        --- output was ---\n%s\n        ------------------\n' "$3"
      ;;
  esac
}

check_not_contains() {   # $1 desc, $2 needle, $3 haystack
  case "$3" in
    *"$2"*)
      fail "$1: output unexpectedly contains '$2'"
      printf '        --- output was ---\n%s\n        ------------------\n' "$3"
      ;;
    *) pass "$1" ;;
  esac
}

check_eq() {   # $1 desc, $2 expected, $3 actual
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1: expected '$2', got '$3'"; fi
}

# Every command the suite runs is BOUNDED, so a case that blocks — a contended lock, a wrapper
# waiting on one, a helper that never returns — produces a FAIL and a verdict, never a hang.
# rc 124 (timeout) can never equal an expected rc here (2/3/4/5/6/7/8/…), so the case fails with
# the command named, and the total is reported in the verdict line.
RUN_CMD_TIMEOUT_DEFAULT=60
run_cmd() {   # OUT=..., RC=...
  local t="${BENCH_TEST_CMD_TIMEOUT:-$RUN_CMD_TIMEOUT_DEFAULT}"
  if command -v timeout >/dev/null 2>&1; then
    OUT="$(timeout "$t" "$@" 2>&1)"; RC=$?
  else
    OUT="$("$@" 2>&1)"; RC=$?
  fi
  if [ "$RC" = 124 ]; then
    TESTS_TIMEOUTS=$((TESTS_TIMEOUTS + 1))
    OUT="$OUT
[harness] TIMEOUT: '$*' did not finish within ${t}s (BENCH_TEST_CMD_TIMEOUT) — counted as a failure, not a hang"
    printf '[harness] TIMEOUT after %ss: %s\n' "$t" "$*" >&2
  fi
}

# Wait for a pid, bounded. 0 = it exited, 124 = it is still alive after $2 seconds.
wait_bounded() {   # $1=pid $2=seconds
  local pid="$1" secs="$2" i
  i=0
  while [ "$i" -lt "$secs" ]; do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 1
    i=$((i + 1))
  done
  kill -0 "$pid" 2>/dev/null && return 124
  return 0
}

# Start a bench-lock holder in the background (the suite's own windows) and set HOLDER_PID.
# It must stay a CHILD of the caller's shell (a command substitution would orphan the job in a
# subshell and `wait` could then never reap it), so it reports through HOLDER_PID, not stdout.
start_holder() {   # $1=profile $2=out file ; rest = bench-lock args
  local prof="$1" out="$2"; shift 2
  BENCH_PROFILE="$prof" "$BENCH_LOCK" "$@" >"$out" 2>&1 &
  HOLDER_PID=$!
}

# End a holder without ever letting it wedge the suite: bounded wait, then TERM, then KILL.
stop_holder() {   # $1=pid $2=what it was
  local pid="$1" what="$2" bound="${BENCH_TEST_WAIT_BOUND:-20}"
  wait_bounded "$pid" "$bound" && { wait "$pid" 2>/dev/null; return 0; }
  kill -TERM "$pid" 2>/dev/null || true
  if ! wait_bounded "$pid" 5; then
    fail "$what: still alive ${bound}s+5s after release — SIGKILLed (a holder that will not exit is a failure, not a hang)"
    kill -9 "$pid" 2>/dev/null || true
    wait_bounded "$pid" 5 || true
  fi
}

# ---------------------------------------------------------------- harness "router"

harness_root_new() {   # $1=dir [$2=installed payload] [$3=pkg version]
  local d="$1" payload="${2:-}" ver="${3:-tollgate-wrt-0.6.0_alpha9-r0}"
  rm -rf "$d"
  mkdir -p "$d/tmp" "$d/usr/bin" "$d/etc/tollgate" "$d/var/log" "$d/var/lib/apk" "$d/payload-map" "$d/bin"
  cp -f "$HARNESS_DIR/bin/apk" "$d/bin/apk"; chmod +x "$d/bin/apk"
  if [ -n "$payload" ]; then install -D -m 0755 "$payload" "$d/usr/bin/tollgate-wrt"; fi
  printf '%s\n' "$ver" > "$d/var/lib/apk/installed"
  : > "$d/var/log/apk.log"
  printf '{\n  "package_path": "false",\n  "version": "1"\n}\n' > "$d/etc/tollgate/install.json"
}

harness_payload_map() {   # $1=apk  $2=payload file  -> teach the stand-in apk the mapping
  local h; h="$(sha256sum "$1" | cut -d' ' -f1)"
  printf '%s\n' "$2" > "$BENCH_HARNESS_ROOT/payload-map/$h"
}

harness_installed_sha() { sha256sum "$BENCH_HARNESS_ROOT/usr/bin/tollgate-wrt" 2>/dev/null | cut -d' ' -f1; }
harness_apk_installs() { grep -c 'Running .apk add' "$BENCH_HARNESS_ROOT/var/log/apk.log" 2>/dev/null | head -1; }
harness_installed_pkg() { head -1 "$BENCH_HARNESS_ROOT/var/lib/apk/installed" 2>/dev/null; }

apk_static_find() {
  local c
  for c in "${BENCH_APK_STATIC:-}" "$HOME/.cache/apk-v3/apk.static" "$(command -v apk.static 2>/dev/null || true)"; do
    [ -n "$c" ] && [ -x "$c" ] && { printf '%s' "$c"; return 0; }
  done
  return 1
}

payload_extract() {   # $1=apk  $2=out file
  local apk="$1" out="$2" static dest
  static="$(apk_static_find)" || return 1
  dest="$(mktemp -d)"
  "$static" extract --allow-untrusted --destination "$dest" "$apk" >/dev/null 2>&1 || { rm -rf "$dest"; return 1; }
  [ -f "$dest/usr/bin/tollgate-wrt" ] || { rm -rf "$dest"; return 1; }
  cp -f "$dest/usr/bin/tollgate-wrt" "$out"
  rm -rf "$dest"
  return 0
}

lock_holder_line() { head -1 "$BENCH_LOCK_PATH" 2>/dev/null; }
lock_kill_all() {   # end any holder this suite started (its OWN lock, never the production one)
  BENCH_PROFILE="$BENCH_PROFILE" "$BENCH_LOCK" release --force --lock "$BENCH_LOCK_PATH" >/dev/null 2>&1 || true
  BENCH_PROFILE="$BENCH_PROFILE" "$BENCH_LOCK" release --force --lock "$BENCH_LOCK_PATH" >/dev/null 2>&1 || true
}

summary() {
  printf '\n==== %s: tests=%s failed=%s skipped=%s timeouts=%s ====\n' \
    "$(basename "$0")" "$TESTS_RUN" "$TESTS_FAILED" "$TESTS_SKIPPED" "$TESTS_TIMEOUTS"
  [ "$TESTS_FAILED" -eq 0 ] || return 1
  [ "$TESTS_TIMEOUTS" -eq 0 ] || return 1
  return 0
}

# ---------------------------------------------------------------- the production bench lock
#
# The suite must not touch this file. It is named in one place so tests and docs cannot drift,
# and every reader here is READ-ONLY (realpath -m / stat / sha256sum / head never create it).

PROD_LOCK_DEFAULT="${HOME}/.hermes/state/bench-mt3000.lock"
prod_lock_path() { printf '%s\n' "${BENCH_PROD_LOCK_PATH:-$PROD_LOCK_DEFAULT}"; }

_bench_realpath() { realpath -m "$1" 2>/dev/null || printf '%s' "$1"; }

# THE GUARD. Returns 90 (a code no bench case uses) with a loud banner when the lock this suite
# would operate on is not private. Wired into run-tests.sh before the first case, and driven
# directly by the suite's negative control through harness/private-lock-probe.sh — so the guard
# itself is proven to fire, not assumed to.
bench_assert_private_lock() {   # $1 = the private root the lock must live under (the workdir)
  local root="${1:-}" lf prod lp rp why=""
  lf="${BENCH_LOCK_PATH:-}"
  prod="$(prod_lock_path)"
  lp="$(_bench_realpath "$lf")"
  rp="$(_bench_realpath "$prod")"
  if [ -z "$lf" ]; then
    why="BENCH_LOCK_PATH is unset/empty — bench-lock.sh would fall back to the PRODUCTION lock ($rp)"
  elif [ "$lp" = "$rp" ]; then
    why="the suite's lock path IS the production bench lock ($rp)"
  elif [ -n "$root" ]; then
    case "$lp" in
      "$(_bench_realpath "$root")"/*) ;;
      *) why="the suite's lock path is not inside the suite workdir ($root): $lp" ;;
    esac
  fi
  if [ -n "$why" ]; then
    printf '\n' >&2
    printf '!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n' >&2
    printf '!!! FATAL: THIS SUITE IS NOT HERMETIC — REFUSING TO RUN\n' >&2
    printf '!!! %s\n' "$why" >&2
    printf '!!! These cases take and release bench locks to assert ownership semantics. Against\n' >&2
    printf '!!! the production lock that would rewrite (or clear) the holder line of a LIVE bench\n' >&2
    printf '!!! run, and it would block behind one whenever the bench is busy.\n' >&2
    printf '!!! Fix: run tests/mt3000-bench/run-tests.sh and leave BENCH_LOCK_PATH unset — the\n' >&2
    printf '!!! harness sets it to $WORK/bench-mt3000.lock. Point BENCH_PROD_LOCK_PATH at the\n' >&2
    printf '!!! production path if your environment needs a different one named.\n' >&2
    printf '!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n' >&2
    return 90
  fi
  printf 'hermetic: suite lock=%s  production lock (NOT used by this suite)=%s\n' "$lp" "$rp"
  return 0
}

# A fingerprint of the production lock: bytes, identity and the holder line. Compared before and
# after the run, that is the standing proof the suite left a live run's lock alone.
prod_lock_fingerprint() {
  local f; f="$(prod_lock_path)"
  if [ ! -e "$f" ]; then printf 'ABSENT\t-\t-\t-\t-\n'; return 0; fi
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)" \
    "$(stat -c '%i' "$f" 2>/dev/null)" \
    "$(stat -c '%s' "$f" 2>/dev/null)" \
    "$(stat -c '%Y' "$f" 2>/dev/null)" \
    "$(head -1 "$f" 2>/dev/null)"
}

prod_lock_holder_line() { printf '%s' "$(prod_lock_fingerprint | cut -f5)"; }

# Read-only flock probe on the production lock. Never creates the file (an absent lock is
# reported as ABSENT rather than created by the probe).
prod_lock_flock_state() {
  local f; f="$(prod_lock_path)"
  [ -e "$f" ] || { printf 'ABSENT\n'; return 0; }
  ( exec 8>>"$f" 2>/dev/null || { printf 'UNKNOWN\n'; exit 0; }
    if flock -n -x 8 2>/dev/null; then printf 'FREE\n'; else printf 'HELD\n'; fi
    exec 8>&- ) 2>/dev/null
}
