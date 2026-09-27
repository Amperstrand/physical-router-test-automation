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
#     * a wedged-socket line in the window                                        => rc 13;
#     * the module settles the address but NEVER states that the client is gone   => rc 13, because
#       "no error lines" is not a state change (converge_assert_state_change);
#     * the client RE-APPEARS in nodogsplash's table during the window            => rc 13: the
#       drift dissolved, so the window proves nothing about the zombie path;
#     * the forcing step finds nodogsplash STILL knowing the client               => rc 13: the step
#       forced nothing, and a phase that "passes" without forcing anything is evidence of nothing
#       (the anti-vacuity precondition);
#     * THE MEASURED pre17 CAPTURE, replayed verbatim (ANSI escapes and all)      => rc 13: the real
#       pre17 binary's own lines, captured on the bench on 2026-09-26, through the real readers;
#     * THE FIX'S OWN LOG LINE, replayed verbatim (module PR #595)                => rc 0.
#   Those last two ARE the negative and positive controls: the same step and the same assertions,
#   driven by what the two BINARIES actually logged. Their fixtures live in
#   tests/mt3000-bench/fixtures/ and carry their provenance.
#   The FORCING STEP is covered as real code, not as a stub: the control runs the actual
#   force_client_drift (only the router transport is replaced) and checks that it announces itself
#   as a BENCH ACTION and that it issues the nodogsplash restart at all.
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
# ...and the run's own ANSI normaliser. It lives next to the transport, OUTSIDE the helper block
# this control extracts, and it is load-bearing here: the pre17 fixture is the bench's COLOURED
# output, where the logrus field arrives as `unconfirmed_closes\x1b[0m=2134`. Without it the
# counter parse reads 0 for ever, which is the false pass this control exists to prevent.
STRIP="$(awk '/^strip_ansi\(\) \{/{f=1} f{print} f&&/^\}/{f=0}' "$SCRIPT")"
if [ -z "$HELPERS" ] || [ -z "$PHASE" ] || [ -z "$ASERTS" ] || [ -z "$DECISION" ] || [ -z "$STRIP" ]; then
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
case "$STRIP" in
  *'x1b'*) ;;
  *) printf 'FAIL: the strip_ansi extraction from %s does not look like the normaliser\n' "$SCRIPT" >&2
     exit 2 ;;
esac

# ---- the stand-in transport ------------------------------------------------------------------
#
# SETTLE_MODE selects what the "router" answers. Every mode renders the same windows the phase
# reads, keyed on the pattern that was asked for:
#   * the state-change window (the grep contains "nothing left to deauthorize")
#   * the settle window       (the grep contains "already gone")
#   * the counter window      (the grep contains "unconfirmed")
#   * everything else         (the wedge window, the error window, …)
#
# Two of the modes are not synthetic at all: `pre17_forced_restart` and `fix_forced_restart`
# REPLAY the lines the two binaries actually logged (tests/mt3000-bench/fixtures/), through the
# same readers and the same ANSI normalisation the run uses. Those are the negative and positive
# controls for the forcing step.
CLIENT_MAC="02:11:22:33:44:55"
SETTLE_MODE="converged"
# What nodogsplash answers about the client in this mode: 0 = it does not know it (the drift is
# intact), 1 = it knows it again (the drift dissolved), ? = ndsctl could not be asked.
NDS_KNOWS=0

# The fix's own positive line, verbatim from module PR #595 (src/valve/valve.go) as the module logs
# it: it carries BOTH phrases the phase's state-change assertion looks for, in ONE line, and names
# the MAC in a field. Rendered per read, because the control changes the MAC per case.
STATE_CHANGE_TEXT='Client already gone from NoDogSplash: ndsctl reports that NoDogSplash does not know this client, so there is nothing left to deauthorize — the gate is closed by definition and the session is retired (no retry is armed for a client that is not there)'
state_change_line() {
  printf 'time="2026-09-26T14:44:02+02:00" level=info msg="%s" mac_address="%s" module=valve ndsctl="Client %s not found."\n' \
    "$STATE_CHANGE_TEXT" "$CLIENT_MAC" "$CLIENT_MAC"
}

# the two samples of the counter window, in call order. It is a FILE, not a shell variable: the
# phase reads every window through a command substitution, so a counter kept in a variable would
# increment inside a subshell and vanish — and both samples would look identical.
COUNTER_FILE="$(mktemp "${TMPDIR:-/tmp}/zombie-control.XXXXXX")"
ANCHORED_FILE="$(mktemp "${TMPDIR:-/tmp}/zombie-control-anchored.XXXXXX")"
# ---- the fixture replay (the measured pre17 / fix controls) -----------------------------------
FIXDIR="$HERE/fixtures"
FIXTURE_STEP="${FIXTURE_STEP:-1}"          # lines revealed per read: a live buffer grows between samples
CURSORS="$(mktemp -d "${TMPDIR:-/tmp}/zombie-control-cursors.XXXXXX")"
trap 'rm -f "$COUNTER_FILE" "$ANCHORED_FILE"; rm -rf "$CURSORS"' EXIT
counter_sample() {   # $1 = the counter file to advance
  local n
  n=$(( $(cat "$1" 2>/dev/null || printf 0) + 1 ))
  printf '%s' "$n" > "$1"
  printf '%s' "$n"
}

# The window class a pattern belongs to. One place, so the synthetic dispatch and the replay agree
# on what a caller is asking for.
pattern_class() {
  case "$1" in
    *"nothing left to deauthorize"*) printf 'state' ;;
    *"already gone"*)                printf 'settle' ;;
    *unconfirmed*)                   printf 'counter' ;;
    *)                               printf 'other' ;;
  esac
}

# REPLAY the lines a binary logged, as the run would see them:
#   * the file for the window being read (whole buffer / anchored window);
#   * revealed PROGRESSIVELY, because a live ring buffer carries more lines on every read and the
#     counter assertion's "A == B" only means something if B could have differed;
#   * through the run's OWN strip_ansi, because these are the bench's COLOURED lines: the logrus
#     field is `unconfirmed_closes\x1b[0m=2134`, so without the normaliser the counter reads 0.
fixture_read() {   # $1 = fixture dir, $2 = anchored(0/1), $3 = pattern, $4 = window class
  local dir="$1" anchored="$2" pattern="$3" class="$4" n cur key
  key="$CURSORS/$(basename "$dir").$anchored.$class"
  n=$(( $(cat "$key" 2>/dev/null || printf 0) + FIXTURE_STEP ))
  if [ "$anchored" = 1 ]; then cur="$dir/anchored.log"; else cur="$dir/whole.log"; fi
  printf '%s' "$n" > "$key"
  sed -n "1,${n}p" "$cur" | strip_ansi | grep -iE "$pattern" || true
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
  local anchored="$1" pattern="$2" class sample

  case "$SETTLE_MODE" in
    pre17_forced_restart|fix_forced_restart)
      fixture_read "$FIXDIR/$SETTLE_MODE" "$anchored" "$pattern" "$(pattern_class "$pattern")"
      return
      ;;
  esac

  class="$(pattern_class "$pattern")"
  case "$class" in
    state)
      # The module's own statement about a MAC nodogsplash does not know. A module that merely
      # STOPS logging errors never produces this line — that is the whole point of the assertion.
      case "$SETTLE_MODE" in
        never_settles|no_statement) : ;;
        *) state_change_line ;;
      esac
      ;;
    settle)
      case "$SETTLE_MODE" in
        never_settles) : ;;
        stale_settle_line)
          [ "$anchored" = 0 ] && printf 'Sat Sep 26 10:37:05 tollgate-wrt[6452]: 2026/09/26 10:37:05 Removed expired session for %s\n' "$CLIENT_MAC"
          ;;
        *) printf 'Sat Sep 26 10:44:02 tollgate-wrt[6452]: Reconciled the stale binding of %s: its client is gone, the gate is deauthorised and the session is retired\n' "$CLIENT_MAC" ;;
      esac
      ;;
    counter)
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
box_assert_module_stable() { printf 'BOX CHECK     %-26s (stubbed: only nodogsplash moved, by design)\n' "$1"; }
box_record() { printf 'BOX IDENTITY  %-26s (stubbed in this control)\n' "$1"; }
balance() { printf '{"status":1,"session_active":true,"metric":"bytes","remaining":22010000}'; }
client_leaves_nodsplash() { printf -- '-- stubbed ndsctl deauth of %s\n' "$CLIENT_MAC"; }
# The forcing step asks the box whether nodogsplash still knows the client; the mode decides.
nds_knows_client() { printf '%s' "$NDS_KNOWS"; }
# The forcing step hands a payload to the router transport, and there is no router here: the
# transport RECORDS the payload instead of running it, so the control can check that the step
# really announces itself as a BENCH ACTION and really issues the restart. The two printed lines
# stand in for what a router would have answered.
PAYLOAD_FILE="$CURSORS/forcing-step-payload.sh"
run_on_router() {   # $1 = the payload script the phase just built
  cp -f "$1" "$PAYLOAD_FILE" 2>/dev/null || true
  printf 'router-snapshot: (control) no router: payload recorded, not executed\n'
  printf -- '-- before: nds_pid=22037 (stubbed) clients=1\n-- restart issued. after: nds_pid=22038 (stubbed) clients=0\n'
}
sleep() { :; }   # the control must not wait 135 s per case

# the globals the extracted blocks read (EX_ASSERT is the script's own exit code)
FAILED=0
EX_ASSERT=13
SETTLE_BUDGET=90
SETTLE_WINDOW=45
# PHASE 5b's forcing step, as the run defaults it: the deliberate restart.
FORCE_DRIFT=restart
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
eval "$STRIP"
# shellcheck disable=SC1090
eval "$HELPERS"

# The extracted helpers define their OWN router_log_mark / router_log_since (the real ones, built on
# the router transport), their own client_leaves_nodsplash (a real ndsctl call) and their own
# nds_knows_client (a real box read of nodogsplash's table). Re-assert the stubs AFTER the eval, or
# the phase would go looking for a router in a control that has none.
router_log_grep() { log_window 0 "$1"; }
router_log_mark() { :; }
router_log_since() { log_window 1 "$2"; }
client_leaves_nodsplash() { printf -- '-- stubbed ndsctl deauth of %s\n' "$CLIENT_MAC"; }
nds_knows_client() { printf '%s' "$NDS_KNOWS"; }
box_assert_module_stable() { printf 'BOX CHECK     %-26s (stubbed: only nodogsplash moved, by design)\n' "$1"; }
box_record() { printf 'BOX IDENTITY  %-26s (stubbed in this control)\n' "$1"; }

RC=0
# $1 label  $2 mode  $3 want(ok|fail)  [$4 = the MAC this case's world is about]  [$5 = what
# nodogsplash answers about that MAC: 0 no record, 1 it knows it again, ? unreadable]
run_case() {
  local label="$1" mode="$2" want="$3" out rc
  printf '\n== %s (want: %s)\n' "$label" "$want"
  SETTLE_MODE="$mode"
  CLIENT_MAC="${4:-02:11:22:33:44:55}"
  NDS_KNOWS="${5:-0}"
  printf 0 > "$COUNTER_FILE"
  printf 0 > "$ANCHORED_FILE"
  : > "$PAYLOAD_FILE"
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
# The new directions this card is about: a state change needs the module's OWN statement, and the
# forcing step has to have forced something.
run_case "the module settles the address but never SAYS the client is gone (no state, only silence)" no_statement fail || RC=1
run_case "the client RE-APPEARS in nodogsplash's table during the window (the drift dissolved)" converged fail "02:11:22:33:44:55" 1 || RC=1
run_case "the forcing step finds nodogsplash STILL knowing the client (it forced nothing)" converged fail "02:11:22:33:44:55" 1 || RC=1

# ---- the forcing step itself ------------------------------------------------------------------
#
# The step ran as REAL code in every case above (only the router transport is a stub), and it can be
# called directly here to check the two properties PHASE 5b depends on: it is ATTRIBUTABLE (it
# writes a BENCH ACTION line into the ROUTER's own log before it restarts) and it really issues the
# restart; and it FIRES when it did not force anything.
: > "$PAYLOAD_FILE"
STEP_OUT="$( ( FORCE_DRIFT=restart; NDS_KNOWS=0; FAILED=0; force_client_drift ) 2>&1 )"
if grep -q 'BENCH ACTION' "$PAYLOAD_FILE" && grep -q '/etc/init.d/nodogsplash restart' "$PAYLOAD_FILE"; then
  printf '\n== the forcing step announces itself as a BENCH ACTION and issues the restart\n'
  printf '   ok   - %s BENCH ACTION line(s) in the payload it built, before the restart\n' "$(grep -c 'BENCH ACTION' "$PAYLOAD_FILE")"
else
  printf '\n== the forcing step: NO attributable BENCH ACTION or NO restart in the payload it built\n   FAIL\n'
  sed 's/^/   | /' "$PAYLOAD_FILE"
  RC=1
fi
case "$STEP_OUT" in
  *'ASSERT PASS  PHASE 5b precondition'*) printf '   ok   - and it proves the drift exists (nodogsplash answers: no record for the client)\n' ;;
  *) printf '   FAIL - the precondition did not pass for a box that does not know the client\n'
     printf '%s\n' "$STEP_OUT" | sed 's/^/   | /'; RC=1 ;;
esac
# The payload is a script the ROUTER's own shell runs, so check it parses there — the same check the
# suite applies to the snapshot payload. A syntax error would only ever show up on the bench, in the
# middle of a paid run.
if sh -n "$PAYLOAD_FILE" 2>/dev/null; then
  printf '   ok   - the payload it built passes sh -n\n'
else
  printf '   FAIL - the payload it built does NOT parse (sh -n)\n'; RC=1
fi
if command -v busybox >/dev/null 2>&1; then
  if busybox ash -n "$PAYLOAD_FILE" 2>/dev/null; then
    printf '   ok   - and busybox ash -n (the router own shell)\n'
  else
    printf '   FAIL - the payload does NOT parse under busybox ash\n'; RC=1
  fi
fi
STEP_OUT="$( ( FORCE_DRIFT=restart; NDS_KNOWS=1; FAILED=0; force_client_drift ) 2>&1 )"
case "$STEP_OUT" in
  *'ASSERT FAIL  PHASE 5b precondition'*) printf '   ok   - and it FIRES when nodogsplash still knows the client (nothing was forced)\n' ;;
  *) printf '   FAIL - the precondition did not fire for a box that still knows the client\n'
     printf '%s\n' "$STEP_OUT" | sed 's/^/   | /'; RC=1 ;;
esac
NDS_KNOWS=0

# ---- the SAME step, driven by what the two BINARIES actually logged ---------------------------
#
# These are the negative and positive controls: the fixtures under tests/mt3000-bench/fixtures/ hold
# lines taken VERBATIM from the capture of the forcing step on the bench (pre17) and from the fix's
# own output (module PR #595). The MACs are the ones those capture runs were about, because the
# phase's assertions are MAC-scoped on purpose.
# A missing or renamed fixture must ABORT the control: an empty window would make the pre17 case
# "fail for the wrong reason" and the fix case fail for a reason that has nothing to do with the
# module, and both would still look like a direction that behaved.
for _m in pre17_forced_restart fix_forced_restart; do
  for _w in whole anchored; do
    if [ ! -s "$FIXDIR/$_m/$_w.log" ]; then
      printf 'FAIL: fixture missing or empty: %s\n' "$FIXDIR/$_m/$_w.log" >&2
      exit 2
    fi
  done
done
printf "\n---- the measured pre17 window (the negative control: it MUST fire) ----\n"
run_case "pre17, verbatim capture: the close loop keeps escalating the client after the forced restart" \
  pre17_forced_restart fail "02:11:22:33:77:0c" || RC=1
printf "\n---- the fix's own line (the positive control: it must NOT fire) ----\n"
run_case "the fix, verbatim: the module states the client is gone and nothing is left to deauthorize" \
  fix_forced_restart ok "aa:bb:cc:dd:ee:60" || RC=1

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
    NDS_KNOWS=0
    eval "$ASERTS"
    eval "$VACUOUS_HELPERS"
    router_log_grep() { log_window 0 "$1"; }
    router_log_mark() { :; }
    router_log_since() { log_window 1 "$2"; }
    client_leaves_nodsplash() { printf -- '-- stubbed ndsctl deauth of %s\n' "$CLIENT_MAC"; }
    nds_knows_client() { printf '%s' "$NDS_KNOWS"; }
    box_assert_module_stable() { printf 'BOX CHECK     %-26s (stubbed)\n' "$1"; }
    box_record() { printf 'BOX IDENTITY  %-26s (stubbed)\n' "$1"; }
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
