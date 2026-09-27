#!/usr/bin/env bash
# Pre-auth port / UI sweep for a TollGate router.
#
# Run this from a client that is attached to the router's CAPTIVE LAN and has
# NOT paid — i.e. exactly the position a guest is in. Every check below is
# read-only over HTTP(S): no SSH, no credentials, nothing on the router is
# modified. Exit status is 0 only when every expected response matched.
#
# Why this exists: nodogsplash REJECTs every port that its `users_to_router`
# allow-list does not name, so a service can be "running and healthy" on the
# router yet be unreachable for the guest standing in front of it. That is not
# visible from the router's own shell (loopback bypasses nodogsplash) and it is
# not visible to a config-grep test. The checks that matter most are the LuCI
# pair: :8080 answers `307 Location: https://<router>/` when HTTPS is
# provisioned, so :443 must answer too, or the operator's journey dead-ends.
#
# Usage:
#   bash scripts/tollgate-port-sweep.sh [--host 192.168.8.1]
# Environment:
#   TOLLGATE_ROUTER_HOST    router address (default 192.168.8.1)
#   TOLLGATE_PROBE_TIMEOUT  per-request connect timeout, seconds (default 8)
set -uo pipefail

HOST="${TOLLGATE_ROUTER_HOST:-192.168.8.1}"
if [ "${1:-}" = "--host" ]; then
    HOST="${2:-$HOST}"
fi
TIMEOUT="${TOLLGATE_PROBE_TIMEOUT:-8}"
MAXTIME=$((TIMEOUT * 2))

PASS=0
FAIL=0

if [ -t 1 ]; then GREEN=$'\033[32m'; RED=$'\033[31m'; DIM=$'\033[2m'; RST=$'\033[0m'
else GREEN=''; RED=''; DIM=''; RST=''; fi

pass() { PASS=$((PASS + 1)); printf '  %sok  %s %-40s %s\n' "$GREEN" "$RST" "$1" "$2"; }
fail() { FAIL=$((FAIL + 1)); printf '  %sFAIL%s %-40s %s\n' "$RED" "$RST" "$1" "$2"; }

# check <label> <url> <expected-code> [extra curl args...]
check() {
    local label="$1" url="$2" want="$3"; shift 3
    local got
    got=$(curl -s -k -o /dev/null -w '%{http_code}' \
        --connect-timeout "$TIMEOUT" --max-time "$MAXTIME" "$@" "$url" 2>/dev/null) || got='000'
    [ -n "$got" ] || got='000'
    if [ "$got" = "$want" ]; then
        pass "$label" "$got ${DIM}(want $want)$RST"
    else
        fail "$label" "$got ${DIM}(want $want)$RST"
    fi
}

# check_in <label> <url> <expected-code> <body-regex>
# A captive portal can answer 200 with a splash page, so the body is checked
# too: the status alone does not prove the operator reached the service.
check_in() {
    local label="$1" url="$2" want="$3" regex="$4"
    local out code body
    out=$(curl -s -k -w $'\n%{http_code}' \
        --connect-timeout "$TIMEOUT" --max-time "$MAXTIME" "$url" 2>/dev/null) || out=$'\n000'
    code="${out##*$'\n'}"
    body="${out%$'\n'*}"
    if [ "$code" = "$want" ] && printf '%s' "$body" | grep -qiE "$regex"; then
        pass "$label" "$code + body /$regex/"
    else
        fail "$label" "$code ${DIM}(want $want + body /$regex/)$RST"
    fi
}

# check_tcp <label> <port> — a listening port the guest may reach at all.
check_tcp() {
    local label="$1" port="$2"
    if (exec 3<>"/dev/tcp/${HOST}/${port}") 2>/dev/null; then
        pass "$label" "open"
    else
        fail "$label" "closed"
    fi
}

echo "TollGate pre-auth sweep against ${HOST}"
echo "client side of the captive portal, unauthenticated — $(hostname), $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

echo "== reachable ports =="
check_tcp "ssh               (22)" 22
check "tollgate API     (2121)" "http://${HOST}:2121/" "200"
check "admin board HTTP (8090)" "http://${HOST}:8090/" "200"
check "admin board TLS  (8443)" "https://${HOST}:8443/" "200"
check "captive portal   (2051)" "http://${HOST}:2051/" "403"
check "nodogsplash      (2050)" "http://${HOST}:2050/" "200"

echo
echo "== LuCI: the HTTP listener must reach the TLS listener it redirects to =="
REDIR=$(curl -s -k -o /dev/null -w '%{redirect_url}' \
    --connect-timeout "$TIMEOUT" --max-time "$MAXTIME" "http://${HOST}:8080/" 2>/dev/null || printf '')
if [ -n "$REDIR" ]; then
    pass "LuCI :8080 redirect target" "$REDIR"
else
    check "LuCI             (8080)" "http://${HOST}:8080/" "307"
fi
check_in "LuCI TLS         (443)" "https://${HOST}/" "200" "luci|tollgate|overview"
check "LuCI :8080 followed (-L)" "http://${HOST}:8080/" "200" -L

echo
TOTAL=$((PASS + FAIL))
if [ "$FAIL" -eq 0 ]; then
    printf '%sall %d checks passed%s\n' "$GREEN" "$TOTAL" "$RST"
    exit 0
fi
printf '%s%d of %d checks FAILED%s\n' "$RED" "$FAIL" "$TOTAL" "$RST"
exit 1
