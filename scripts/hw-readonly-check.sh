#!/usr/bin/env bash
# =============================================================================
# Read-only bench surface check — the non-destructive lane of hw-smoke.yml
# =============================================================================
# WHAT THIS PROVES: the bench is alive, answering, and in FULL mode (real
# pricing reachable) — WITHOUT touching it. Nothing here authenticates, pays,
# reboots, flashes, writes config, or takes the hardware lock, so it can run
# while another agent is mid-run and while a merchant session is live.
#
# This is the "an agent must be able to run a non-destructive surface check
# without spending paid traffic or deauthorising the bench's existing session"
# primitive. Every mutating bench lane must run it FIRST (see hw-smoke.yml).
#
# GATES (fail-closed unless noted)
#   G0  Refuse to run at all if a bench-mutating spec flag is enabled
#       (TOLLGATE_ENABLE_WIFI_CLIENT_TESTS / TOLLGATE_ENABLE_DATA_ALLOTMENT_TESTS).
#       A read-only lane that silently carries mutating flags is how the bench
#       gets corrupted for the next agent.
#   G1  tollgate API :2121/ answers 200 AND is in FULL mode — `kind: 10021` with
#       `price_per_step`. Degraded mode is a FAIL, never adapted to: a green
#       check on a broken router is worse than a red one.
#   G2  :2121/balance answers and is parseable. Reports `session_active`; an
#       unreadable/absent balance is treated as NOT idle (fail closed).
#   G3  scripts/tollgate-port-sweep.sh, when present in the tree, must pass —
#       that is the guest-visible port surface. Absence is a loud PARTIAL, not a
#       silent pass (the sweep script currently lives on the
#       fix/nodogsplash-443-regression-guard branch).
#
# USAGE
#   bash scripts/hw-readonly-check.sh [--host 192.168.1.1] [--api-port 2121] [--no-sweep]
# ENV
#   TOLLGATE_ROUTER_HOST   bench host (default 192.168.1.1)
#   TOLLGATE_API_PORT      tollgate API port (default 2121)
#   TOLLGATE_PROBE_TIMEOUT per-request connect timeout, seconds (default 8)
# EXIT
#   0 = every required gate passed   1 = a gate failed   2 = refused to run
# =============================================================================
set -uo pipefail

HOST="${TOLLGATE_ROUTER_HOST:-192.168.1.1}"
API_PORT="${TOLLGATE_API_PORT:-2121}"
TIMEOUT="${TOLLGATE_PROBE_TIMEOUT:-8}"
RUN_SWEEP=1

while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="${2:?--host needs a value}"; shift 2 ;;
        --api-port) API_PORT="${2:?--api-port needs a value}"; shift 2 ;;
        --no-sweep) RUN_SWEEP=0; shift ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ -t 1 ]; then GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'
else GREEN=''; RED=''; YELLOW=''; DIM=''; RST=''; fi

gates_failed=0
partial=0
full_mode="unknown"
session_active="unknown"
sweep_result="skipped"

say()  { printf '%s\n' "$*"; }
ok()   { printf '  %sok  %s %s\n' "$GREEN" "$RST" "$*"; }
bad()  { gates_failed=$((gates_failed + 1)); printf '  %sFAIL%s %s\n' "$RED" "$RST" "$*"; }
warn() { partial=1; printf '  %sWARN%s %s\n' "$YELLOW" "$RST" "$*"; }

# ---------------------------------------------------------------------------
# G0 — refuse to run if this is secretly a mutating run
# ---------------------------------------------------------------------------
say "HW read-only bench surface check"
say "  host    : ${HOST}"
say "  api port: ${API_PORT}"
say "  client  : $(hostname)  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
say "  mode    : READ-ONLY — no credentials, no payment, no router writes"
say

say "== G0 mutating-flag refusal =="
mutating_flags=0
for f in TOLLGATE_ENABLE_WIFI_CLIENT_TESTS TOLLGATE_ENABLE_DATA_ALLOTMENT_TESTS; do
    v="${!f:-}"
    case "$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on)
            mutating_flags=$((mutating_flags + 1))
            say "  ${RED}REFUSING${RST} ${f}=${v} is set — this is a read-only lane"
            ;;
    esac
done
if [ "$mutating_flags" -gt 0 ]; then
    say
    say "Set the mutating flags only on the bench-hardware-gated lane (hw-smoke.yml, lane=mutating)."
    exit 2
fi
ok "no bench-mutating spec flag enabled"

# ---------------------------------------------------------------------------
# G1 — tollgate API answers, and answers in FULL mode
# ---------------------------------------------------------------------------
say
say "== G1 tollgate API full-mode =="
api_out="$(curl -s -k -w $'\n%{http_code}' \
    --connect-timeout "$TIMEOUT" --max-time $((TIMEOUT * 2)) \
    "http://${HOST}:${API_PORT}/" 2>/dev/null)" || api_out=$'\n000'
api_code="${api_out##*$'\n'}"
api_body="${api_out%$'\n'*}"
[ -n "$api_code" ] || api_code="000"

if [ "$api_code" != "200" ]; then
    bad ":${API_PORT}/ answered ${api_code} (want 200) — is the tollgate service up?"
else
    ok ":${API_PORT}/ answered 200"
fi

if printf '%s' "$api_body" | grep -qE '"kind"[[:space:]]*:[[:space:]]*10021'; then
    if printf '%s' "$api_body" | grep -q 'price_per_step'; then
        full_mode="full"
        ok "advert is kind:10021 WITH price_per_step tags (FULL mode)"
    else
        full_mode="degraded"
        bad "kind:10021 but NO price_per_step tags — DEGRADED mode. Fix upstream before capturing any evidence or running any test."
    fi
else
    full_mode="unknown"
    bad "no kind:10021 advert on :${API_PORT}/ — router not serving an offer"
fi

# ---------------------------------------------------------------------------
# G2 — balance readable, and is it idle?
# ---------------------------------------------------------------------------
say
say "== G2 session state (read-only) =="
bal_out="$(curl -s -k -w $'\n%{http_code}' \
    --connect-timeout "$TIMEOUT" --max-time $((TIMEOUT * 2)) \
    "http://${HOST}:${API_PORT}/balance" 2>/dev/null)" || bal_out=$'\n000'
bal_code="${bal_out##*$'\n'}"
bal_body="${bal_out%$'\n'*}"

if [ "$bal_code" != "200" ]; then
    bad "/balance answered ${bal_code} (want 200) — treating the bench as NOT idle (fail closed)"
else
    parsed="$(printf '%s' "$bal_body" \
        | grep -oE '"session_active"[[:space:]]*:[[:space:]]*(true|false)' \
        | head -1 | sed -E 's/.*:[[:space:]]*//')"
    if [ -z "$parsed" ]; then
        bad "/balance is not parseable for session_active — treating the bench as NOT idle (fail closed)"
    else
        session_active="$parsed"
        ok "/balance parseable — session_active=${parsed}"
    fi
fi

# ---------------------------------------------------------------------------
# G3 — guest-visible port surface (optional script, loud when absent)
# ---------------------------------------------------------------------------
say
say "== G3 pre-auth port sweep =="
if [ "$RUN_SWEEP" -eq 0 ]; then
    warn "sweep skipped by --no-sweep — this run is PARTIAL"
elif [ -f scripts/tollgate-port-sweep.sh ]; then
    if bash scripts/tollgate-port-sweep.sh --host "$HOST"; then
        sweep_result="pass"
        ok "port sweep passed"
    else
        sweep_result="fail"
        bad "port sweep FAILED (see the sweep table above)"
    fi
else
    warn "scripts/tollgate-port-sweep.sh not in this tree — surface sweep SKIPPED (PARTIAL run). The sweep lives on the fix/nodogsplash-443-regression-guard branch."
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
say
say "== summary =="
say "  full_mode      : ${full_mode}"
say "  session_active : ${session_active}"
say "  port_sweep     : ${sweep_result}"
say "  gates_failed   : ${gates_failed}"
say "  partial        : ${partial}"
say
say "{\"check\":\"hw-readonly\",\"host\":\"${HOST}\",\"api_port\":${API_PORT},\"full_mode\":\"${full_mode}\",\"session_active\":\"${session_active}\",\"port_sweep\":\"${sweep_result}\",\"gates_failed\":${gates_failed},\"partial\":${partial}}"

if [ "$gates_failed" -gt 0 ]; then
    say "RESULT: FAIL"
    exit 1
fi
if [ "$partial" -eq 1 ]; then
    say "RESULT: PASS (PARTIAL)"
    exit 0
fi
say "RESULT: PASS"
exit 0
