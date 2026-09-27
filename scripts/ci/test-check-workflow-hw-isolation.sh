#!/usr/bin/env bash
# =============================================================================
# Self-test for the hardware-lane isolation guard.
# =============================================================================
# A guard that has never been seen red is decoration. Each case copies the REAL
# .github/workflows tree into a temp dir, injects exactly one regression, and
# asserts the guard fails and names the rule that is supposed to catch it. The
# baseline case asserts the unmodified tree passes.
#
# Run: bash scripts/ci/test-check-workflow-hw-isolation.sh
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECK="$REPO_ROOT/scripts/ci/check-workflow-hw-isolation.sh"
FIXTURES="$REPO_ROOT/scripts/ci/fixtures"
SRC="$REPO_ROOT/.github/workflows"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cases_run=0
cases_failed=0

# run_case <name> <expected_rc> <expected_rule_regex_or_-> [fixture...]
#   fixture = path relative to fixtures/ ; a directory is copied over the tree
#   (replacing same-named files), a file is copied in, "@rm:<file>" deletes.
run_case() {
    local name="$1" want="$2" want_rule="$3"
    shift 3
    local dir="$TMP/$name" rc=0
    mkdir -p "$dir"
    cp "$SRC"/*.yml "$dir"/ 2>/dev/null || true
    cp "$SRC"/*.yaml "$dir"/ 2>/dev/null || true

    local fx
    for fx in "$@"; do
        case "$fx" in
            @rm:*) rm -f "$dir/${fx#@rm:}" ;;
            *)
                if [ -d "$FIXTURES/$fx" ]; then
                    cp "$FIXTURES/$fx"/* "$dir"/
                else
                    cp "$FIXTURES/$fx" "$dir/$fx"
                fi
                ;;
        esac
    done

    bash "$CHECK" "$dir" > "$TMP/$name.out" 2>&1
    rc=$?
    cases_run=$((cases_run + 1))

    if [ "$rc" -ne "$want" ]; then
        echo "FAIL [$name] exit=$rc want=$want"
        sed 's/^/      | /' "$TMP/$name.out"
        cases_failed=$((cases_failed + 1))
        return
    fi
    if [ "$want_rule" != "-" ] && ! grep -qE "$want_rule" "$TMP/$name.out"; then
        echo "FAIL [$name] exit ok ($rc) but output never mentions /$want_rule/"
        sed 's/^/      | /' "$TMP/$name.out"
        cases_failed=$((cases_failed + 1))
        return
    fi
    echo "ok   [$name] exit=$rc${want_rule:+  rule=/$want_rule/}"
}

echo "Hardware-lane isolation guard — self-test"
echo "  guard    : $CHECK"
echo "  real tree: $SRC"
echo

# GREEN: the real tree must be safe.
run_case baseline 0 -

# RED: a self-hosted job back on a PR-triggered workflow (the original bug).
run_case pr-selfhosted 1 'R1 self-hosted' pr-selfhosted.yml

# RED: the mutating, paid-traffic env flags reachable from a PR.
run_case pr-mutating-env 1 'R1 bench-mutating' pr-mutating-env.yml

# RED: the hardware workflow gains a pull_request trigger.
run_case hw-pull-request 1 'R2 hardware workflow is reachable' hw-pull-request

# RED: the hardware workflow gains a non-whitelisted trigger.
run_case hw-push-trigger 1 "R2 trigger 'push' not allowed" hw-push-trigger

# RED: mutating job with no environment approval gate.
run_case hw-mutating-unapproved 1 'R4 job "mutating-e2e"' hw-mutating-unapproved

# RED: secrets in the hardware workflow with no environment approval gate.
run_case hw-secrets-unapproved 1 'R5 job "secrets-leak"' hw-secrets-unapproved

# RED: the stale kill switch that started all of this.
run_case job-if-false 1 'R3 disabled job' if-false.yml

# RED: hardware work with no dedicated hardware workflow at all.
run_case hw-missing 1 'R2 hardware workflow missing' @rm:hw-smoke.yml

# RED (round-2, cold cross-family review glm-5.3 Y1): a PR workflow that names
# ONLY the bench runner's custom label — no `self-hosted` string to grep for.
run_case pr-label-only 1 'R1 self-hosted runner label' pr-label-only.yml

# RED (review Y2): an expression-valued runner target cannot be verified, so it
# must fail closed.
run_case pr-runs-on-expression 1 'R1 expression-valued runs-on' pr-runs-on-expression.yml

# RED (review Y3): the kill switch with a capital F.
run_case job-if-false-capital 1 'R3 disabled job' job-if-false-capital.yml

# RED (review Y4): the approval gate declared under a typo'd environment name.
run_case hw-env-typo 1 'R4 job "mutating-e2e"' hw-env-typo

# RED (round-2 review Y6): the runner target as a block sequence — the label is
# on the next line, so a same-line parser reads nothing and passes it.
run_case pr-runs-on-block-sequence 1 'R1 runs-on with no inline value' pr-runs-on-block-sequence.yml

# RED (round-2 review Y7): the denied label set is DERIVED from the hardware
# workflow, so degrading that file (expression-valued runner target) would
# silently shrink the denied set to `self-hosted` alone. The derivation must
# fail closed instead.
run_case hw-runs-on-unverifiable 1 'cannot derive the bench label set' hw-runs-on-expression

# RED (round-3 review of the hardened head, finding 2 / Y7-family): the trigger
# section written as a YAML flow mapping (`on: {…}`) is invisible to the trigger
# parser, so a PR-triggered self-hosted job would pass as "no triggers at all".
run_case pr-on-flow-mapping 1 'flow mapping' pr-on-flow-mapping.yml

# RED (round-3 review finding 3): R3 only caught the bare word `false`, so the
# kill switch could simply change spelling (${{ false }} / 0 / '').
run_case job-if-expression-false 1 'R3 disabled job' job-if-expression-false.yml

# RED (round-4 review finding 1, blocker): a bench-label runner in a THIRD file
# — not PR-reachable, so R1 never arms, and not the hardware workflow, so
# R2/R4/R5 never look at it. Only R6 catches this.
run_case other-self-hosted 1 'R6 bench runner label' other-self-hosted.yml

# RED (round-4 review finding 2): the kill switch as a folded scalar (`if: >-`
# with `false` on the continuation line) — invisible to a line-anchored regex.
run_case job-if-folded-false 1 'R3 disabled job parked behind a folded falsy' job-if-folded-false.yml

# RED (round-5 review finding 1, blocker): the ALLOWLISTED basename carrying a
# bench-SPECIFIC label. A PR can add an allowlisted file and `uses:` it, so the
# allowlist must excuse only a different self-hosted fleet.
run_case cloud-lab-runner.yml-bench-label 1 "R6 bench runner label 'tollgate-router'" cloud-lab-runner.yml

# RED (round-5 review finding 2, blocker): valid YAML whose `on:` keys are
# indented four spaces, so a non-empty `on:` block parses to zero triggers and
# the file would pass every reachability rule by default.
run_case on-deep-indent 1 "'on:' section is not readable by the parser" on-deep-indent.yml

# RED (round-5 review finding 2, second half): the hardware workflow's jobs
# indented away from the two-space form the R4/R5 scanner matches, so `secrets.`
# without an `environment:` gate would report nothing at all.
run_case hw-deep-jobs 1 'R4/R5 job structure is not parseable' hw-deep-jobs

# RED (round-6 review Y1, blocker): the ALLOWLISTED basename naming ONLY
# `self-hosted`. GitHub gives EVERY self-hosted runner that label — the bench's
# included — so an allowlisted basename may not excuse it without a label of a
# different fleet on the same `runs-on` line.
run_case cloud-lab-runner.yml-bare-selfhosted 1 "'self-hosted' alone reaches EVERY self-hosted runner" cloud-lab-bare-selfhosted

# RED (round-6 review Y2, same family): the denied label set is derived from
# hw-smoke.yml, a file a PR can respell to `[self-hosted]`. The hardcoded floor
# must keep the bench-specific label denied. Fixture = degraded hw-smoke.yml plus
# another file still carrying `tollgate-router`.
run_case hw-labels-degraded 1 "R6 bench runner label 'tollgate-router'" hw-labels-degraded

echo
if [ "$cases_failed" -eq 0 ]; then
    echo "PASS — $cases_run/$cases_run guard cases behaved as specified."
    exit 0
fi
echo "FAILED — $cases_failed/$cases_run guard cases wrong."
exit 1
