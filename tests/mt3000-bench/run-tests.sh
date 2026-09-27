#!/usr/bin/env bash
#
# run-tests.sh — the bench-lock + bench-deploy negative-control suite (NO ROUTER).
#
# Proves, offline:
#   * lock exclusivity            — a second owner cannot take the bench
#   * holder identity in the error — every refusal names who owns the bench
#   * the wrapper refuses while held, and the router is untouched
#   * stale-lock recovery only with an explicit flag, and it warns
#   * a deploy refuses without the lock (and installs nothing)
#   * a deploy rotates stale staged apks and names its artifact
#   * the post-install identity check PASSES on the artifact's real payload bytes
#   * ... and FAILS LOUDLY when a different build is what actually got installed
#     (install build A while naming build B — the card's evidence gate)
#   * a substituted apk already staged under our name is refused BEFORE installing
#   * the second-purchase e2e is dry-run by default, refuses a paid run it cannot pay for,
#     and is refused (naming the holder) while another window owns the bench — without
#     ever probing the router or taking a transcript
#   * the router-side snapshot refuses without a window, and its payload (render mode) is
#     accepted by both `sh -n` and BusyBox `ash -n` — the router's own shell
#   * the token tool mints nothing without --yes and refuses to check an absent token
#
# The "router" is a throw-away directory; ssh/scp/apk are PATH test doubles in
# harness/bin/. The remote scripts are the production ones — only TG_TMP/TG_BIN/TG_ETC/
# TG_APKLOG are exported into the harness root by the ssh double.
#
# Fixtures: two REAL aarch64 .apk files (their usr/bin/tollgate-wrt payloads are extracted
# with apk.static). Override with BENCH_TEST_APK_A / BENCH_TEST_APK_B. If they or
# apk.static are missing the hardware-identity tests SKIP with a reason (never a false pass).
#
# HERMETIC BENCH LOCK. This suite takes and releases bench locks to assert ownership semantics,
# so it must NEVER operate on the lock a real bench run holds
# (~/.hermes/state/bench-mt3000.lock). It owns a private lock inside a mktemp workdir, asserts
# that BEFORE the first case (harness/lib.sh: bench_assert_private_lock, exit 90 and loud when the
# path is the production one or outside the workdir), and proves at the end that the production
# file — holder line included — is byte-identical to what it found. So it can run WHILE a live
# run holds the bench: it neither blocks behind that lock nor rewrites its holder metadata.
# Ledger: 2026-09-26 a suite run was SIGTERMed with no verdict while the bench was busy, and the
# pre-fix suite had unbounded waits of its own (bare `wait "$HOLDER1"` / `wait "$HOLD2"` on its own
# holders, plus a family-pattern `pkill -f 'sleep 20'` that could kill a LIVE run's process).
# Reproduced 2026-09-27 in a sandbox whose lock path IS the production shape, with another process
# holding the flock: the PRE-FIX suite (HEAD 9e7cdb36; exit 0, tests=23, 20 s) DELETED the lock
# file from its first case, leaving the holder line GONE and the path takeable again (flock FREE)
# while the live run's flock sat on an unlinked inode — a second window could then take the bench
# under a run that believed it owned it. The same invocation under the gate below exits 90 and
# leaves the file byte-identical. Every lock invocation is therefore bounded twice over — `--lock`
# naming the suite's own file, `--wait` on the lock itself, and a per-command timeout in run_cmd —
# so a contended lock is a FAIL with the holder named, never a hang, and rc 124 is counted and
# reported in the verdict line.
#
# The harness's own assertions were ALSO a trap, and are fixed here: check_contains /
# check_not_contains were `printf | grep -qF` pipelines under `set -o pipefail`, where `grep -q`
# exits at the first match and the writer is then SIGPIPEd (rc 141) — the pipeline reports FAILED
# while the match succeeded, so the failure message prints a haystack that plainly CONTAINS the
# needle (measured: 30 spurious failures / 20000 iterations, i.e. ~1 in 5 suite runs). They are
# pure-bash `case` matches now. See harness/lib.sh for the measurement.
#
# The live-run guards are covered here too, so they cannot rot: the box-identity (restart) guard
# and the PHASE 5b zombie-settle assertions inside scripts/mt3000-bench/second-purchase-e2e.sh
# are driven by their own offline controls (restart-guard-control.sh, zombie-settle-control.sh),
# and each control is then run against a MUTATED copy of that script which must make it go red.
# A control wired in but unable to fail is decoration.
#
# env: BENCH_TEST_WORKDIR    reuse a workdir (default: a fresh mktemp -d)
#      BENCH_TEST_CMD_TIMEOUT  per-command bound in seconds (default 60)
#      BENCH_LOCK_WAIT        --wait passed to every lock invocation (default 5)
#      BENCH_PROD_LOCK_PATH   the production lock this suite must not touch
#      BENCH_TEST_APK_A / _B  fixtures for the identity cases
#
# usage: tests/mt3000-bench/run-tests.sh
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/harness/lib.sh"

WORK="${BENCH_TEST_WORKDIR:-$(mktemp -d "${TMPDIR:-/tmp}/bench-lock-tests.XXXXXX")}"
export BENCH_LOCK_PATH="$WORK/bench-mt3000.lock"

# ================================================ 0. this suite is HERMETIC (private lock)
# THE GATE RUNS FIRST — before this script creates or writes ANYTHING. A workdir whose lock file
# IS the production one (reachable: BENCH_TEST_WORKDIR=~/.hermes/state) would otherwise let a
# live bench run have its holder line rewritten under it, so refuse loudly (exit 90) instead.
bench_assert_private_lock "$WORK" || exit 90
PROD_LOCK="$(prod_lock_path)"
PROD_BEFORE="$(prod_lock_fingerprint)"
PROD_FLOCK_BEFORE="$(prod_lock_flock_state)"

mkdir -p "$WORK"
export BENCH_PROFILE="test-harness"
export BENCH_ROUTER_PW_FILE="$WORK/pw"
# the harness ssh double ignores this value; never put a real lab credential in a repo
printf 'harness-dummy-pw\n' > "$BENCH_ROUTER_PW_FILE"; chmod 600 "$BENCH_ROUTER_PW_FILE"
export BENCH_HARNESS_ROOT="$WORK/router"
export PATH="$HERE/harness/bin:$PATH"

# Every lock invocation names the SUITE'S lock explicitly (`--lock`) instead of trusting the
# environment alone, and every one of them carries a bounded `--wait`: a contended lock is then a
# refusal naming the holder (a FAIL), never an unbounded wait. BENCH_LOCK_PATH stays exported so
# the wrappers and the scripts under test resolve the same private file.
export BENCH_LOCK_WAIT="${BENCH_LOCK_WAIT:-5}"      # --wait on every lock invocation
export BENCH_TEST_HOLD="${BENCH_TEST_HOLD:-30}"     # how long the suite's own holders live
LOCKOPTS=(--lock "$BENCH_LOCK_PATH" --wait "$BENCH_LOCK_WAIT")

APK_A="${BENCH_TEST_APK_A:-$HOME/.tg-e2e/cache/tollgate-wrt_0.6.0_alpha5_aarch64_cortex-a53_portalcu102ln004.apk}"
APK_B="${BENCH_TEST_APK_B:-$HOME/tollgate-pre16-validation/published/tollgate-wrt_0.6.0_alpha4_pre16_aarch64_cortex-a53.apk}"
SHA_A=""; SHA_B=""; PAY_A=""; PAY_B=""

printf 'bench-lock tests: workdir=%s\nlock=%s\n' "$WORK" "$BENCH_LOCK_PATH"

printf 'production lock (NEVER operated on by this suite): %s\n  state at start: %s\n  holder line:    %s\n' \
  "$PROD_LOCK" "$PROD_FLOCK_BEFORE" "$(prod_lock_holder_line)"

t_begin "the harness REFUSES to run against the production bench lock (negative control)"
run_cmd env BENCH_LOCK_PATH="$PROD_LOCK" "$PRIVATE_LOCK_PROBE" "$WORK"
check_rc "the production lock path is refused (exit 90)" 90 "$RC"
check_contains "the refusal is loud" "NOT HERMETIC" "$OUT"
check_contains "the refusal names the production lock" "$PROD_LOCK" "$OUT"
check_contains "the refusal names the failure mode" "holder line of a LIVE bench" "$OUT"
run_cmd env BENCH_LOCK_PATH="" "$PRIVATE_LOCK_PROBE" "$WORK"
check_rc "an empty lock path is refused (exit 90)" 90 "$RC"
check_contains "the refusal names the fallback it would take" "unset/empty" "$OUT"
run_cmd env BENCH_LOCK_PATH="$(dirname "$WORK")/not-ours.lock" "$PRIVATE_LOCK_PROBE" "$WORK"
check_rc "a lock outside the private workdir is refused (exit 90)" 90 "$RC"
check_contains "the refusal names the workdir rule" "not inside the suite workdir" "$OUT"
run_cmd env BENCH_LOCK_PATH="$BENCH_LOCK_PATH" "$PRIVATE_LOCK_PROBE" "$WORK"
check_rc "the suite's own private lock is accepted" 0 "$RC"
check_contains "and it says which file it will use" "hermetic: suite lock=$BENCH_LOCK_PATH" "$OUT"

# ...and the same guard on the REAL entry point, through the one misconfiguration that reaches it:
# a workdir whose bench-mt3000.lock IS the production lock. The gate must fire before any case and
# before this script writes anything into that workdir.
t_begin "the entry point refuses a workdir whose lock IS the production lock (exit 90)"
run_cmd env BENCH_TEST_WORKDIR="$(dirname "$PROD_LOCK")" bash "$HERE/run-tests.sh"
check_rc "run-tests.sh refuses to run against the production lock (exit 90)" 90 "$RC"
check_contains "the refusal is loud" "NOT HERMETIC" "$OUT"
check_contains "and names the production lock" "$PROD_LOCK" "$OUT"
check_not_contains "and ran no case at all" "== 01" "$OUT"
check_eq "and left the production lock untouched" "$PROD_BEFORE" "$(prod_lock_fingerprint)"

# ---------------------------------------------------------------- fixtures
have_fixtures=0
if [ -f "$APK_A" ] && [ -f "$APK_B" ]; then
  mkdir -p "$WORK/payloads"
  if payload_extract "$APK_A" "$WORK/payloads/A" && payload_extract "$APK_B" "$WORK/payloads/B"; then
    PAY_A="$WORK/payloads/A"; PAY_B="$WORK/payloads/B"
    SHA_A="$(sha256sum "$APK_A" | cut -d' ' -f1)"
    SHA_B="$(sha256sum "$APK_B" | cut -d' ' -f1)"
    if [ "$(sha256sum "$PAY_A" | cut -d' ' -f1)" = "$(sha256sum "$PAY_B" | cut -d' ' -f1)" ]; then
      printf 'fixtures: A and B have identical payloads — using them would prove nothing\n'
    else
      have_fixtures=1
      printf 'fixtures: A=%s (payload %s)\n          B=%s (payload %s)\n' \
        "$(basename "$APK_A")" "$(sha256sum "$PAY_A" | cut -c1-16)" \
        "$(basename "$APK_B")" "$(sha256sum "$PAY_B" | cut -c1-16)"
    fi
  fi
fi
[ "$have_fixtures" = 1 ] || printf 'fixtures: MISSING (apk.static or the two fixture apks) — identity tests will SKIP\n'

new_router() {   # fresh harness router root with both payloads mapped
  harness_root_new "$BENCH_HARNESS_ROOT" "$PAY_B"
  harness_payload_map "$APK_A" "$PAY_A"
  harness_payload_map "$APK_B" "$PAY_B"
}

# The scripts under test by section 4 (paths live here, not in the shared harness lib).
SECOND_PURCHASE="$BENCH_DIR/second-purchase-e2e.sh"
ROUTER_SNAPSHOT="$BENCH_DIR/router-snapshot.sh"
BENCH_TOKEN="$BENCH_DIR/bench-token.py"

deploy_in_window() {   # $1=purpose ; rest = deploy args
  local purpose="$1"; shift
  run_cmd "$BENCH_WITH_LOCK" "${LOCKOPTS[@]}" --purpose "$purpose" -- "$BENCH_DEPLOY" "$@"
}

# =============================================================================== 1. lock
# Every case here runs against the suite's OWN lock file (section 0) and is bounded twice over:
# `--wait` on the lock invocation and a per-command timeout inside run_cmd. A live bench run can
# therefore neither block this suite nor be disturbed by it, and a contended lock is a FAIL that
# names the holder rather than a hang.
t_begin "two owners cannot both take the bench"
rm -f "$BENCH_LOCK_PATH"
start_holder alpha "$WORK/holder1.out" take "${LOCKOPTS[@]}" --purpose "holder-test" --task t_aaa --hold "$BENCH_TEST_HOLD"
HOLDER1="$HOLDER_PID"
sleep 1
if ! kill -0 "$HOLDER1" 2>/dev/null; then
  fail "the first holder did not stay up: $(head -3 "$WORK/holder1.out" 2>/dev/null | tr '\n' ' ') — the exclusivity cases below would prove nothing"
fi
run_cmd env BENCH_PROFILE=beta "$BENCH_LOCK" take "${LOCKOPTS[@]}" --purpose "second-window"
check_rc "second take refused" 3 "$RC"
check_contains "refusal names the holder profile" "HOLDER alpha pid=" "$OUT"
check_contains "refusal names the holder purpose" "purpose=holder-test" "$OUT"
check_contains "refusal names the holder task"   "task=t_aaa" "$OUT"
check_contains "refusal says the bench is OWNED" "OWNED by another window" "$OUT"
check_contains "the refusal took a BOUNDED wait (never an unbounded one)" "timed out after ${BENCH_LOCK_WAIT}s waiting for the bench" "$OUT"
HL="$(lock_holder_line)"
check_contains "holder line carries profile+pid" "alpha pid=" "$HL"
check_contains "holder line carries purpose+since" "purpose=holder-test since=" "$HL"

t_begin "the router-touching WRAPPER refuses while the bench is held (negative control A)"
run_cmd "$BENCH_WITH_LOCK" "${LOCKOPTS[@]}" --purpose "second-window" -- true
check_rc "bench-with-lock refused" 3 "$RC"
check_contains "wrapper refusal names the holder" "HOLDER alpha pid=" "$OUT"
printf '   raw refusal: %s\n' "$(printf '%s' "$OUT" | head -2 | tr '\n' ' ')"

t_begin "a second ROUTER-TOUCHING SCRIPT is refused, naming the holder (negative control A2)"
run_cmd "$ROUTER_TOUCHER"
check_rc "router-touching script refused" 4 "$RC"
check_contains "script refusal names the holder line" "HOLDER alpha pid=" "$OUT"
check_contains "script refusal names the holder profile" "profile=alpha" "$OUT"
check_contains "script did not touch the router" "not running under the bench lock" "$OUT"
check_not_contains "script never reached its router commands" "ROUTER-TOUCHED" "$OUT"
printf '   raw refusal (stderr):\n%s\n' "$(printf '%s' "$OUT" | sed 's/^/        /')"

t_begin "the deploy helper refuses without the lock and installs nothing"
new_router
BEFORE="$(harness_installed_sha)"
run_cmd "$BENCH_DEPLOY" --apk "$APK_B" --sha256 "$SHA_B" --install-timeout 30
check_rc "deploy without a lock refused" 4 "$RC"
check_contains "refusal explains the required invocation" "not running under the bench lock" "$OUT"
check_eq "router binary untouched" "$BEFORE" "$(harness_installed_sha)"
check_eq "no apk install was attempted" "0" "$(harness_apk_installs)"

t_begin "release ends the window; the bench becomes takeable again"
run_cmd "$BENCH_LOCK" release "${LOCKOPTS[@]}" --force
check_rc "release ok" 0 "$RC"
stop_holder "$HOLDER1" "the first holder"
run_cmd "$BENCH_LOCK" status "${LOCKOPTS[@]}"
check_rc "status FREE after release" 0 "$RC"
check_contains "status reports FREE" "STATE     FREE" "$OUT"
run_cmd env BENCH_PROFILE=beta "$BENCH_LOCK" status "${LOCKOPTS[@]}"
check_eq "a different profile sees the same FREE state" "0" "$RC"

t_begin "stale holder line (no flock behind it) needs an EXPLICIT reclaim flag"
printf 'ghost pid=999999 purpose=dead-since-tuesday since=2020-01-01T00:00:00+00:00 task=- host=nowhere\n' > "$BENCH_LOCK_PATH"
run_cmd "$BENCH_LOCK" status "${LOCKOPTS[@]}"
check_rc "status reports STALE-METADATA" 5 "$RC"
check_contains "status names the stale holder" "STATE     STALE-METADATA" "$OUT"
run_cmd env BENCH_PROFILE=beta "$BENCH_LOCK" take "${LOCKOPTS[@]}" --purpose "should-refuse" --hold 1
check_rc "take refused on stale metadata" 5 "$RC"
check_contains "refusal shows the stale holder line" "HOLDER ghost pid=999999" "$OUT"
check_contains "refusal names the explicit recovery flag" "--reclaim-stale" "$OUT"
check_not_contains "auto-recovery did NOT happen silently" "LOCKED" "$OUT"
run_cmd env BENCH_PROFILE=beta "$BENCH_LOCK" take "${LOCKOPTS[@]}" --purpose "explicit-reclaim" --hold 1 --reclaim-stale
check_rc "explicit reclaim succeeds" 0 "$RC"
check_contains "reclaim prints a warning" "WARNING: reclaiming a STALE holder line" "$OUT"
check_contains "reclaim restates the safety rule" "explicit-only by design" "$OUT"
: > "$BENCH_LOCK_PATH"

# =============================================================================== 2. deploy
if [ "$have_fixtures" != 1 ]; then
  for n in "deploy rotates a stale staged apk, names its artifact and verifies the installed identity" \
           "verify-only matches the installed build, and fails loudly when naming a different one" \
           "a substituted apk already staged under our name is REFUSED before installing" \
           "install build A while naming build B => loud identity MISMATCH" \
           "--refuse-foreign-staged refuses instead of rotating" \
           "install.json package_path revert bomb is refused, then cleared on request"; do
    t_begin "$n"; skip "no fixture apks / apk.static on this host"
  done
else
  t_begin "deploy rotates a stale staged apk, names its artifact and verifies the installed identity"
  new_router
  cp -f "$APK_A" "$BENCH_HARNESS_ROOT/tmp/tg-alpha5.apk"        # the 2026-09-24 revert bomb shape
  cp -f "$APK_A" "$BENCH_HARNESS_ROOT/tmp/tollgate-wrt-old.apk"
  deploy_in_window "t09-deploy" --apk "$APK_B" --sha256 "$SHA_B" --install-timeout 60
  check_rc "deploy succeeded" 0 "$RC"
  check_contains "artifact is NAMED with path+sha256" "ARTIFACT name=$(basename "$APK_B")" "$OUT"
  check_contains "artifact sha256 is the named one" "sha256=$SHA_B" "$OUT"
  check_contains "payload identity is named too" "PAYLOAD usr/bin/tollgate-wrt" "$OUT"
  check_contains "stale apk rotated aside" "ROTATED tg-alpha5.apk -> tg-alpha5.apk.rotated-" "$OUT"
  check_contains "second stale apk rotated aside" "ROTATED tollgate-wrt-old.apk ->" "$OUT"
  check_contains "staged under a sha-named artifact" "STAGED-VERIFIED /tmp/bench-${SHA_B:0:12}-" "$OUT"
  check_contains "installed identity verified" "INSTALLED VERIFIED" "$OUT"
  check_contains "identity names both hashes" "artifact_payload_sha256=$(sha256sum "$PAY_B" | cut -d' ' -f1)" "$OUT"
  check_eq "router binary IS the artifact payload" "$(sha256sum "$PAY_B" | cut -d' ' -f1)" "$(harness_installed_sha)"
  check_eq "an install actually happened" "1" "$(harness_apk_installs)"
  check_eq "no apk left staged under its original name" "0" "$(ls "$BENCH_HARNESS_ROOT/tmp/tg-alpha5.apk" 2>/dev/null | wc -l | tr -d ' ')"
  check_eq "rotated bytes are preserved, not deleted" "2" "$(ls "$BENCH_HARNESS_ROOT"/tmp/*.rotated-* 2>/dev/null | wc -l | tr -d ' ')"

  t_begin "verify-only matches the installed build, and fails loudly when naming a different one"
  deploy_in_window "t10-verify" --apk "$APK_B" --sha256 "$SHA_B" --verify-only
  check_rc "verify-only against the installed build: MATCH" 0 "$RC"
  check_contains "verify-only reports the identity" "IDENTITY VERIFIED (verify-only)" "$OUT"
  check_eq "verify-only installed nothing" "1" "$(harness_apk_installs)"
  deploy_in_window "t10-verify-mismatch" --apk "$APK_A" --sha256 "$SHA_A" --verify-only
  check_rc "verify-only naming build A fails loudly" 8 "$RC"
  check_contains "mismatch is reported" "IDENTITY MISMATCH (verify-only)" "$OUT"
  check_contains "mismatch shows the router's bytes" "$(harness_installed_sha)" "$OUT"
  check_contains "mismatch shows the named artifact's bytes" "$(sha256sum "$PAY_A" | cut -d' ' -f1)" "$OUT"

  t_begin "a substituted apk already staged under our name is REFUSED before installing"
  new_router
  cp -f "$APK_A" "$BENCH_HARNESS_ROOT/tmp/$(basename "$APK_B")"     # right name, wrong build
  BEFORE="$(harness_installed_sha)"
  deploy_in_window "t11-substituted" --apk "$APK_B" --sha256 "$SHA_B" --install-timeout 30
  check_rc "substituted staged artifact refused (exit 7)" 7 "$RC"
  check_contains "refusal says SUBSTITUTED ARTIFACT" "SUBSTITUTED ARTIFACT REFUSED" "$OUT"
  check_contains "refusal shows the staged sha256" "staged sha256=$SHA_A" "$OUT"
  check_contains "refusal shows the named sha256" "named sha256=$SHA_B" "$OUT"
  check_eq "router binary untouched" "$BEFORE" "$(harness_installed_sha)"
  check_eq "no install happened" "0" "$(harness_apk_installs)"

  t_begin "install build A while naming build B => loud identity MISMATCH (negative control B)"
  new_router
  export HARNESS_FORCE_PAYLOAD="$PAY_A"     # the stand-in apk installs A no matter what is staged
  deploy_in_window "t12-substitution" --apk "$APK_B" --sha256 "$SHA_B" --install-timeout 60
  RC_SUB="$RC"; OUT_SUB="$OUT"
  unset HARNESS_FORCE_PAYLOAD
  check_rc "deploy failed loudly (exit 8)" 8 "$RC_SUB"
  check_contains "loud failure is named" "IDENTITY MISMATCH - LOUD FAILURE" "$OUT_SUB"
  check_contains "failure shows what the router runs" "$(sha256sum "$PAY_A" | cut -d' ' -f1)" "$OUT_SUB"
  check_contains "failure shows what was named" "$(sha256sum "$PAY_B" | cut -d' ' -f1)" "$OUT_SUB"
  check_contains "failure says do not call the bench ready" "Do not report this bench as ready" "$OUT_SUB"
  check_eq "router really did end up with A's bytes" "$(sha256sum "$PAY_A" | cut -d' ' -f1)" "$(harness_installed_sha)"
  check_not_contains "no success line was printed" "INSTALLED VERIFIED" "$OUT_SUB"

  t_begin "--refuse-foreign-staged refuses instead of rotating"
  new_router
  cp -f "$APK_A" "$BENCH_HARNESS_ROOT/tmp/tg-alpha5.apk"
  deploy_in_window "t13-refuse-foreign" --apk "$APK_B" --sha256 "$SHA_B" --refuse-foreign-staged --install-timeout 30
  check_rc "foreign staged apk refused (exit 7)" 7 "$RC"
  check_contains "refusal names the foreign file" "FOREIGN STAGED APK REFUSED" "$OUT"
  check_eq "foreign file was NOT rotated" "1" "$(ls "$BENCH_HARNESS_ROOT/tmp/tg-alpha5.apk" 2>/dev/null | wc -l | tr -d ' ')"
  check_eq "no install happened" "0" "$(harness_apk_installs)"

  t_begin "install.json package_path revert bomb is refused, then cleared on request"
  new_router
  printf '{\n  "package_path": "/tmp/tg-alpha5.apk",\n  "version": "1"\n}\n' > "$BENCH_HARNESS_ROOT/etc/tollgate/install.json"
  deploy_in_window "t14-package-path" --apk "$APK_B" --sha256 "$SHA_B" --install-timeout 30
  check_rc "revert bomb refused (exit 7)" 7 "$RC"
  check_contains "refusal names the revert bomb" "package_path='/tmp/tg-alpha5.apk'" "$OUT"
  check_eq "no install happened" "0" "$(harness_apk_installs)"
  deploy_in_window "t14-clear" --apk "$APK_B" --sha256 "$SHA_B" --clear-package-path --install-timeout 60
  check_rc "deploy proceeds once package_path is cleared" 0 "$RC"
  check_contains "package_path was neutralised" "CLEARED_PACKAGE_PATH" "$OUT"
  check_contains "install.json now says false" '"package_path": "false"' "$(cat "$BENCH_HARNESS_ROOT/etc/tollgate/install.json")"
  check_contains "identity verified after the cleared deploy" "INSTALLED VERIFIED" "$OUT"
fi

# =============================================================================== 3. naming
t_begin "a deploy that does not NAME its artifact is refused before anything else"
new_router
run_cmd "$BENCH_LOCK" exec "${LOCKOPTS[@]}" --purpose "t15-noname" -- "$BENCH_DEPLOY" --apk "$APK_B" 2>&1
check_rc "missing --sha256 refused (usage)" 2 "$RC"
check_contains "refusal says a deploy must name its sha256" "must name the sha256" "$OUT"

if [ "$have_fixtures" = 1 ]; then
t_begin "a named sha256 that does not match the file is refused"
run_cmd "$BENCH_LOCK" exec "${LOCKOPTS[@]}" --purpose "t16-badhash" -- "$BENCH_DEPLOY" --apk "$APK_B" --sha256 "$(printf '0%.0s' $(seq 1 64))" 2>&1
check_rc "artifact/sha mismatch refused (exit 6)" 6 "$RC"
check_contains "refusal names both hashes" "artifact identity mismatch" "$OUT"

t_begin "--dry-run stops before the install"
new_router
deploy_in_window "t17-dry" --apk "$APK_B" --sha256 "$SHA_B" --dry-run
check_rc "dry-run exits 0" 0 "$RC"
check_contains "dry-run says it would install" "DRY-RUN: would now 'apk add" "$OUT"
check_eq "dry-run installed nothing" "0" "$(harness_apk_installs)"

t_begin "the whole deploy path runs under BusyBox ash (the router's shell)"
if command -v busybox >/dev/null 2>&1; then
  new_router
  export BENCH_HARNESS_SH="busybox ash"
  deploy_in_window "t18-busybox" --apk "$APK_B" --sha256 "$SHA_B" --install-timeout 60
  unset BENCH_HARNESS_SH
  check_rc "deploy under busybox ash succeeded" 0 "$RC"
  check_contains "identity verified under busybox ash" "INSTALLED VERIFIED" "$OUT"
  INST="$(ls -1 "$BENCH_HARNESS_ROOT"/tmp/bench-install-*.sh 2>/dev/null | head -1)"
  if [ -n "$INST" ] && busybox ash -n "$INST"; then pass "the generated install script is accepted by busybox ash"
  else fail "busybox ash rejected the generated install script ($INST)"; fi
else
  skip "no busybox on this host"
fi
else
  t_begin "a named sha256 that does not match the file is refused"; skip "no fixtures"
  t_begin "--dry-run stops before the install"; skip "no fixtures"
  t_begin "the whole deploy path runs under BusyBox ash (the router's shell)"; skip "no fixtures"
fi

t_begin "the lock fd does not leak into the command, so a detached descendant cannot hold the bench"
run_cmd "$BENCH_LOCK" exec "${LOCKOPTS[@]}" --purpose "fd-leak-test" -- sh -c 'if [ -e /proc/self/fd/9 ]; then echo FD9_PRESENT; else echo FD9_CLOSED; fi'
check_contains "fd 9 is closed inside the window (no leak into exec'd children)" "FD9_CLOSED" "$OUT"
# The descendant is a UNIQUELY NAMED sleep, never a bare `sleep 20`: a live bench run can have
# `sleep 20` on a command line, and a family-pattern kill issued from this suite would then kill a
# process it does not own — the very collision class this card is about. The unique name is what
# lets the suite reap exactly its own descendant and nothing else.
DESC_SLEEP="$WORK/bin/bench-detached-descendant.sleep"
ln -sfn "$(command -v sleep)" "$DESC_SLEEP"
run_cmd "$BENCH_LOCK" exec "${LOCKOPTS[@]}" --purpose "detached-descendant-test" -- sh -c "setsid '$DESC_SLEEP' 20 >/dev/null 2>&1 &"
check_rc "window with a detached descendant exits 0" 0 "$RC"
run_cmd "$BENCH_LOCK" status "${LOCKOPTS[@]}"
check_rc "the bench is FREE after the window even though a descendant survived" 0 "$RC"
check_contains "status says FREE" "STATE     FREE" "$OUT"
pkill -f 'bench-detached-descendant\.sleep' >/dev/null 2>&1 || true
i=0
while pgrep -f 'bench-detached-descendant\.sleep' >/dev/null 2>&1 && [ "$i" -lt 5 ]; do sleep 1; i=$((i + 1)); done
if pgrep -f 'bench-detached-descendant\.sleep' >/dev/null 2>&1; then
  fail "the detached descendant survived the suite's own cleanup (it holds no lock, but the harness must not leave strays behind)"
else
  pass "the detached descendant was reaped by its unique name (no family-pattern kill was used)"
fi

t_begin "the INSTALLED shape works: symlinks on PATH (regression: the live run hit the symlink bug)"
SYMDIR="$WORK/bin"; mkdir -p "$SYMDIR"
for s in bench-lock bench-with-lock bench-deploy-apk; do ln -sfn "$BENCH_DIR/$s.sh" "$SYMDIR/$s"; done
run_cmd "$SYMDIR/bench-lock" status
check_rc "symlinked bench-lock runs" 0 "$RC"
check_contains "symlinked bench-lock finds the real lock" "$BENCH_LOCK_PATH" "$OUT"
check_not_contains "and never resolves the production lock" "$PROD_LOCK" "$OUT"
run_cmd "$SYMDIR/bench-with-lock" "${LOCKOPTS[@]}" --purpose "symlink-shape" -- true
check_rc "symlinked bench-with-lock runs (was rc=127 before the symlink fix)" 0 "$RC"
if [ "$have_fixtures" = 1 ]; then
  new_router
  run_cmd "$SYMDIR/bench-with-lock" "${LOCKOPTS[@]}" --purpose "symlink-deploy" -- \
      "$SYMDIR/bench-deploy-apk" --apk "$APK_B" --sha256 "$SHA_B" --install-timeout 60
  check_rc "symlinked deploy helper completed a full deploy" 0 "$RC"
  check_contains "symlinked deploy verified the identity" "INSTALLED VERIFIED" "$OUT"
else
  skip "no fixtures for the symlinked deploy"
fi

# ================================================= 4. second-purchase e2e + snapshot + tokens
# Offline only, like the rest of this suite: the paid path is exercised with the lock
# REFUSING, and the router-side snapshot is checked as text (render mode) rather than run.
# Live NUT-07 / mint checks belong to `make bench-token-verify`, not to a hermetic suite.

t_begin "the second-purchase e2e DEFAULTS to a dry run: nothing bought, no lock, no router"
run_cmd "$SECOND_PURCHASE"
check_rc "dry run exits 0" 0 "$RC"
check_contains "dry run is labelled" "mode=DRY-RUN" "$OUT"
check_contains "dry run promises nothing was spent" "nothing was purchased" "$OUT"
check_contains "dry run prints the phases it would run" "ndsctl deauth discriminator" "$OUT"
check_not_contains "dry run never took the bench lock" "taking the single-owner bench lock" "$OUT"

t_begin "the second-purchase e2e refuses a paid run it cannot pay for (usage, no router)"
run_cmd "$SECOND_PURCHASE" --purchase
check_rc "paid run without TOKEN_1 refused" 2 "$RC"
check_contains "refusal names the missing variable" "TOKEN_1 is required" "$OUT"

TOKDIR="$WORK/tokens"
mkdir -p "$TOKDIR"
printf 'cashuAfake-not-a-real-token\n' > "$TOKDIR/tok1.txt"
printf 'cashuAfake-not-a-real-token\n' > "$TOKDIR/tok2.txt"
run_cmd env TOKEN_1="$TOKDIR/tok1.txt" TOKEN_2="$TOKDIR/tok1.txt" "$SECOND_PURCHASE" --purchase
check_rc "the same file for both tokens refused" 2 "$RC"
check_contains "refusal explains why" "the second purchase needs a fresh token" "$OUT"

t_begin "a PAID run is refused while another window holds the bench — and never probes the router"
start_holder alpha "$WORK/holder2.out" take "${LOCKOPTS[@]}" --purpose "e2e-refusal-holder" --hold "$BENCH_TEST_HOLD"
HOLD2="$HOLDER_PID"
sleep 1
rm -rf "$WORK/e2e-logs"
run_cmd env BENCH_PROFILE=beta \
  TOKEN_1="$TOKDIR/tok1.txt" TOKEN_2="$TOKDIR/tok2.txt" \
  LOG_DIR="$WORK/e2e-logs" \
  "$SECOND_PURCHASE" --purchase --nic lo --client-ip 127.0.0.2
check_rc "paid run refused (bench-lock exit 3)" 3 "$RC"
check_contains "refusal names the holder's purpose" "e2e-refusal-holder" "$OUT"
check_not_contains "refusal happened before the router liveness probe" "preflight: backend reports" "$OUT"
check_not_contains "and before any purchase was attempted" "PURCHASE buy#1" "$OUT"
if [ -e "$WORK/e2e-logs" ]; then
  fail "the refused run created $WORK/e2e-logs — it got past the lock"
else
  pass "no transcript directory was created: the run stopped at the lock"
fi
"$BENCH_LOCK" release "${LOCKOPTS[@]}" --force >/dev/null 2>&1 || true
stop_holder "$HOLD2" "the e2e-refusal holder"

t_begin "the router-side snapshot: no lock without a window, and a payload the router's sh accepts"
run_cmd "$ROUTER_SNAPSHOT" snapshot
check_rc "snapshot without a bench window refused (bench-lock require)" 4 "$RC"
check_contains "refusal explains the sanctioned invocation" "must run as" "$OUT"

run_cmd "$ROUTER_SNAPSHOT" render --label phase0-fresh
check_rc "render mode runs with no lock and no ssh" 0 "$RC"
PAYLOAD="$WORK/snapshot-payload.sh"
printf '%s\n' "$OUT" > "$PAYLOAD"
check_contains "payload interpolates the label" 'LABEL="phase0-fresh"' "$OUT"
check_contains "payload reads the data-allotment guard chain" "nds_enforce_forward" "$OUT"
check_contains "payload reads the admin-board guard chain" "admin_board_input_guard" "$OUT"
check_contains "payload reads balance as the router sees it" "127.0.0.1:2121/balance" "$OUT"
check_contains "payload still ends with its completion marker" "SNAP_DONE" "$OUT"

run_cmd sh -n "$PAYLOAD"
check_rc "sh -n accepts the payload" 0 "$RC"
if command -v busybox >/dev/null 2>&1; then
  run_cmd busybox ash -n "$PAYLOAD"
  check_rc "busybox ash -n accepts the payload (the router's own shell)" 0 "$RC"
else
  skip "no busybox on this host"
fi

t_begin "the token tool is dry-run safe and refuses to check a token that is not there"
run_cmd "$BENCH_TOKEN" mint --amount 64 --out "$WORK/never-written.txt"
check_rc "mint without --yes is a dry run (exit 0)" 0 "$RC"
check_contains "dry run says it minted nothing" "nothing minted" "$OUT"
if [ -e "$WORK/never-written.txt" ]; then
  fail "the dry run wrote a token file"
else
  pass "no token file was written"
fi

run_cmd "$BENCH_TOKEN" verify --token-file "$WORK/definitely-absent.txt"
check_rc "verify on a missing token file refused" 2 "$RC"
check_contains "refusal names the path" "definitely-absent.txt" "$OUT"

# ===================================== 5. the live-run guards this lane depends on (no router)
# second-purchase-e2e.sh carries the two guards that decide whether a LIVE run's verdict means
# anything: the box-identity (restart) guard and the PHASE 5b zombie-settle assertions. Their
# controls are hand-run today, i.e. evidence that rots. They are driven here — and then each
# control is run against a MUTATED copy of the script that must make it go RED, so the wiring
# itself is proven: a control that cannot fail is decoration.
E2E="$BENCH_DIR/second-purchase-e2e.sh"
MUT_DIR="$WORK/mutants"
mkdir -p "$MUT_DIR"

t_begin "restart-guard-control: the box-identity guard fires on every way the box can move"
run_cmd "$HERE/restart-guard-control.sh"
check_rc "restart-guard-control exits 0 on the frozen script" 0 "$RC"
check_contains "it reports PASS" "restart-guard-control: PASS (1 no-false-fire + 4 can-fail)" "$OUT"
check_not_contains "no direction failed" "   FAIL - " "$OUT"
check_eq "every direction ran" "5" "$(printf '%s' "$OUT" | grep -c '^   ok   - ' | head -1)"

t_begin "zombie-settle-control: the PHASE 5b assertions fire on every way the module can fail"
run_cmd "$HERE/zombie-settle-control.sh"
check_rc "zombie-settle-control exits 0 on the frozen script" 0 "$RC"
check_contains "it reports PASS" "PASS: PHASE 5b fails in every direction it is supposed to" "$OUT"
check_not_contains "no direction failed" "   FAIL - " "$OUT"
check_eq "every direction ran (8 cases + the anti-vacuity read)" "9" "$(printf '%s' "$OUT" | grep -c '^   ok   - ' | head -1)"

t_begin "the wired-in controls still go RED on a mutated e2e script (the wiring is not decoration)"
# The restart guard has exactly one failure path (box_broken): neutralising it must make every
# "the box moved" direction return 0, and the control must notice.
sed 's/^box_broken() {/box_broken() { return 0;/' "$E2E" > "$MUT_DIR/mutant-restart.sh"
check_eq "the restart mutation landed" "1" "$(grep -c '^box_broken() { return 0;' "$MUT_DIR/mutant-restart.sh" | head -1)"
run_cmd "$HERE/restart-guard-control.sh" "$MUT_DIR/mutant-restart.sh"
check_rc "restart-guard-control FAILS on the mutated script" 1 "$RC"
check_contains "and says so" "restart-guard-control: FAIL" "$OUT"
check_contains "naming the direction that broke" "   FAIL - wanted want=broken" "$OUT"

# Every settle assertion funnels into FAILED; with the counter frozen the phase can no longer
# report a failure at all, which the control must catch.
sed 's/^\([[:space:]]*\)FAILED=$((FAILED + 1))/\1:/' "$E2E" > "$MUT_DIR/mutant-zombie.sh"
check_eq "the zombie mutation landed (no FAILED increment left)" "0" "$(grep -c 'FAILED=$((FAILED + 1))' "$MUT_DIR/mutant-zombie.sh" | head -1)"
run_cmd "$HERE/zombie-settle-control.sh" "$MUT_DIR/mutant-zombie.sh"
check_rc "zombie-settle-control FAILS on the mutated script" 1 "$RC"
check_contains "and says so" "FAIL: at least one control direction did not behave as documented" "$OUT"

# ============================================ 6. the live run's lock survived this suite
t_begin "the PRODUCTION bench lock is exactly as this suite found it (holder line included)"
PROD_AFTER="$(prod_lock_fingerprint)"
check_eq "production lock bytes + inode + size + holder line unchanged" "$PROD_BEFORE" "$PROD_AFTER"
check_not_contains "the production holder line was not rewritten by this suite" "test-harness" "$(prod_lock_holder_line)"
PROD_FLOCK_AFTER="$(prod_lock_flock_state)"
if [ "$PROD_FLOCK_BEFORE" = "$PROD_FLOCK_AFTER" ]; then
  pass "the production flock state is unchanged ($PROD_FLOCK_AFTER)"
else
  # Deliberately NOT a FAIL: a real run may have started or finished while this suite ran, and
  # this suite cannot tell that apart from its own interference. What IS attributable — a rewritten
  # holder line or a changed file — is asserted above, and this suite's own lock is named here.
  printf '   note - production flock state moved %s -> %s while the suite ran (this suite holds only %s)\n' \
    "$PROD_FLOCK_BEFORE" "$PROD_FLOCK_AFTER" "$BENCH_LOCK_PATH"
fi

lock_kill_all
rm -f "$BENCH_LOCK_PATH"
printf '\nworkdir kept for inspection: %s\n' "$WORK"
summary

