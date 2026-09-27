#!/usr/bin/env bash
# =============================================================================
# Guard: no workflow reachable from an untrusted PR event may touch the bench.
# =============================================================================
# WHY: a `pull_request` trigger plus a `[self-hosted, tollgate-router]` runner
# means any contributor's PR code executes on the machine that flashes routers,
# holds the repo secrets and sits in the bench's L1/L2 neighbourhood. The
# previous state (ci.yml `test-ui` / `test-physical` at `if: false` on a
# pull_request-triggered workflow) was one flipped boolean away from that, and
# the job force-enabled router-mutating, paid-traffic specs besides.
#
# RULES
#   R1  A workflow with a pull_request / pull_request_target trigger must not
#       reach a bench runner. "Reach a bench runner" is decided on the LABEL
#       SET, not on the literal `self-hosted` string: every label used by the
#       hardware workflow's `runs-on` (plus `self-hosted`, plus anything in
#       HW_RUNNER_LABELS_EXTRA) is denied, and an expression-valued `runs-on:`
#       is rejected outright because it cannot be verified by reading. It must
#       also not reference any bench-mutating env flag. An `on:` written as a
#       flow mapping (`on: {…}`) is ALSO a violation: the trigger parser cannot
#       read it, so PR-reachability becomes undecidable and the file cannot be
#       proven bench-safe.
#   R2  The hardware workflow (default `hw-smoke.yml`) must exist, must NOT be
#       PR-reachable, and its triggers must be a subset of
#       {workflow_dispatch, schedule} — maintainer-triggered only.
#   R3  No disabled job parked behind a falsy `if:` anywhere in
#       .github/workflows/ (case-insensitive `false` — bare, quoted, inside an
#       expression, or as the body of a folded `if: >-` scalar — plus the
#       YAML-1.1 falsy words `no`/`off`, numeric `0` and a quoted empty string).
#       A job parked behind a flag is an invitation to flip it; enablement must
#       be an explicit input / repo variable / environment approval.
#   R4  Any job that references a bench-mutating env flag must declare
#       `environment:` NAMED `$HW_ENVIRONMENT_NAME` (default `bench-hardware`) —
#       presence of the key is not enough, a typo'd environment would silently
#       drop the required-reviewer gate.
#   R5  In the hardware workflow, any job that uses `secrets.` must declare the
#       same named environment too — the read-only lane must stay
#       credential-free.
#   R6  No bench-label runner outside the hardware workflow. R1 only arms on
#       files whose triggers the parser can read, and R2/R4/R5 only ever look at
#       the hardware workflow — so a self-hosted job added to a THIRD file
#       (push-triggered, or `workflow_call`-able and therefore reachable from a
#       pull_request-triggered caller) would otherwise pass every rule. Bench
#       work lives in exactly one file. A file that legitimately runs on a
#       DIFFERENT self-hosted fleet must be named in
#       HW_NON_BENCH_SELF_HOSTED_ALLOW and is reported as an explicit
#       exception on every run, never silently skipped; unverifiable `runs-on`
#       targets in it still fail closed.
#
# LIMIT (stated, not hidden): this guard runs from the PR's own checkout, so a
# hostile PR can delete it in the same commit that adds the self-hosted job.
# For that class of PR the mechanical protection is branch protection + the
# `lint` check being REQUIRED, not this script. See docs/hw-lane-isolation.md.
#
# KNOWN RESIDUALS (measured, deliberately not chased — R3's job is to remove the
# *flipped boolean*, not to be a YAML evaluator):
#   * `if:` values that are falsy without being spelled as one of the forms R3
#     matches: `if: !true`, `if: null`, `if: ${{ !inputs.enabled }}`, and any
#     other expression whose falsiness only exists at evaluation time.
#   * an `if:` reached through YAML anchors/aliases (`if: *disabled`) is not
#     resolved.
#   * this parser is line-form based: forms it cannot read never pass silently
#     (that is what R1c/R1d/R6's fail-closed branches are for), but it is not a
#     YAML parser and does not claim to be.
#
# Only effective YAML is scanned: full-line and trailing comments are stripped
# first, so documentation prose (which necessarily names the anti-patterns it
# bans) cannot trip the guard.
#
# USAGE
#   bash scripts/ci/check-workflow-hw-isolation.sh [WORKFLOWS_DIR]
#   Exit 0 = safe. Exit 1 = unsafe (every violation printed as file: R# …).
#   Env knobs: HW_WORKFLOW_NAME, HW_ENVIRONMENT_NAME, HW_RUNNER_LABELS_EXTRA
#              (space-separated extra runner labels to deny),
#              HW_NON_BENCH_FLEET_LABELS (labels identifying a *different*
#              self-hosted fleet), HW_BENCH_LABELS_DEFAULT (bench-specific labels
#              denied even if the hardware workflow stops declaring them).
#
# The guard's own RED/GREEN matrix lives in
# scripts/ci/test-check-workflow-hw-isolation.sh — a guard never seen red is
# decoration, not evidence. It runs in the ci.yml `lint` job on every PR.
# =============================================================================
set -uo pipefail

WF_DIR="${1:-.github/workflows}"
HW_WORKFLOW_NAME="${HW_WORKFLOW_NAME:-hw-smoke.yml}"
HW_ENVIRONMENT_NAME="${HW_ENVIRONMENT_NAME:-bench-hardware}"
HW_RUNNER_LABELS_EXTRA="${HW_RUNNER_LABELS_EXTRA:-}"
HW_TRIGGER_WHITELIST="workflow_dispatch schedule"
# Files (basenames) that may legitimately run on a DIFFERENT self-hosted fleet
# (i.e. not the bench). They are reported as an explicit exception on every run,
# never silently skipped. R6 still rejects unverifiable `runs-on` targets in
# them. Default: cloud-lab-runner.yml targets the ephemeral GCP `cloud-lab` VM.
HW_NON_BENCH_SELF_HOSTED_ALLOW="${HW_NON_BENCH_SELF_HOSTED_ALLOW:-cloud-lab-runner.yml}"
# Labels that identify a DIFFERENT self-hosted fleet. `self-hosted` on its own is
# not a fleet — it matches EVERY self-hosted runner, the bench's included — so an
# allowlisted file may only be excused for it on a `runs-on` line that also names
# one of these (round-6 review Y1).
HW_NON_BENCH_FLEET_LABELS="${HW_NON_BENCH_FLEET_LABELS:-cloud-lab}"
# Bench-specific labels denied even when the hardware workflow stops declaring
# them. Derived sets live in files a PR controls, so the floor is hardcoded: a PR
# cannot shrink the denied set by respelling hw-smoke.yml's runner target
# (round-6 review Y2). `tollgate-router` is the label this card names.
HW_BENCH_LABELS_DEFAULT="${HW_BENCH_LABELS_DEFAULT:-tollgate-router}"

if [ ! -d "$WF_DIR" ]; then
    echo "FATAL: workflows dir not found: $WF_DIR" >&2
    exit 2
fi

violations=0
note() { printf '%s\n' "$*"; }
violate() { violations=$((violations + 1)); printf 'VIOLATION %s\n' "$*"; }

# YAML-effective text: drop full-line and trailing comments.
yaml_effective() {
    sed -E 's/(^|[[:space:]])#.*$/\1/' "$1"
}

# The `on:` trigger section (block form) plus any inline list on the `on:` line.
on_block() {
    awk '
        /^on[[:space:]]*:/ {
            f = 1
            inline = $0
            sub(/^on[[:space:]]*:/, "", inline)
            if (inline !~ /^[[:space:]]*$/) print inline
            next
        }
        f && /^[^[:space:]]/ { exit }
        f { print }
    ' "$1"
}

# Trigger names, one per line (block form at exactly 2-space indent, or inline
# list). Scoped to the `on:` section: job names are also 2-space keys, so a
# whole-file scan would mistake them for triggers.
triggers_of() {
    on_block "$1" \
        | sed -E 's/(^|[[:space:]])#.*$/\1/' \
        | sed -n -E 's/^\[(.*)\][[:space:]]*$/\1/p; s/^  ([A-Za-z_][A-Za-z0-9_-]*)[[:space:]]*:.*/\1/p; s/^[[:space:]]*([A-Za-z_][A-Za-z0-9_-]*)[[:space:]]*$/\1/p' \
        | tr ',' '\n' \
        | tr -d " \"'" \
        | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
        | grep -v '^$' || true
}

is_pr_reachable() {
    triggers_of "$1" | grep -qxE 'pull_request|pull_request_target'
}

# `lineno:…` for every `on:` written as a YAML FLOW MAPPING (`on: {…}`).
# `triggers_of()` reads the block form and the inline LIST form only, so a flow
# mapping makes it return nothing — the file then looks exactly like a file with
# no triggers at all: `is_pr_reachable` is false (R1 never arms) and the R2
# whitelist loop iterates zero triggers (nothing "not allowed"). A PR-reachable
# workflow, or the hardware workflow itself, could therefore be replaced with
# `on: {pull_request: null}` and pass. Unverifiable trigger routing fails closed.
on_flow_mapping_lines() {
    [ -f "$1" ] || return 0
    yaml_effective "$1" | grep -nE '^on[[:space:]]*:[[:space:]]*\{' || true
}

# A reason string when an `on:` section exists but the trigger parser extracts
# ZERO triggers from it — e.g. the keys indented at four spaces instead of two.
# Such a file looks exactly like a file with no triggers, so PR-reachability is
# undecidable and the file must fail closed rather than pass by default.
on_section_unreadable() {
    local body trig
    [ -f "$1" ] || return 0
    body="$(on_block "$1" | sed -E 's/(^|[[:space:]])#.*$/\1/' | tr -d '[:space:]')"
    [ -n "$body" ] || return 0
    trig="$(triggers_of "$1" | tr -d '[:space:]')"
    [ -z "$trig" ] && printf '%s\n' "a non-empty 'on:' block yielded 0 parsed triggers"
    return 0
}

# `runs-on` lines of the hardware workflow, as `lineno:rest-of-line`.
hw_runs_on_lines() {
    [ -f "$1" ] || return 0
    yaml_effective "$1" | grep -nE '^[[:space:]]*runs-on:' || true
}

# Lowercased runner labels on any file's `runs-on` lines.
#
# NOTE: this deliberately DROPS `${{ … }}` values (they are not labels). That is
# only safe because every caller either checks the file with
# `hw_runs_on_unverifiable()` first (the hardware workflow / R6) or is guarded by
# R1a's own expression check. A future caller that uses this function alone would
# silently shrink the denied set to `self-hosted` — the Y7 defect. If you add a
# caller, add the unverifiable check with it.
runs_on_labels() {
    hw_runs_on_lines "$1" \
        | sed -E 's/^[0-9]+:[[:space:]]*runs-on:[[:space:]]*//' \
        | sed -E 's/[[:space:]]*$//' \
        | tr -d "[]\"'" \
        | tr ',' '\n' \
        | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
        | grep -v '^$' \
        | grep -v '\${{' \
        | tr '[:upper:]' '[:lower:]' || true
}

# Lowercased runner labels used by the hardware workflow's `runs-on` lines.
hw_runner_labels() {
    runs_on_labels "$1"
}

# Bodies of `if:` guards written as a folded/block scalar (`if: >-`, `if: |`),
# one joined line per guard. A line-anchored regex cannot see a value that lives
# on the continuation lines, so a kill switch can simply respell itself as
# `if: >-` + `false`.
falsy_folded_if_bodies() {
    [ -f "$1" ] || return 0
    yaml_effective "$1" | awk '
        function flush() {
            if (body != "") { gsub(/^[ \t]+|[ \t]+$/, "", body); print body; body = "" }
        }
        /^[ \t]*if:[ \t]*[>|]/ { flush(); collecting = 1; body = ""; indent = match($0, /[^ \t]/); next }
        collecting && /^[ \t]*$/ { next }
        collecting {
            m = match($0, /[^ \t]/)
            if (m <= indent) { flush(); collecting = 0; next }
            line = $0; gsub(/^[ \t]+|[ \t]+$/, "", line)
            body = (body == "" ? line : body " " line)
            next
        }
        END { flush() }
    ' || true
}

# A guard body that evaluates falsy: bare/quoted false/no/off/0, an empty quoted
# string, or an expression that is literally falsy.
RE_FALSY_GUARD='^["'"'"']?(false|no|off|0)["'"'"']?$|^["'"'"']{2}$|^[$]\{\{[^}]*\b(false|off|no|0)\b[^}]*\}\}$'

# `lineno:reason` for every hardware-workflow `runs-on` the guard cannot read a
# label set out of. Deriving the denied set from a file the SAME PR controls is
# only safe if the derivation fails closed when the source is degraded: an
# expression-valued or block-sequence `runs-on` would otherwise silently shrink
# the denied set (down to `self-hosted`) and let the original payload through.
hw_runs_on_unverifiable() {
    local hit n val
    while IFS= read -r hit; do
        [ -n "$hit" ] || continue
        n="${hit%%:*}"
        val="${hit#*:runs-on:}"
        val="${val%%#*}"
        if printf '%s' "$val" | grep -q '\${{'; then
            printf '%s:%s\n' "$n" "expression-valued runs-on"
        elif [ -z "$(printf '%s' "$val" | tr -d '[:space:]')" ]; then
            printf '%s:%s\n' "$n" "block-sequence runs-on with no inline value"
        fi
    done < <(hw_runs_on_lines "$1")
}

echo "Hardware-lane isolation guard"
echo "  workflows dir : $WF_DIR"
echo "  hardware wf   : $HW_WORKFLOW_NAME"
echo "  approval env  : $HW_ENVIRONMENT_NAME"
echo

shopt -s nullglob
wf_files=("$WF_DIR"/*.yml "$WF_DIR"/*.yaml)
shopt -u nullglob

if [ "${#wf_files[@]}" -eq 0 ]; then
    echo "FATAL: no workflow files in $WF_DIR" >&2
    exit 2
fi

# Denied label set: `self-hosted` always, every label the hardware workflow
# itself uses, plus operator-supplied extras. Compared case-insensitively.
hw_path="$WF_DIR/$HW_WORKFLOW_NAME"

# Fail closed if the hardware workflow's own runner target cannot be read: the
# denied set is derived from that file, and a degraded derivation would silently
# shrink it.
if [ -f "$hw_path" ]; then
    while IFS= read -r hit; do
        [ -n "$hit" ] || continue
        violate "$hw_path:${hit%%:*}  R1 cannot derive the bench label set from the hardware workflow (${hit#*:}) — fix the runner target"
    done < <(hw_runs_on_unverifiable "$hw_path")
fi

bench_labels="$(printf '%s\n%s\n%s\n' "self-hosted" "$(hw_runner_labels "$hw_path")" \
    "$(printf '%s' "$HW_BENCH_LABELS_DEFAULT" | tr ' ' '\n')" | sort -u | grep -v '^$' || true)"
if [ -n "$HW_RUNNER_LABELS_EXTRA" ]; then
    bench_labels="$(printf '%s\n%s\n' "$bench_labels" "$(printf '%s' "$HW_RUNNER_LABELS_EXTRA" | tr ' ' '\n')" \
        | sort -u | grep -v '^$' || true)"
fi

# Bench-SPECIFIC labels: what only the bench declares. The hardcoded floor is
# unioned in because this set is otherwise derived from hw-smoke.yml, which a PR
# can respell — an empty set would turn every R6 check into a no-op (Y2).
hw_specific_labels="$(printf '%s\n%s\n%s\n' \
    "$(hw_runner_labels "$hw_path" | grep -vx 'self-hosted' || true)" \
    "$(printf '%s' "$HW_BENCH_LABELS_DEFAULT" | tr ' ' '\n')" \
    "$(printf '%s' "$HW_RUNNER_LABELS_EXTRA" | tr ' ' '\n')" | sort -u | grep -v '^$' || true)"

if [ -z "$hw_specific_labels" ]; then
    violate "$hw_path  R1 no bench-specific runner label could be derived (the denied set must never shrink to nothing) — set HW_BENCH_LABELS_DEFAULT or fix the runner target"
fi

is_bench_label() {
    local l
    l="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
    [ -n "$l" ] || return 1
    printf '%s\n' "$bench_labels" | grep -qxF "$l"
}

# ---------------------------------------------------------------------------
# R1 + R3 — whole-tree rules
# ---------------------------------------------------------------------------
for f in "${wf_files[@]}"; do
    # R1c: an `on:` the trigger parser cannot read (flow mapping) means
    # PR-reachability itself is undecidable -> fail closed, whatever the file is.
    while IFS= read -r hit; do
        [ -n "$hit" ] || continue
        violate "$f:${hit%%:*}  R1 'on:' written as a flow mapping ({...}) is not verifiable by reading — PR-reachability cannot be decided, so the file cannot be proven bench-safe (fail closed)"
    done < <(on_flow_mapping_lines "$f")

    # R1d: an `on:` section the parser cannot read AT ALL (non-empty body, zero
    # triggers parsed — e.g. keys indented at four spaces) leaves PR-reachability
    # undecidable. Same class as the flow mapping above, different spelling.
    while IFS= read -r why; do
        [ -n "$why" ] || continue
        violate "$f  R1 'on:' section is not readable by the parser ($why) — PR-reachability cannot be decided, so the file cannot be proven bench-safe (fail closed)"
    done < <(on_section_unreadable "$f")

    # R6: a bench-label runner in ANY file other than the hardware workflow.
    # R1 only arms on files the trigger parser can see as PR-reachable, and R2/R4/
    # R5 only look at the hardware workflow — so a self-hosted job in a third file
    # (push-triggered, or `workflow_call`-able and therefore reachable from a
    # pull_request-triggered caller) used to pass every rule. Bench work lives in
    # exactly one file; anything else must be named in
    # HW_NON_BENCH_SELF_HOSTED_ALLOW and is reported as an exception — and even
    # there a bench-SPECIFIC label is a violation, while the generic
    # `self-hosted` is excused only next to a label of a different fleet.
    if [ "$f" != "$hw_path" ]; then
        r6_allowed=0
        for _a in $HW_NON_BENCH_SELF_HOSTED_ALLOW; do
            [ "$(basename "$f")" = "$_a" ] && r6_allowed=1
        done
        while IFS= read -r hit; do
            [ -n "$hit" ] || continue
            n="${hit%%:*}"
            lin="${hit#*:}"
            val="${lin#*runs-on:}"
            val="${val%%#*}"
            if [ -z "$(printf '%s' "$val" | tr -d '[:space:]')" ] \
               || printf '%s' "$val" | grep -q '\${{'; then
                violate "$f:$n  R6 runs-on outside the hardware workflow is not verifiable by reading (fail closed)"
                continue
            fi
            line_labels="$(printf '%s' "$val" | tr -d "[]\"'" | tr ',' ' ')"
            # `self-hosted` alone names no fleet — it matches EVERY self-hosted
            # runner, the bench's included — so it may only be excused on a line
            # that ALSO names a label of a different fleet (round-6 review Y1).
            fleet_ok=0
            for fl in $(printf '%s' "$HW_NON_BENCH_FLEET_LABELS" | tr ' ' '\n'); do
                [ -n "$fl" ] || continue
                for cand in $line_labels; do
                    [ "$(printf '%s' "$cand" | tr '[:upper:]' '[:lower:]')" = \
                      "$(printf '%s' "$fl" | tr '[:upper:]' '[:lower:]')" ] && fleet_ok=1
                done
            done
            for lab in $line_labels; do
                is_bench_label "$lab" || continue
                ll="$(printf '%s' "$lab" | tr '[:upper:]' '[:lower:]')"
                # A bench-SPECIFIC label is never excusable by the allowlist: a PR
                # controls an allowlisted file's NAME as much as another file's
                # contents (it can drop `.github/workflows/cloud-lab-runner.yml` in
                # and add `uses:` for it), so the basename may not authorise
                # bench-specific routing.
                if [ "$r6_allowed" -eq 1 ] \
                   && printf '%s\n' "$hw_specific_labels" | grep -qxF "$ll"; then
                    violate "$f:$n  R6 bench runner label '$lab' outside the hardware workflow, even under HW_NON_BENCH_SELF_HOSTED_ALLOW (that allowlist covers a different self-hosted fleet, not the bench)"
                elif [ "$r6_allowed" -eq 1 ] && [ "$fleet_ok" -eq 1 ]; then
                    note "  exception: $f:$n  '$lab' allowed by HW_NON_BENCH_SELF_HOSTED_ALLOW (non-bench fleet: $(printf '%s' "$HW_NON_BENCH_FLEET_LABELS" | tr ' ' '/'))"
                elif [ "$r6_allowed" -eq 1 ] && [ "$ll" = "self-hosted" ]; then
                    violate "$f:$n  R6 'self-hosted' alone reaches EVERY self-hosted runner, the bench included — an allowlisted basename cannot excuse it here (name a different fleet label too, e.g. $(printf '%s' "$HW_NON_BENCH_FLEET_LABELS" | tr ' ' '/'))"
                else
                    violate "$f:$n  R6 bench runner label '$lab' outside the hardware workflow (only $HW_WORKFLOW_NAME may run bench work)"
                fi
            done
        done < <(hw_runs_on_lines "$f")
    fi

    if is_pr_reachable "$f"; then
        # R1a: no bench runner label, and no unverifiable expression-valued runs-on.
        while IFS= read -r hit; do
            [ -n "$hit" ] || continue
            n="${hit%%:*}"
            lin="${hit#*:}"
            val="${lin#*runs-on:}"
            val="${val%%#*}"
            if [ -z "$(printf '%s' "$val" | tr -d '[:space:]')" ]; then
                violate "$f:$n  R1 runs-on with no inline value (block-sequence form is not verifiable by reading — fail closed)"
                continue
            fi
            if printf '%s' "$val" | grep -q '\${{'; then
                violate "$f:$n  R1 expression-valued runs-on:'$val' is not verifiable by reading — PR-reachable bench routing must be a literal label (fail closed)"
                continue
            fi
            for lab in $(printf '%s' "$val" | tr -d "[]\"'" | tr ',' ' '); do
                if is_bench_label "$lab"; then
                    violate "$f:$n  R1 self-hosted runner label '$lab' reachable from pull_request"
                fi
            done
        done < <(yaml_effective "$f" | grep -nE '^[[:space:]]*runs-on:' || true)

        # R1b: no bench-mutating env flag.
        while IFS= read -r hit; do
            [ -n "$hit" ] || continue
            violate "$f:$hit  R1 bench-mutating env flag reachable from pull_request"
        done < <(yaml_effective "$f" | grep -n 'TOLLGATE_ENABLE_' || true)
    fi

    # R3: an `if:` that evaluates falsy parks a job exactly as effectively as the
    # original `if: false` — bare false/no/off, numeric 0, a quoted empty string,
    # or an expression that is literally false.
    while IFS= read -r hit; do
        [ -n "$hit" ] || continue
        violate "$f:$hit  R3 disabled job parked behind a falsy \`if:\`"
    done < <(
        yaml_effective "$f" | grep -nEi "^[[:space:]]*if:[[:space:]]*(false|no|off|0)['\"]?[[:space:]]*$" || true
        yaml_effective "$f" | grep -nE "^[[:space:]]*if:[[:space:]]*(''|\"\")[[:space:]]*$" || true
        yaml_effective "$f" | grep -nEi '^[[:space:]]*if:[[:space:]]*[$]\{\{[^}]*\b(false|off|no|0)\b[^}]*\}\}[[:space:]]*$' || true
    )

    # R3 (folded form): `if: >-` / `if: |` hides its value on the continuation
    # lines, where a line-anchored regex cannot see it.
    while IFS= read -r body; do
        [ -n "$body" ] || continue
        printf '%s' "$body" | grep -qE "$RE_FALSY_GUARD" || continue
        violate "$f  R3 disabled job parked behind a folded falsy \`if:\` ($body)"
    done < <(falsy_folded_if_bodies "$f")
done

# ---------------------------------------------------------------------------
# R2 — the hardware workflow is dispatch/schedule only and must exist
# ---------------------------------------------------------------------------
if [ ! -f "$hw_path" ]; then
    violate "$hw_path  R2 hardware workflow missing (bench work must live in its own dispatch-only workflow)"
else
    if is_pr_reachable "$hw_path"; then
        violate "$hw_path  R2 hardware workflow is reachable from pull_request"
    fi
    while IFS= read -r trig; do
        [ -n "$trig" ] || continue
        case " $HW_TRIGGER_WHITELIST " in
            *" $trig "*) : ;;
            *) violate "$hw_path  R2 trigger '$trig' not allowed (whitelist: $HW_TRIGGER_WHITELIST)" ;;
        esac
    done < <(triggers_of "$hw_path")

    # -----------------------------------------------------------------------
    # R4 + R5 — job-scoped rules, hardware workflow only
    # -----------------------------------------------------------------------
    job_rule_report="$(
        awk -v file="$hw_path" -v wantenv="$HW_ENVIRONMENT_NAME" '
            function envdesc() { return (env_seen ? ("found: " env_name) : "found: none") }
            function flush() {
                if (job == "") return
                if (mutating && !env_ok)
                    printf "VIOLATION %s  R4 job \"%s\" references bench-mutating env flags without an `environment: %s` approval gate (%s)\n", file, job, wantenv, envdesc()
                if (uses_secrets && !env_ok)
                    printf "VIOLATION %s  R5 job \"%s\" uses `secrets.` without an `environment: %s` approval gate (%s)\n", file, job, wantenv, envdesc()
            }
            # Scope to the `jobs:` section: without this, a 2-space key anywhere
            # (a trigger key, a `permissions:` child) is mistaken for a job.
            /^jobs:[[:space:]]*$/ { in_jobs = 1; next }
            in_jobs && /^[^[:space:]]/ { in_jobs = 0 }
            in_jobs && /^  [A-Za-z0-9_-]+:/ {
                flush()
                job = $1; sub(/:.*/, "", job)
                mutating = 0; uses_secrets = 0; env_seen = 0; env_name = ""; env_ok = 0
            }
            job != "" && in_jobs {
                if ($0 ~ /^    environment:/) {
                    env_seen = 1
                    v = $0
                    sub(/^[[:space:]]*environment:[[:space:]]*/, "", v)
                    sub(/[[:space:]]+$/, "", v)
                    if (v != "") { env_name = v; if (v == wantenv) env_ok = 1 }
                } else if (env_seen && env_name == "" && $0 ~ /^      name:/) {
                    v = $0
                    sub(/^[[:space:]]*name:[[:space:]]*/, "", v)
                    sub(/[[:space:]]+$/, "", v)
                    env_name = v
                    if (v == wantenv) env_ok = 1
                }
                if ($0 ~ /TOLLGATE_ENABLE_(WIFI_CLIENT|DATA_ALLOTMENT)_TESTS/) mutating = 1
                if ($0 ~ /secrets\./) uses_secrets = 1
            }
            END { flush() }
        ' "$hw_path"
    )"
    if [ -n "$job_rule_report" ]; then
        while IFS= read -r line; do
            [ -n "$line" ] || continue
            printf '%s\n' "$line"
            violations=$((violations + 1))
        done <<< "$job_rule_report"
    fi

    # Fail closed if the job structure was not parseable at all. The awk matches
    # job keys at exactly two spaces INSIDE the `jobs:` section, so a file whose
    # jobs are indented differently would silently skip R4/R5 — the same
    # "unreadable YAML form passes by default" class as the `on:` checks above.
    jobs_parsed="$(yaml_effective "$hw_path" | awk '
        /^jobs:[[:space:]]*$/ { f = 1; next }
        f && /^[^[:space:]]/ { f = 0 }
        f && /^  [A-Za-z0-9_-]+:/ { n++ }
        END { print n + 0 }
    ')"
    if [ "${jobs_parsed:-0}" -eq 0 ]; then
        violate "$hw_path  R4/R5 job structure is not parseable at the expected indentation (fail closed)"
    fi
fi

echo
if [ "$violations" -eq 0 ]; then
    note "OK — no workflow reachable from pull_request can reach the bench."
    exit 0
fi
note "FAILED — $violations violation(s). See rules R1-R6 in $(basename "$0")."
exit 1
