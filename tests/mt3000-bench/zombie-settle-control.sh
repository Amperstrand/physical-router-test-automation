#!/usr/bin/env bash
#
# zombie-settle-control.sh — prove the PHASE 5b convergence assertions CAN fail.
#
# WHY THIS EXISTS
#   On 2026-09-26 (module pin 2796d96c) a client that left nodogsplash left the module with an
#   UNRETIRABLE session: `ndsctl deauth` answered `Client <mac> not found.` and exited 1, the
#   module read that exit status as an unconfirmed close, retried it at the sweep cadence for ever
#   (`unconfirmed_closes` 113 -> 193 -> 195), never retired the session, logged the false warning
#   "this client may still hold open, unmetered access", and drove ndsctl until its socket died —
#   after which a PAID purchase could not be authorised at all (state=PAID, wallet +1 sat,
#   access_granted never true). `second-purchase-e2e.sh` now asserts, in PHASE 5b, that the module
#   CONVERGES on an address nodogsplash no longer knows — the phase runs where a client LEAVES with
#   its PAID allotment still open, which is the state that reproduced the leak.
#
#   An assertion that has never been seen red is decoration. This control extracts the helpers and
#   the PHASE 5b block FROM THE SCRIPT (so it cannot drift out of sync with it), drives them with a
#   stubbed transport and a stubbed `sleep`, and asserts both directions:
#     * a module that settles the address, stops escalating and keeps ndsctl answerable => rc 0;
#     * a module that never settles it (the measured leak)                       => rc 13;
#     * a settle line that PREDATES the phase's marker (buy#1's exhaustion)       => rc 13, because
#       the phase anchors its window; a whole-buffer read would have called that convergence. The
#       anti-vacuity check at the end runs exactly that comparison and fails the control if an
#       un-anchored read ever becomes able to reject the stale line;
#     * a module whose unconfirmed-closes total keeps climbing                    => rc 13;
#     * a module that escalates the client again after it left                    => rc 13;
#     * a module that claims unmetered access for a MAC nodogsplash does not know => rc 13;
#     * an ndsctl socket that stops answering during the settle window            => rc 13;
#     * a wedged-socket line in the window                                        => rc 13.
#
#   The extraction itself is checked both ways: it must BE the settle phase, and it must not have
#   swallowed PHASE 5 (whose `buy "$TOKEN_2" "buy#2"` reached the extracted block through the
#   blanket end marker this control used to carry, killing it on an unbound TOKEN_2 before it
#   asserted anything).
#
# No router, no ssh, no bench lock, no network, no waiting (sleep is stubbed):
#   tests/mt3000-bench/zombie-settle-control.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${1:-$HERE/../../scripts/mt3000-bench/second-purchase-e2e.sh}"
[ -f "$SCRIPT" ] || { echo "FAIL: no such script: $SCRIPT" >&2; exit 2; }

HELPERS="$(awk '/^# ------.* zombie-session convergence helpers/{f=1} /^# ------.* box identity \(the restart guard\)/{f=0} f' "$SCRIPT")"
# The settle phase is delimited by its OWN pair of separator lines — `PHASE 5b` opens it and
# `end of PHASE 5b` closes it — never by a neighbouring phase's `say`. The blanket end marker
# this used to carry (`say "MODULE LOG`) spanned the phase AFTER its own as well, so the control
# ran its assertions over a neighbour's text and died on that neighbour's unbound TOKEN_2
# (PHASE 5's `buy "$TOKEN_2" "buy#2"`) before it asserted anything — a control that errors out
# proves nothing about what it drives.
#
# The terminator is consumed by its OWN rule (`next`), not by falling through to the print: both
# markers end in `... PHASE 5b`, so the start pattern matches the END line as well, and a form
# whose rules merely happened to evaluate in this order re-opens the block on its own terminator
# and swallows everything after it. With `next` the end line can only CLOSE the block.
PHASE="$(awk '
  /^# ------.* end of PHASE 5b$/ { f = 0; next }
  /^# ------.* PHASE 5b$/        { f = 1 }
  f
' "$SCRIPT")"
# Only the assertion helpers, and only the functions the phase uses: the run's argument parsing
# and its client setup sit between them, and sourcing those would execute them here.
ASERTS="$(awk '/^assert_eq\(\) \{/{f=1} /^# ------.* args/{f=0} f' "$SCRIPT")"
# ...and the script's own verdict decision, so a failed assertion becomes the script's own exit
# code instead of a printout this control would have to interpret itself.
DECISION="$(awk '/^if \[ "\$FAILED" -ne 0 \]; then/{f=1} f&&/^fi$/{print; f=0} f' "$SCRIPT")"
if [ -z "$HELPERS" ] || [ -z "$PHASE" ] || [ -z "$ASERTS" ] || [ -z "$DECISION" ]; then
  echo "FAIL: could not extract the blocks from $SCRIPT (did the markers move?)" >&2
  exit 2
fi
# An extraction that lands on the wrong text is SILENT: the control still runs, just against
# something other than the settle phase, and every direction below is then meaningless. So check
# both ends of the window — it must BE the settle phase, and it must not have swallowed a
# neighbouring one.
case "$PHASE" in
  *converge_assert_settled*) ;;
  *) printf 'FAIL: the PHASE 5b extraction is not the settle phase (no converge_assert_settled in it)\n' >&2
     printf '      the start separator in %s moved onto some other comment\n' "$SCRIPT" >&2
     exit 2 ;;
esac
case "$PHASE" in
  *'TOKEN_2'*|*'buy#2 answered'*)
    printf 'FAIL: the PHASE 5b extraction swallowed a neighbouring phase (PHASE 5 got in).\n' >&2
    printf '      the start/end separators in %s are no longer unique.\n' "$SCRIPT" >&2
    exit 2 ;;
esac
case "$PHASE" in
  *'MODULE LOG'*|*'VERDICT'*)
    printf 'FAIL: the PHASE 5b extraction runs past its end separator into the run transcript.\n' >&2
    printf '      the end separator in %s no longer terminates the block.\n' "$SCRIPT" >&2
    exit 2 ;;
esac
printf 'extracted %s lines of helpers, %s lines of PHASE 5b and %s lines of assertions from %s\n' \
  "$(printf '%s\n' "$HELPERS" | wc -l)" "$(printf '%s\n' "$PHASE" | wc -l)" \
  "$(printf '%s\n' "$ASERTS" | wc -l)" "$SCRIPT"

# ---- the stand-in transport ------------------------------------------------------------------
#
# SETTLE_MODE selects what the "router" answers. Every mode renders the same three windows the
# phase reads, keyed on the pattern that was asked for:
#   * the settle window  (the grep contains "already gone")
#   * the counter window (the grep contains "unconfirmed")
#   * everything else    (the wedge window, the error window, …)
CLIENT_MAC="02:11:22:33:44:55"
SETTLE_MODE="converged"

# the two samples of the counter window, in call order. It is a FILE, not a shell variable: the
# phase reads every window through a command substitution, so a counter kept in a variable would
# increment inside a subshell and vanish — and both samples would look identical.
COUNTER_FILE="$(mktemp "${TMPDIR:-/tmp}/zombie-control.XXXXXX")"
ANCHORED_FILE="$(mktemp "${TMPDIR:-/tmp}/zombie-control-anchored.XXXXXX")"
trap 'rm -f "$COUNTER_FILE" "$ANCHORED_FILE"' EXIT
counter_sample() {   # $1 = the counter file to advance
  local n
  n=$(( $(cat "$1" 2>/dev/null || printf 0) + 1 ))
  printf '%s' "$n" > "$1"
  printf '%s' "$n"
}

# The two log readers the phase uses, driven from one function so they can only differ in ONE
# place: whether a line that PREDATES the phase's marker is visible.
#
#   router_log_grep   = the whole buffer  (what a naive "is the module settled?" read returns)
#   router_log_since  = only the lines after the anchor the phase wrote into the router's log
#
# The `stale_settle_line` mode exists because the difference is not academic: buy#1's exhaustion
# already logs "Removed expired session for <mac>" BEFORE the phase starts, so a whole-buffer read
# reports the address as settled without the module having done anything. That control case must
# FAIL, and it is the reason the phase anchors its window.
log_window() {   # $1 = 1 when the read is anchored to the marker, else 0   $2 = the pattern asked for
  local anchored="$1" pattern="$2" sample
  case "$pattern" in
    *"already gone"*)
      case "$SETTLE_MODE" in
        never_settles) : ;;
        stale_settle_line)
          [ "$anchored" = 0 ] && printf 'Sat Sep 26 10:37:05 tollgate-wrt[6452]: 2026/09/26 10:37:05 Removed expired session for %s\n' "$CLIENT_MAC"
          ;;
        *) printf 'Sat Sep 26 10:44:02 tollgate-wrt[6452]: Reconciled the stale binding of %s: its client is gone, the gate is deauthorised and the session is retired\n' "$CLIENT_MAC" ;;
      esac
      ;;
    *unconfirmed*)
      # Two independent samples, because the phase reads the SAME question through both readers:
      # the cumulative total comes from the whole buffer, how many escalations name this client
      # comes from the anchored window. Tying them to one counter would make the stub's answer
      # depend on how many reads the phase happens to make.
      if [ "$anchored" = 1 ]; then sample="$(counter_sample "$ANCHORED_FILE")"; else sample="$(counter_sample "$COUNTER_FILE")"; fi
      case "$SETTLE_MODE" in
        climbing)      printf 'Sat Sep 26 10:44:10 tollgate-wrt[6452]: level=error msg="Gate close NOT confirmed for client" unconfirmed_closes=%s mac_address="%s"\n' "$((190 + sample))" "$CLIENT_MAC" ;;
        names_client)  printf 'Sat Sep 26 10:44:10 tollgate-wrt[6452]: level=error msg="Gate close NOT confirmed for client" unconfirmed_closes=193 mac_address="%s"\n' "$CLIENT_MAC"
                       # the SECOND read of the anchored window carries the escalation again — the
                       # leak this catches: an escalation that lands INSIDE the settle window
                       if [ "$sample" -ge 2 ]; then printf 'Sat Sep 26 10:45:00 tollgate-wrt[6452]: level=error msg="Gate close NOT confirmed for client" unconfirmed_closes=193 mac_address="%s"\n' "$CLIENT_MAC"; fi ;;
        *) : ;;
      esac
      ;;
    *)
      case "$SETTLE_MODE" in
        wedged_line)   printf 'Sat Sep 26 10:44:20 nodogsplash[6452]: Socket is not ready for communication : Bad file descriptor\n' ;;
        unmetered)     printf 'Sat Sep 26 10:44:20 tollgate-wrt[6452]: ERROR: could not close the gate for %s: exit status 1 — the client may still hold open, unmetered access\n' "$CLIENT_MAC" ;;
        *) : ;;
      esac
      ;;
  esac
}

router_log_grep() { log_window 0 "$1"; }

# The phase writes a marker into the router's log from the router (`logger`), which the control
# cannot do and does not need to: the marker's ONLY job is to separate the two reads above, and
# `anchored` already does that.
router_log_mark() { :; }

router_log_since() { log_window 1 "$2"; }

box_identity() {
  if [ "$SETTLE_MODE" = wedged_socket ]; then
    printf 'router-snapshot: appended 1 bytes to /dev/null\nuptime_s=5040.00\nnds_uptime_raw=\nnds_pid=22037\nwrt_pid=21353\n'
    return
  fi
  printf 'router-snapshot: appended 1 bytes to /dev/null\nuptime_s=5040.00\nnds_uptime_raw=7m 33s\nnds_pid=22037\nwrt_pid=21353\n'
}

box_field() { printf '%s\n' "$1" | sed -n "s/^$2=//p" | head -1; }

# The phase runs the same placeholders a real run does; none of them may touch anything here.
box_assert_stable() { printf 'BOX CHECK     %-26s (stubbed in this control)\n' "$1"; }
balance() { printf '{"status":1,"session_active":true,"metric":"bytes","remaining":22010000}'; }
client_leaves_nodsplash() { printf -- '-- stubbed ndsctl deauth of %s\n' "$CLIENT_MAC"; }
sleep() { :; }   # the control must not wait 135 s per case

# the globals the extracted blocks read (EX_ASSERT is the script's own exit code)
FAILED=0
EX_ASSERT=13
SETTLE_BUDGET=90
SETTLE_WINDOW=45
# The run's own timestamp: PHASE 5b builds its log-anchor token from it.
TS=20260926T104402Z
# The phase text is EXTRACTED from the run, so it can name the run's own placeholders. Should a
# future edit ever widen that extraction, the guards ABOVE must be what fails: they inspect the
# text and run before any of it is evaluated. Dying on an unbound variable here instead would
# kill the control before it asserted anything — which is exactly how it first broke. PHASE 5's
# `buy "$TOKEN_2" "buy#2"` is the neighbour's reference this defends against.
TOKEN_1="${TOKEN_1:-}"; TOKEN_2="${TOKEN_2:-}"
say() { printf '\n########## %s ##########\n' "$*"; }
# shellcheck disable=SC1090
eval "$ASERTS"
# shellcheck disable=SC1090
eval "$HELPERS"

# The extracted helpers define their OWN router_log_mark / router_log_since (the real ones, built on
# the router transport) and their own client_leaves_nodsplash (a real ndsctl call). Re-assert the
# stubs AFTER the eval, or the phase would go looking for a router in a control that has none.
router_log_grep() { log_window 0 "$1"; }
router_log_mark() { :; }
router_log_since() { log_window 1 "$2"; }
client_leaves_nodsplash() { printf -- '-- stubbed ndsctl deauth of %s\n' "$CLIENT_MAC"; }

RC=0
run_case() {   # $1 label  $2 mode  $3 want(ok|fail)
  local label="$1" mode="$2" want="$3" out rc
  printf '\n== %s (want: %s)\n' "$label" "$want"
  SETTLE_MODE="$mode"
  printf 0 > "$COUNTER_FILE"
  printf 0 > "$ANCHORED_FILE"
  # The phase runs in a subshell (its FAILED cannot leak) and ends with the script's OWN verdict
  # decision, so the exit code is the exit code the real run would produce.
  out="$( ( FAILED=0; eval "$PHASE"; eval "$DECISION" ) 2>&1 )"
  rc=$?
  printf '%s\n' "$out" | sed 's/^/   | /'
  printf -- '-- rc=%s (want %s)\n' "$rc" "$want"
  if [ "$want" = ok ] && [ "$rc" = 0 ]; then printf '   ok   - did not false-fire\n'; return 0; fi
  if [ "$want" = fail ] && [ "$rc" = "$EX_ASSERT" ]; then
    printf '   ok   - fired\n'
    printf '%s\n' "$out" | grep -q 'ASSERT FAIL' || printf '   note - no ASSERT FAIL line (see rc)\n'
    return 0
  fi
  printf '   FAIL - wanted want=%s, got rc=%s\n' "$want" "$rc"
  return 1
}

run_case "a module that settles the address, stays quiet and keeps ndsctl answering" converged    ok   || RC=1
run_case "the measured leak: the address is never settled"                        never_settles  fail || RC=1
run_case "only a PRE-window settle line exists (buy#1's exhaustion) — the anchor must reject it" stale_settle_line fail || RC=1
run_case "the counter climbs across the settle window"                           climbing       fail || RC=1
run_case "the module escalates this client again after it left"                  names_client   fail || RC=1
run_case "the module claims unmetered access for a MAC nodogsplash does not know" unmetered      fail || RC=1
run_case "ndsctl stops answering during the settle window (wedged socket)"       wedged_socket  fail || RC=1
run_case "a wedged-socket line appears in the settle window"                     wedged_line    fail || RC=1

# ---- anti-vacuity: the read this assertion was proposed in ----------------------------------
#
# The settle assertion was first proposed reading the WHOLE logread buffer with no anchor, so
# "settled" could match a line written BEFORE the phase — buy#1's own exhaustion logs
# "Removed expired session for <mac>", so it reported convergence for a module that did nothing.
# That shape is not merely looser than the anchored read; it is unfalsifiable in the one
# direction that matters. This keeps the anchor's justification machine-checked on the SAME
# code, with only the settle read un-anchored: it must NOT be able to tell, i.e. it must report
# the stale line as convergence. If it ever starts catching it, the anchor is not what makes the
# third case fail and this control is no longer evidence for it.
#
# The anchored read lives in converge_assert_settled, which the extraction above takes from the
# helpers block — so the mutation targets the helpers, and the phase that calls them is unchanged.
SETTLE_READ_ANCHORED='router_log_since "$token" "$(settled_pattern)"'
SETTLE_READ_WHOLE='router_log_grep "$(settled_pattern)"'
VACUOUS_HELPERS="${HELPERS//"$SETTLE_READ_ANCHORED"/"$SETTLE_READ_WHOLE"}"
if [ "$VACUOUS_HELPERS" = "$HELPERS" ]; then
  printf '\n== anti-vacuity: SKIPPED - the helpers no longer contain the anchored settle read\n'
else
  printf '\n== anti-vacuity: the SAME phase with an UN-ANCHORED settle read (the shape it was proposed in)\n'
  SETTLE_MODE=stale_settle_line
  printf 0 > "$COUNTER_FILE"
  printf 0 > "$ANCHORED_FILE"
  # A fresh subshell, exactly like run_case: the mutated helpers must not leak into the cases
  # above (or into a future one), and the stubs must be re-asserted after the helpers redefine
  # the real router readers.
  out="$( (
    FAILED=0
    eval "$ASERTS"
    eval "$VACUOUS_HELPERS"
    router_log_grep() { log_window 0 "$1"; }
    router_log_mark() { :; }
    router_log_since() { log_window 1 "$2"; }
    client_leaves_nodsplash() { printf -- '-- stubbed ndsctl deauth of %s\n' "$CLIENT_MAC"; }
    eval "$PHASE"
    eval "$DECISION"
  ) 2>&1 )"
  rc=$?
  printf '%s\n' "$out" | sed 's/^/   | /'
  printf -- '-- rc=%s (want 0: an un-anchored read cannot see that the line PREDATES the phase)\n' "$rc"
  if [ "$rc" = 0 ]; then
    printf '   ok   - vacuity reproduced: the un-anchored read calls a pre-window line convergence\n'
  else
    printf '   FAIL - the un-anchored read rejected the stale line; the anchor is not what makes the\n'
    printf '          third case fail, so this control is not evidence for the anchor.\n'
    RC=1
  fi
fi

printf '\n'
if [ "$RC" = 0 ]; then
  printf 'PASS: PHASE 5b fails in every direction it is supposed to, and does not false-fire.\n'
else
  printf 'FAIL: at least one control direction did not behave as documented.\n'
fi
exit "$RC"
