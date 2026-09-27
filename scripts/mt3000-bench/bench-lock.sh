#!/usr/bin/env bash
#
# bench-lock.sh — the SINGLE-OWNER lock for the shared GL-MT3000 bench router.
#
# WHY THIS EXISTS (measured 2026-09-24, bench GL-MT3000 @192.168.1.1):
#   The bench was silently rewritten under a running smoke test. A Hermes cron job
#   (`tg-e2e-watcher`, */10, no_agent) piped the feed's alpha5 apk to the router and
#   ran `apk add --allow-untrusted` whenever the box did not carry its pinned build:
#   19:50, 20:05 and 20:10 (the last one from an ORPHAN after its parent was killed).
#   Each revert re-ran alpha5's postinst: it pruned
#   /etc/nftables.d/31-admin-board-not-guest-reachable.nft (`:8090` guest-reachable
#   again), re-randomised the guest SSID, and replaced the build under test - which
#   invalidated a smoke test twice, a published-artifact check once and a curl|bash
#   validation once, and made an operator tell "bench ready" about a build that was gone.
#
#   Root cause class: MORE THAN ONE OWNER, NONE OF THEM COORDINATED. This file makes the
#   bench a single-owner resource: one flock, one named holder, and every router-touching
#   script either runs under this lock or refuses.
#
# HOLDER LINE FORMAT (the convention the incident card names; kept verbatim and extended):
#
#   <profile> pid=<pid> purpose=<purpose> since=<iso8601> task=<id|-> host=<hostname>
#
#   The first four fields are exactly what the manager's own in-flight window wrote
#   (`manager pid=2584919 purpose=curl|bash-pre16-validation since=2026-09-25T00:19:40+02:00`);
#   `task=` and `host=` are appended so a refused caller can see who owns the bench.
#   `purpose` is whitespace-free (spaces are folded to `_`).
#
# SEMANTICS
#   * Mutual exclusion is the flock on the lock file. The kernel drops it when the holder
#     dies — an orphan cannot hold the bench, and a killed holder cannot wedge it.
#   * "held" = some process holds the flock on the lock file.
#   * A holder LINE with no flock behind it is STALE METADATA: free by flock, but we
#     refuse by default and require an explicit `--reclaim-stale`, which prints a warning.
#   * Refusal is the default, not the exception. Waiting needs `--wait N`.
#
# USAGE
#   bench-lock.sh status                       # read-only: is the bench owned, and by whom
#   bench-lock.sh take   [opts]                # take it and HOLD it (blocks until released)
#   bench-lock.sh exec   [opts] -- CMD...      # take it for the lifetime of CMD (primary use)
#   bench-lock.sh require [opts]               # assert WE already hold it (for routers scripts)
#   bench-lock.sh release [--force]            # end the window named by the holder line
#
#   opts: --purpose P --task ID --wait SECONDS --reclaim-stale --profile P --lock PATH
#
# EXIT CODES
#   0 ok | 2 usage | 3 refused: held by another window | 4 not holding (require failed)
#   5 stale holder line: pass --reclaim-stale | 6 lock path unusable
#
# ENV
#   BENCH_LOCK_PATH    override the lock file (default ~/.hermes/state/bench-mt3000.lock)
#   BENCH_PROFILE      holder profile name (default $HERMES_PROFILE, else $USER)
#   BENCH_LOCK_HELD / BENCH_LOCK_HOLDER_PID   set by `take`/`exec`, checked by `require`
#
set -uo pipefail

EX_OK=0
EX_USAGE=2
EX_HELD=3
EX_NO_LOCK=4
EX_STALE=5
EX_LOCKPATH=6

DEFAULT_LOCK_PATH="${HOME}/.hermes/state/bench-mt3000.lock"
HOSTNAME_S="$(hostname -s 2>/dev/null || echo unknown)"

# --------------------------------------------------------------------------- helpers

die() { local rc="$1"; shift; printf 'bench-lock: %s\n' "$*" >&2; exit "$rc"; }
warn() { printf 'bench-lock: WARNING: %s\n' "$*" >&2; }
info() { printf 'bench-lock: %s\n' "$*" >&2; }

bench_lock_path()     { printf '%s\n' "${BENCH_LOCK_PATH:-${DEFAULT_LOCK_PATH}}"; }
bench_lock_profile()  { printf '%s\n' "${BENCH_PROFILE:-${HERMES_PROFILE:-${USER:-unknown}}}"; }
bench_lock_hostname() { printf '%s\n' "${BENCH_HOSTNAME_SHORT:-$HOSTNAME_S}"; }

bench_now_iso() {
  date --iso-8601=seconds 2>/dev/null || date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S%z'
}

# fold whitespace so the line stays parseable
bench_clean_field() { printf '%s' "$1" | tr -s '[:space:]' '_'; }

bench_lock_field() {   # $1=holder line  $2=key
  printf '%s\n' "${1:-}" | tr ' ' '\n' | sed -n "s/^${2}=//p" | head -1
}

bench_lock_profile_of() {   # $1=holder line -> owner profile
  local first
  first=$(printf '%s' "${1:-}" | awk '{print $1}')
  case "$first" in
    *=*) bench_lock_field "$1" profile ;;
    *)   printf '%s\n' "$first" ;;
  esac
}

# purpose is whitespace-free when THIS helper writes the line, but a hand-written holder line
# may contain spaces (observed live: `purpose=pair-set validation (read-only)`), so read
# everything between `purpose=` and ` since=` before falling back to the field parser.
bench_lock_purpose_of() {
  local line="${1:-}" p
  p=$(printf '%s' "$line" | sed -n 's/.*[[:space:]]purpose=\(.*\)[[:space:]]since=.*/\1/p')
  [ -n "$p" ] || p=$(bench_lock_field "$line" purpose)
  printf '%s\n' "$p"
}

bench_read_holder() {   # first line of the lock file, if any
  local lf; lf="$(bench_lock_path)"
  [ -s "$lf" ] || return 0
  head -1 "$lf" 2>/dev/null
}

# Do we (or anyone) hold the flock?  A fresh open file description is used on purpose:
# an inherited fd would always look lockable by us, which is exactly the question here.
bench_flock_held() {
  local lf; lf="$(bench_lock_path)"
  [ -e "$lf" ] || return 1
  exec 8>>"$lf" || return 1          # >> : never truncate the holder line while probing
  if flock -n -x 8 2>/dev/null; then
    flock -u 8 2>/dev/null
    exec 8>&-
    return 1                          # we could take it => nobody held it
  fi
  exec 8>&-
  return 0                            # someone else holds it
}

bench_lock_state() {   # echoes: FREE | HELD | STALE
  local line
  line="$(bench_read_holder)"
  if bench_flock_held; then printf 'HELD\n'; return 0; fi
  if [ -n "$line" ]; then printf 'STALE\n'; return 0; fi
  printf 'FREE\n'
}

bench_pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

bench_holder_description() {   # $1=holder line -> multi-line human description
  local line="$1" pid prof alive
  pid=$(bench_lock_field "$line" pid)
  prof=$(bench_lock_profile_of "$line")
  if bench_pid_alive "$pid"; then alive=yes; else alive=no; fi
  printf 'profile=%s pid=%s alive=%s purpose=%s since=%s task=%s host=%s\n' \
    "${prof:-unknown}" "${pid:-unknown}" "$alive" \
    "$(bench_lock_purpose_of "$line")" \
    "$(bench_lock_field "$line" since)" \
    "$(bench_lock_field "$line" task)" \
    "$(bench_lock_field "$line" host)"
}

# THE REFUSAL. Always carries the holder identity - that is the whole point.
bench_refuse_held() {
  local line; line="$(bench_read_holder)"
  printf 'bench-lock: REFUSED - the MT3000 bench is OWNED by another window.\n' >&2
  printf 'bench-lock: HOLDER %s\n' "${line:-<holder line missing but the flock IS held>}" >&2
  [ -n "$line" ] && printf 'bench-lock: %s\n' "$(bench_holder_description "$line")" >&2
  printf 'bench-lock: do NOT touch the router. Wait for the owner (`bench-lock status`), or ask the operator.\n' >&2
  printf 'bench-lock: (want to wait instead of fail? re-run with --wait SECONDS)\n' >&2
  exit "$EX_HELD"
}

bench_refuse_stale() {
  local line="$1"
  printf 'bench-lock: REFUSED - stale holder line with no flock behind it.\n' >&2
  printf 'bench-lock: HOLDER %s\n' "$line" >&2
  printf 'bench-lock: %s\n' "$(bench_holder_description "$line")" >&2
  printf 'bench-lock: the previous owner died without releasing; the flock itself is FREE.\n' >&2
  printf 'bench-lock: recovery is explicit only: re-run with --reclaim-stale (it prints a warning).\n' >&2
  exit "$EX_STALE"
}

bench_clear_holder_line() {   # truncate the holder line (caller must have decided it is safe)
  local lf; lf="$(bench_lock_path)"
  : > "$lf" 2>/dev/null || true
}

bench_write_holder_line() {   # $1=purpose $2=task  -> prints the line, writes it
  local lf purpose task line
  lf="$(bench_lock_path)"
  purpose="$(bench_clean_field "${1:-ad-hoc}")"
  task="$(bench_clean_field "${2:--}")"
  line="$(bench_lock_profile) pid=$$ purpose=$purpose since=$(bench_now_iso) task=$task host=$(bench_lock_hostname)"
  printf '%s\n' "$line" > "$lf" || die "$EX_LOCKPATH" "cannot write holder line to $lf"
  printf '%s\n' "$line"
}

# --------------------------------------------------------------------------- commands

cmd_status() {
  local lf line state pid
  lf="$(bench_lock_path)"
  line="$(bench_read_holder)"
  state="$(bench_lock_state)"
  printf 'LOCK      %s\n' "$lf"
  case "$state" in
    HELD)
      printf 'STATE     HELD (flock held on this host)\n'
      printf 'HOLDER    %s\n' "$line"
      printf '          %s\n' "$(bench_holder_description "$line")"
      exit "$EX_HELD" ;;
    STALE)
      printf 'STATE     STALE-METADATA (no flock held, but a holder line is present)\n'
      printf 'HOLDER    %s\n' "$line"
      printf '          %s\n' "$(bench_holder_description "$line")"
      printf 'ACTION    recovery is explicit: bench-lock.sh take --reclaim-stale\n'
      exit "$EX_STALE" ;;
    *)
      printf 'STATE     FREE (nobody holds the bench)\n'
      [ -n "$line" ] && printf 'HOLDER    %s (leftover, flock is free)\n' "$line"
      exit "$EX_OK" ;;
  esac
}

# acquire + hold; caller keeps the flock on fd 9
bench_acquire() {   # $1=purpose  $2=task  $3=wait_seconds  $4=reclaim_stale(0/1)
  local lf purpose task wait_s reclaim line deadline
  lf="$(bench_lock_path)"; purpose="$1"; task="$2"; wait_s="$3"; reclaim="$4"
  mkdir -p "$(dirname "$lf")" || die "$EX_LOCKPATH" "cannot create $(dirname "$lf")"
  exec 9>>"$lf" || die "$EX_LOCKPATH" "cannot open lock file $lf"
  deadline=$(( $(date +%s) + wait_s ))
  while :; do
    if flock -n -x 9 2>/dev/null; then break; fi
    if [ "$wait_s" -le 0 ]; then bench_refuse_held; fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      info "timed out after ${wait_s}s waiting for the bench; still held."
      bench_refuse_held
    fi
    line="$(bench_read_holder)"
    info "waiting for the bench (held by: ${line:-<held, holder line missing>})"
    sleep 1
  done
  # we hold the flock; check for stale metadata left by a dead owner
  line="$(bench_read_holder)"
  if [ -n "$line" ]; then
    if [ "$reclaim" = 1 ]; then
      warn "reclaiming a STALE holder line: $line"
      warn "no flock was held behind it, so its owner is gone; you are now the only owner."
      warn "stale-lock recovery is explicit-only by design - confirm no other window is live."
    else
      flock -u 9 2>/dev/null
      exec 9>&-
      bench_refuse_stale "$line"
    fi
  fi
  line="$(bench_write_holder_line "$purpose" "$task")"
  BENCH_LOCK_HELD=1
  BENCH_LOCK_HOLDER_PID=$$
  BENCH_LOCK_PATH="$lf"
  export BENCH_LOCK_HELD BENCH_LOCK_HOLDER_PID BENCH_LOCK_PATH
  export BENCH_LOCK_PURPOSE="$purpose" BENCH_LOCK_TASK="$task"
  printf 'LOCKED: %s\n' "$line"
}

# remove our own holder line if it is still ours (never steal another window's line)
bench_drop_holder_line_if_ours() {
  local line
  line="$(bench_read_holder)"
  [ -n "$line" ] || return 0
  if [ "$(bench_lock_field "$line" pid)" = "$$" ]; then
    bench_clear_holder_line
  fi
}

cmd_take() {   # $1=purpose $2=task $3=wait $4=reclaim $5=hold_seconds (0 = until released)
  local hold="$5" stop=0 deadline
  bench_acquire "$1" "$2" "$3" "$4"
  trap 'stop=1' TERM INT
  trap 'bench_drop_holder_line_if_ours; exit 0' EXIT
  if [ "$hold" -gt 0 ]; then
    info "holding the bench for ${hold}s (auto-release); Ctrl-C to release early"
    deadline=$(( $(date +%s) + hold ))
    while [ "$stop" = 0 ] && [ "$(date +%s)" -lt "$deadline" ]; do sleep 1 & wait $! 2>/dev/null || true; done
  else
    info "holding the bench until 'bench-lock.sh release' or SIGTERM (pid $$)"
    while [ "$stop" = 0 ]; do sleep 1 & wait $! 2>/dev/null || true; done
  fi
  info "releasing the bench"
}

cmd_exec() {   # $1=purpose $2=task $3=wait $4=reclaim ; rest = command
  local purpose="$1" task="$2" wait_s="$3" reclaim="$4"; shift 4
  bench_acquire "$purpose" "$task" "$wait_s" "$reclaim"
  trap 'bench_drop_holder_line_if_ours; exit 130' INT TERM
  # fd 9 (the flock) must NOT leak into the command or its descendants. It is inherited across
  # fork AND exec, so a detached child (e.g. an install launched with setsid) would keep the
  # flock held long after this window closed: the next window is then refused by a holder that
  # `release` cannot even name, because the holder-line pid is gone. Measured in the suite
  # (a deploy window closed, the next status still said HELD). Closing fd 9 in the subshell
  # costs nothing: `require` checks the env marker + a fresh fd, never fd 9.
  ( exec 9>&- ; "$@" )
  local rc=$?
  bench_drop_holder_line_if_ours
  exit "$rc"
}

cmd_require() {   # assert we ALREADY hold the lock (used by router-touching scripts)
  local line lh
  if [ "${BENCH_LOCK_HELD:-0}" != "1" ]; then
    printf 'bench-lock: REFUSED - you are not running under the bench lock.\n' >&2
    lh="$(bench_read_holder)"
    if bench_flock_held; then
      printf 'bench-lock: HOLDER %s\n' "${lh:-<flock held but the holder line is missing>}" >&2
      [ -n "$lh" ] && printf 'bench-lock: %s\n' "$(bench_holder_description "$lh")" >&2
    elif [ -n "$lh" ]; then
      printf 'bench-lock: STALE HOLDER LINE %s\n' "$lh" >&2
    fi
    printf 'bench-lock: router-touching scripts must run as:\n' >&2
    printf 'bench-lock:   bench-lock.sh exec --purpose "<what>" -- <your script>\n' >&2
    printf 'bench-lock:   (or bench-with-lock.sh --purpose "<what>" -- <your script>)\n' >&2
    printf 'bench-lock: nothing was touched.\n' >&2
    exit "$EX_NO_LOCK"
  fi
  line="$(bench_read_holder)"
  if [ "$(bench_lock_field "$line" pid)" != "${BENCH_LOCK_HOLDER_PID:-}" ]; then
    printf 'bench-lock: REFUSED - the holder line names pid=%s but our lock window is pid=%s.\n' \
      "$(bench_lock_field "$line" pid)" "${BENCH_LOCK_HOLDER_PID:-none}" >&2
    printf 'bench-lock: HOLDER %s\n' "$line" >&2
    printf 'bench-lock: another window replaced the holder line; nothing was touched.\n' >&2
    exit "$EX_NO_LOCK"
  fi
  if ! bench_flock_held; then
    printf 'bench-lock: REFUSED - the flock is no longer held (window closed?).\n' >&2
    printf 'bench-lock: HOLDER %s\n' "$line" >&2
    exit "$EX_NO_LOCK"
  fi
  printf 'bench-lock: LOCK-HELD %s\n' "$line" >&2
}

cmd_release() {   # $1=force(0/1)
  local force="$1" line pid prof ours i
  line="$(bench_read_holder)"
  ours="$(bench_lock_profile)"
  if [ -z "$line" ]; then
    if bench_flock_held; then
      warn "no holder line, but the flock IS held by another process; cannot name it - not touching it."
      exit "$EX_HELD"
    fi
    printf 'bench-lock: nothing to release (no holder line, flock free).\n'
    exit "$EX_OK"
  fi
  pid="$(bench_lock_field "$line" pid)"
  prof="$(bench_lock_profile_of "$line")"
  if ! bench_pid_alive "$pid"; then
    printf 'bench-lock: holder pid %s is not alive; clearing the stale holder line.\n' "${pid:-?}"
    bench_clear_holder_line
    if bench_flock_held; then
      warn "the flock is STILL held though - another process owns the bench; reported, not stolen."
      exit "$EX_HELD"
    fi
    exit "$EX_OK"
  fi
  if [ "$prof" != "$ours" ] && [ "$force" != 1 ]; then
    printf 'bench-lock: REFUSED - the holder belongs to profile "%s" (we are "%s").\n' "$prof" "$ours" >&2
    printf 'bench-lock: HOLDER %s\n' "$line" >&2
    printf 'bench-lock: pass --force only if you are certain that window is abandoned.\n' >&2
    exit "$EX_HELD"
  fi
  printf 'bench-lock: RELEASING holder: %s\n' "$line"
  kill -TERM "$pid" 2>/dev/null || true
  for i in $(seq 1 50); do
    bench_flock_held || break
    sleep 0.2
  done
  if bench_flock_held; then
    if [ "$force" = 1 ]; then
      warn "holder did not release after SIGTERM; sending SIGKILL (--force)"
      kill -9 "$pid" 2>/dev/null || true
      sleep 1
    else
      warn "holder did not release after SIGTERM; re-run with --force if it is abandoned."
      exit "$EX_HELD"
    fi
  fi
  [ "$(bench_lock_field "$(bench_read_holder)" pid)" = "$pid" ] && bench_clear_holder_line
  printf 'bench-lock: RELEASED (flock free)\n'
  exit "$EX_OK"
}

usage() {
  sed -n '3,40p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit "${1:-$EX_USAGE}"
}

# --------------------------------------------------------------------------- main

purpose="${BENCH_LOCK_PURPOSE:-ad-hoc}"
task="${BENCH_LOCK_TASK:--}"
wait_s=0
reclaim=0
hold_s=0
force=0

CMD="${1:-}"
[ -n "$CMD" ] || usage
shift || true

POSITIONAL=()
while [ $# -gt 0 ]; do
  case "$1" in
    --purpose) purpose="$2"; shift 2 ;;
    --task)    task="$2"; shift 2 ;;
    --wait)    wait_s="$2"; shift 2 ;;
    --reclaim-stale) reclaim=1; shift ;;
    --hold)    hold_s="$2"; shift 2 ;;
    --profile) BENCH_PROFILE="$2"; export BENCH_PROFILE; shift 2 ;;
    --lock)    BENCH_LOCK_PATH="$2"; export BENCH_LOCK_PATH; shift 2 ;;
    --force)   force=1; shift ;;
    --)        shift; POSITIONAL+=("$@"); break ;;
    -h|--help) usage ;;
    *)         POSITIONAL+=("$1"); shift ;;
  esac
done

case "$CMD" in
  status)  cmd_status ;;
  take|hold|acquire) cmd_take "$purpose" "$task" "$wait_s" "$reclaim" "$hold_s" ;;
  exec|with-lock)    [ "${#POSITIONAL[@]}" -gt 0 ] || die "$EX_USAGE" "exec needs a command" \
                     ; cmd_exec "$purpose" "$task" "$wait_s" "$reclaim" "${POSITIONAL[@]}" ;;
  require) cmd_require ;;
  release) cmd_release "$force" ;;
  state)   bench_lock_state ;;
  -h|--help|help) usage 0 ;;
  *) die "$EX_USAGE" "unknown command '$CMD' (see --help)" ;;
esac
