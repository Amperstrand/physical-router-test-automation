#!/usr/bin/env bash
#
# restart-guard-control.sh — prove the second-purchase run's box-identity guard CAN fail.
#
# WHY THIS EXISTS
#   On 2026-09-26 a nodogsplash + tollgate-wrt restart landed MID-RUN on the bench MT3000 (an
#   out-of-band remediation of a wedged ndsctl socket). Both daemons came back on new pids, the
#   module's tracked sessions were orphaned, and the transcript went on looking like a product
#   result. Nothing in the run noticed. `second-purchase-e2e.sh` now pins the box at PHASE 0
#   (router uptime, nodogsplash uptime, nodogsplash pid, tollgate-wrt pid) and re-checks all
#   four at every phase boundary, failing with exit 15.
#
#   A guard that has never been seen red is decoration. This control extracts the guard block
#   FROM THE SCRIPT (it never copies the logic, so it cannot drift), drives it with a stubbed
#   transport, and asserts both directions:
#     * a stable box does NOT false-fire (rc 0), and
#     * each way the box can move DOES fire (rc 15):
#         - nodogsplash restarted (new pid + uptime reset)
#         - tollgate-wrt restarted (new pid)
#         - the router rebooted (uptime goes backwards)
#         - ndsctl wedged / unreadable (an unreadable box is not a stable box)
#
# No router, no ssh, no bench lock, no network. Run it from anywhere:
#   tests/mt3000-bench/restart-guard-control.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${1:-$HERE/../../scripts/mt3000-bench/second-purchase-e2e.sh}"
[ -f "$SCRIPT" ] || { echo "FAIL: no such script: $SCRIPT" >&2; exit 2; }

BLOCK="$(awk '/^# ------.* box identity \(the restart guard\)/{f=1} /^# ------.* PHASE 0/{f=0} f' "$SCRIPT")"
if [ -z "$BLOCK" ]; then
  echo "FAIL: could not extract the guard block from $SCRIPT (did the markers move?)" >&2
  exit 2
fi
printf 'extracted %s lines of guard code from %s\n' "$(printf '%s\n' "$BLOCK" | wc -l)" "$SCRIPT"

# ---- the stand-in transport: BOX_MODE selects what the "router" answers ------------------
BOX_MODE=stable
run_on_router() {
  case "$BOX_MODE" in
    stable)        printf 'router-snapshot: appended 1 bytes to /dev/null\nuptime_s=5000.00\nnds_uptime_raw=7m 33s\nnds_pid=22037\nwrt_pid=21353\n' ;;
    nds_restarted) printf 'uptime_s=5040.00\nnds_uptime_raw=45s\nnds_pid=99999\nwrt_pid=21353\n' ;;
    wrt_restarted) printf 'uptime_s=5040.00\nnds_uptime_raw=8m 30s\nnds_pid=22037\nwrt_pid=31777\n' ;;
    rebooted)      printf 'uptime_s=12.00\nnds_uptime_raw=9s\nnds_pid=431\nwrt_pid=444\n' ;;
    wedged)        printf 'uptime_s=5040.00\nnds_uptime_raw=\nnds_pid=22037\nwrt_pid=21353\n' ;;
  esac
}
router_log_grep() { printf 'Sat Sep 26 10:21:18 nodogsplash[6452]: Handler for termination caught signal 15\n'; }

# the globals the guard reads (EX_BOX_CHANGED is the script's own exit code)
EX_BOX_CHANGED=15
FAILED=0
# shellcheck disable=SC1090
eval "$BLOCK"

RC=0
run_case() {   # $1 label  $2 mode  $3 want(stable|broken)
  local label="$1" mode="$2" want="$3" rc
  printf '\n== %s (want: %s)\n' "$label" "$want"
  BOX_MODE=stable
  box_record "phase0-baseline"      # recorded in THIS shell: the guard's state must persist
  BOX_MODE="$mode"
  ( box_assert_stable "after-phase2-exhaust" )   # subshell, so the script's exit 15 is rc
  rc=$?
  printf -- '-- rc=%s\n' "$rc"
  if [ "$want" = stable ] && [ "$rc" = 0 ];  then printf '   ok   - did not false-fire\n'; return 0; fi
  if [ "$want" = broken ] && [ "$rc" = 15 ]; then printf '   ok   - fired, exit 15\n';     return 0; fi
  printf '   FAIL - wanted want=%s, got rc=%s\n' "$want" "$rc"
  return 1
}

run_case "a box that does not move is not flagged"        stable        stable || RC=1
run_case "nodogsplash restarted mid-run is caught"        nds_restarted broken || RC=1
run_case "tollgate-wrt restarted mid-run is caught"       wrt_restarted broken || RC=1
run_case "a router reboot mid-run is caught"              rebooted      broken || RC=1
run_case "an unreadable ndsctl (wedged) is caught"        wedged        broken || RC=1

printf '\n'
if [ "$RC" = 0 ]; then
  printf 'restart-guard-control: PASS (1 no-false-fire + 4 can-fail)\n'
else
  printf 'restart-guard-control: FAIL\n'
fi
exit "$RC"
