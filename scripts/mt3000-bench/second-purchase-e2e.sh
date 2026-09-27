#!/usr/bin/env bash
#
# second-purchase-e2e.sh — does a SECOND purchase re-open the gate on the bench MT3000?
#
# The reported product bug: a client buys, exhausts the allotment, and the gate never
# re-opens for a second purchase. This is the clean, reproducible reproduction: a fresh
# MAC (never seen by nodogsplash) buys, exhausts the allotment by downloading THROUGH the
# router, the post-exhaustion state is captured, the `ndsctl deauth` discriminator runs,
# and a second token is spent.
#
# DRY RUN BY DEFAULT. Nothing is purchased, no interface is created and no bench lock is
# taken until you pass --purchase (or PURCHASE=1). A dry run needs no router at all.
#
# THE TRAP THAT MAKES EVERY OTHER ATTEMPT INVALID
#   The module authorises the MAC of the REQUESTING SOCKET, not a `?mac=` parameter. A
#   purchase POSTed from the bench host with `?mac=<other>` authenticates the HOST. So every
#   purchase is issued THROUGH the client's own interface (`curl --interface <client-ip>`),
#   and the client's routes live in a separate policy table so the host's own management
#   path to the router survives the run.
#
# USAGE
#   scripts/mt3000-bench/second-purchase-e2e.sh                     # dry run: print the plan
#   scripts/mt3000-bench/second-purchase-e2e.sh --purchase          # the real, paid run
#   TOKEN_1=... TOKEN_2=... scripts/mt3000-bench/second-purchase-e2e.sh --purchase
#
#   # the same thing, the way the kit runs bench work (single owner, named holder):
#   bench-with-lock.sh --purpose "second purchase e2e" -- \
#     scripts/mt3000-bench/second-purchase-e2e.sh --purchase
#   # ...or let the script take the lock itself (it re-execs under `bench-lock.sh exec`).
#
#   # a REAL WIRELESS client (run this ON the client host, as the client's own user):
#   # the guest SSID's client is a station that cannot present a second MAC, so the
#   # macvlan is skipped and the station's OWN interface/address/MAC are adopted.
#   # The rule that makes it work: the policy table must also carry the ON-LINK route
#   # for the client's own subnet, or the client's replies to any other br-lan host
#   # (and to a NetBird/WireGuard peer on the same L2) are sent to the router instead
#   # of over the link and die there — the client looks half-deaf while every probe
#   # THROUGH the router still passes.
#   ssh <client-host> \
#     "CLIENT_IFACE=wlp2s0 LANE=ln bench-lock.sh exec --purpose 'second purchase e2e' -- \
#        <kit>/scripts/mt3000-bench/second-purchase-e2e.sh --purchase"
#
# LANES
#   LANE=token (default)  spend a Cashu token at POST / — the allotment is
#                         amount x step_size (a 64 sat token = 63 steps = 1.29 GiB).
#   LANE=ln               the portal's Lightning-invoice lane, the one the operator
#                         reported: POST /ln-invoice -> poll until access_granted.
#                         One step per purchase by default (21 MiB), settles itself on
#                         the testnut FakeWallet. Needs no token files.
#
# TOKENS ARE SINGLE-USE (LANE=token only)
#   Both tokens are NUT-07-verified UNSPENT immediately before the paid phases; the run
#   fails closed (exit 14) if the mint cannot answer. Mint/verify with `bench-token.py`
#   from this directory.
#
# THE BOX MUST NOT RESTART UNDER THE RUN — WITH ONE RECORDED, ATTRIBUTABLE EXCEPTION
#   The run pins the box at PHASE 0 — router uptime, `ndsctl status` Uptime, the nodogsplash
#   pid and the tollgate-wrt pid — and re-reads all four at EVERY phase boundary. If any of
#   them moved UNEXPECTEDLY (a reboot, or either daemon restarted), the run prints
#   "THE BOX RESTARTED UNDER THE TEST — RESULT INVALID", dumps the restart-cause log lines and
#   exits 15. Nothing after such a restart is interpretable: the module comes back with no
#   tracked sessions while nodogsplash still holds clients, and the close path retries forever.
#   THE ONE DELIBERATE RESTART IS PHASE 5b'S FORCING STEP (FORCE_DRIFT=restart, the default):
#   nodogsplash is restarted ON PURPOSE, because a drifted session with an open allotment
#   produces NO close attempt at all — the close has to be FORCED before the failing deauth can
#   happen (measured 2026-09-26: N_DEAUTH=0 N_UNCONF=0 in the window). That restart is
#     * announced in the ROUTER'S OWN log as a BENCH ACTION before it happens (an unattributed
#     restart once invalidated a sibling's run and was mistaken for a product self-restart),
#   * printed in the transcript with the nodogsplash pid change,
#   * guarded: tollgate-wrt must NOT have moved with it — if the MODULE also restarted, that is
#     not our step, it is a product self-restart, and the run is invalid (exit 15),
#   * followed by a RE-PIN of the box baseline, so every later boundary is judged against the
#     post-restart box and everything measured stays attributable.
#   Any OTHER move during the run remains fatal. A bounce needed for any other reason (a wedged
#   ndsctl socket) belongs in PHASE 0, BEFORE the baseline is recorded — doing it ad hoc mid-run
#   is what makes a transcript meaningless.
#
# EXIT CODES
#   0  ran to the end; every assertion held
#   2  usage / preflight failed (missing token file, degraded mode, no wired NIC)
#   3  the bench lock is held by another window (refused, holder named)  [bench-lock's own code]
#   4  not inside a bench window (bench-lock require failed)            [bench-lock's own code]
#   5  bench-lock refused a STALE holder line — its owner died; recovery is explicit only
#      (`bench-lock.sh take --reclaim-stale`), and only an operator clears an orphan
#   10 the FIRST purchase did not open the gate (setup problem, not the bug)
#   11 the allotment never exhausted inside the budget (INCONCLUSIVE — not a pass)
#   12 the SECOND purchase did not re-open the gate (the reported bug reproduced)
#   13 a phase assertion failed (see ASSERT FAIL lines in the transcript)
#   14 the tokens are not spendable (NUT-07 says spent/pending, or the mint is unreachable)
#   15 THE BOX RESTARTED UNDER THE TEST — nodogsplash/tollgate-wrt pid, or an uptime, moved
#      mid-run; the whole result is INVALID (not a pass, not a fail — rerun it)
#
# ENV (all overridable; no user-specific path is baked in)
#   ROUTER_IP         192.168.1.1                 the bench router
#   BENCH_NIC         unset -> auto-detect        the WIRED host NIC on the router's /24
#   CLIENT_IFACE      unset -> create a macvlan   adopt an EXISTING interface instead
#                                                 (e.g. wlp2s0 on the guest SSID): no
#                                                 macvlan, no address add, no link delete
#   CLIENT_VIF        tg-club                     the macvlan interface to create
#   CLIENT_MAC        02:11:22:33:44:55           a MAC the router has never seen
#   CLIENT_IP         <router>/24 + .222          the client's source address
#   LANE              token                       token (POST /) | ln (POST /ln-invoice)
#   MINT_URL          https://testnut.cashu.exchange   LANE=ln: mint for the invoice
#   LN_AMOUNT         1                           LANE=ln: sats per purchase (= steps)
#   LN_POLLS          30                          LANE=ln: quote polls (2 s apart)
#   POLICY_TABLE      100                         policy-routing table for the client
#   POLICY_PRIORITY   100                         ip-rule priority
#   API_BASE          http://$ROUTER_IP:2121       the tollgate backend
#   PROBE_URL         connectivitycheck.gstatic.com/generate_204
#   EGRESS_URL        speed.cloudflare.com __down?bytes=2000000
#   TOKEN_1, TOKEN_2  REQUIRED for --purchase     paths to two cashu token files
#   LOG_DIR           ~/.tg-e2e/second-purchase   transcripts + download logs
#   LOG_TO_FILE       1                           transcript to $LOG_DIR/... (0 = stdout only)
#   BURN_ROUNDS       12                          exhaustion rounds
#   BURN_PARALLEL     6                           parallel downloads per round
#   BURN_URLS         a 6-URL default list        space-separated large-file URLs
#   WATCH_TRIES       6                           gate polls per round (15 s apart)
#   GATE_STRIKES      2                           consecutive non-204/200 probes = "closed"
#   PROBE_TRIES       12                          gate polls after a purchase (10 s apart)
#   SETTLE_BUDGET     180                         PHASE 5b: seconds to wait for the module to settle an
#                                                 address nodogsplash no longer knows
#   SETTLE_WINDOW     45                          PHASE 5b: seconds between the two unconfirmed-closes samples
#   FORCE_DRIFT       restart                     PHASE 5b's forcing step:
#                                                 `restart` — restart nodogsplash DELIBERATELY (the
#                                                 deterministic reproducer: the module's sweeps then find
#                                                 the session's counters unreadable, so the close happens
#                                                 at all and `ndsctl deauth` answers "Client <mac> not
#                                                 found." rc=1 — the failing deauth the defect needs);
#                                                 `deauth` — the weaker ndsctl-only step the lane shipped
#                                                 with (kept for the ablation: on its own it produced
#                                                 N_DEAUTH=0 N_UNCONF=0, i.e. no close attempt at all)
#   CLEAN_PAIR_EPILOGUE 1                         after PHASE 5b, bounce nodogsplash + tollgate-wrt so the
#                                                 bench is left as a clean pair for the next run (0 = leave
#                                                 it exactly as the phase left it; the transcript records
#                                                 which of the two happened, and the bounce is attributable)
#   ROUTER_PW_FILE    ~/.tg-e2e/pw                for router-snapshot.sh
#
set -uo pipefail

EX_OK=0
EX_USAGE=2
EX_LOCK=3
EX_NO_GATE=10
EX_NO_EXHAUST=11
EX_NO_REOPEN=12
EX_ASSERT=13
EX_TOKEN=14
EX_BOX_CHANGED=15

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
HERE="$(cd "$(dirname "$SELF")" && pwd)"
SNAP="$HERE/router-snapshot.sh"
TOKENTOOL="$HERE/bench-token.py"

# ---------------------------------------------------------------- configuration

ROUTER_IP="${ROUTER_IP:-192.168.1.1}"
BENCH_NIC="${BENCH_NIC:-}"
CLIENT_IFACE="${CLIENT_IFACE:-}"
CLIENT_VIF="${CLIENT_VIF:-tg-club}"
CLIENT_MAC="${CLIENT_MAC:-02:11:22:33:44:55}"
CLIENT_IP="${CLIENT_IP:-${ROUTER_IP%.*}.222}"
LANE="${LANE:-token}"
MINT_URL="${MINT_URL:-https://testnut.cashu.exchange}"
LN_AMOUNT="${LN_AMOUNT:-1}"
LN_POLLS="${LN_POLLS:-30}"
POLICY_TABLE="${POLICY_TABLE:-100}"
POLICY_PRIORITY="${POLICY_PRIORITY:-100}"
API_BASE="${API_BASE:-http://$ROUTER_IP:2121}"
PROBE_URL="${PROBE_URL:-http://connectivitycheck.gstatic.com/generate_204}"
EGRESS_URL="${EGRESS_URL:-https://speed.cloudflare.com/__down?bytes=2000000}"
TOKEN_1="${TOKEN_1:-}"
TOKEN_2="${TOKEN_2:-}"
LOG_DIR="${LOG_DIR:-${BENCH_LOG_DIR:-$HOME/.tg-e2e/second-purchase}}"
LOG_TO_FILE="${LOG_TO_FILE:-1}"
BURN_ROUNDS="${BURN_ROUNDS:-12}"
BURN_PARALLEL="${BURN_PARALLEL:-6}"
WATCH_TRIES="${WATCH_TRIES:-6}"
GATE_STRIKES="${GATE_STRIKES:-2}"
PROBE_TRIES="${PROBE_TRIES:-12}"
SETTLE_BUDGET="${SETTLE_BUDGET:-180}"
SETTLE_WINDOW="${SETTLE_WINDOW:-45}"
FORCE_DRIFT="${FORCE_DRIFT:-restart}"
CLEAN_PAIR_EPILOGUE="${CLEAN_PAIR_EPILOGUE:-1}"
BURN_URLS="${BURN_URLS:-https://ash-speed.hetzner.com/100MB.bin https://fsn1-speed.hetzner.com/100MB.bin https://proof.ovh.net/files/100Mb.dat http://ipv4.download.thinkbroadband.com/100MB.zip http://speedtest.tele2.net/100MB.zip https://speed.cloudflare.com/__down?bytes=104857600}"

PURCHASE="${PURCHASE:-0}"
DETACHED="${DETACHED:-0}"

die() { printf 'second-purchase-e2e: %s\n' "$*" >&2; exit "${EX_USAGE}"; }
say() { printf '\n########## %s ##########\n' "$*"; date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# A wireless client is a real station: it cannot present a second MAC (so no macvlan) and
# it owns the address it got from the AP. EXISTING_IFACE=1 switches the whole client setup
# and teardown to that mode.
if [ -n "$CLIENT_IFACE" ]; then EXISTING_IFACE=1; else EXISTING_IFACE=0; fi

# The usage text is the file's own header comment: it cannot drift from the code.
usage() {
  grep -E '^#' "$SELF" | grep -v '^#!' | sed 's/^# \{0,1\}//'
}

FAILED=0
assert_eq() {   # $1 desc  $2 expected  $3 actual
  if [ "$2" = "$3" ]; then
    printf 'ASSERT PASS  %s (=%s)\n' "$1" "$3"
  else
    printf 'ASSERT FAIL  %s: expected %s, got %s\n' "$1" "$2" "$3"
    FAILED=$((FAILED + 1))
  fi
}
assert_contains() {   # $1 desc  $2 needle  $3 haystack
  case "$3" in
    *"$2"*) printf 'ASSERT PASS  %s\n' "$1" ;;
    *) printf 'ASSERT FAIL  %s: output does not contain %s\n' "$1" "$2"
       printf '%s\n' "$3" | sed 's/^/      | /'
       FAILED=$((FAILED + 1)) ;;
  esac
}
assert_not_contains() {   # $1 desc  $2 forbidden  $3 haystack
  case "$3" in
    *"$2"*) printf 'ASSERT FAIL  %s: output DOES contain %s\n' "$1" "$2"
       printf '%s\n' "$3" | sed 's/^/      | /'
       FAILED=$((FAILED + 1)) ;;
    *) printf 'ASSERT PASS  %s\n' "$1" ;;
  esac
}

# ---------------------------------------------------------------- args

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit "$EX_OK" ;;
    --purchase) PURCHASE=1; shift ;;
    --detached) DETACHED=1; shift ;;
    --log-dir) [ $# -ge 2 ] || die "--log-dir needs a value"; LOG_DIR="$2"; shift 2 ;;
    --burn-rounds) [ $# -ge 2 ] || die "--burn-rounds needs a value"; BURN_ROUNDS="$2"; shift 2 ;;
    --client-mac) [ $# -ge 2 ] || die "--client-mac needs a value"; CLIENT_MAC="$2"; CLIENT_MAC_EXPLICIT=1; shift 2 ;;
    --client-ip) [ $# -ge 2 ] || die "--client-ip needs a value"; CLIENT_IP="$2"; CLIENT_IP_EXPLICIT=1; shift 2 ;;
    --client-iface) [ $# -ge 2 ] || die "--client-iface needs a value"; CLIENT_IFACE="$2"; EXISTING_IFACE=0; [ -n "$CLIENT_IFACE" ] && EXISTING_IFACE=1; shift 2 ;;
    --lane) [ $# -ge 2 ] || die "--lane needs a value"; LANE="$2"; shift 2 ;;
    --nic) [ $# -ge 2 ] || die "--nic needs a value"; BENCH_NIC="$2"; shift 2 ;;
    -*) die "unknown option '$1' (see --help)" ;;
    *) die "unexpected argument '$1' (see --help)" ;;
  esac
done

TS="$(date -u '+%Y%m%dT%H%M%SZ')"

# ---------------------------------------------------------------- helpers

norm_probe() { case "$1" in 204|200) printf 'open' ;; *) printf '%s' "$1" ;; esac; }

detect_nic() {   # $1 = first three octets of the router address
  # The client must be a MACVLAN on a WIRED NIC: a macvlan on Wi-Fi cannot present a second
  # MAC through an AP association (every probe returns 000). Never pick the default-route
  # interface — on this host that is Wi-Fi.
  local want3="$1" line iface cidr
  while read -r line; do
    [ -n "$line" ] || continue
    iface="$(printf '%s' "$line" | awk '{print $2}')"
    cidr="$(printf '%s' "$line" | awk '{for (i=1;i<=NF;i++) if ($i=="inet") print $(i+1)}')"
    iface="${iface%%@*}"
    [ "$iface" = "lo" ] && continue
    [ "$iface" = "$CLIENT_VIF" ] && continue
    case "${cidr%.*}" in
      "$want3") printf '%s\n' "$iface"; return 0 ;;
    esac
  done < <(ip -o -4 addr show 2>/dev/null)
  return 1
}

json_field() { printf '%s' "$2" | sed -n "s/.*\"$1\":\"\([^\"]*\)\".*/\1/p" | head -1; }
# tolerant scalar read: the module answers numbers and booleans BARE ("allotment":22020096,
# "access_granted":true), which the quoted-only json_field above cannot see at all.
json_val() {
  printf '%s' "$2" | grep -o "\"$1\":[^,}]*" | head -1 | sed "s/^\"$1\"://; s/^\"//; s/\"$//"
}

# ---------------------------------------------------------------- plan (always printed)

# The forcing step PHASE 5b will use. Validated BEFORE the plan and before the bench lock, so a
# typo is a usage error (exit 2) rather than a surprise after the tokens have been spent.
case "$FORCE_DRIFT" in
  restart|deauth) ;;
  *) die "FORCE_DRIFT must be 'restart' (a deliberate nodogsplash restart — the deterministic reproducer) or 'deauth' (the weaker ndsctl-only step, kept for the ablation), not '$FORCE_DRIFT'" ;;
esac

printf 'second-purchase-e2e  mode=%s\n' "$([ "$PURCHASE" = 1 ] && printf 'PURCHASE' || printf 'DRY-RUN')"
printf '  router            %s  (%s)\n' "$ROUTER_IP" "$API_BASE"
printf '  lane              %s%s\n' "$LANE" \
  "$([ "$LANE" = ln ] && printf ' (portal Lightning invoice, %s sats = %s step(s))' "$LN_AMOUNT" "$LN_AMOUNT" || printf ' (cashu token at /)')"
if [ "$EXISTING_IFACE" = 1 ]; then
  printf '  client            EXISTING interface %s, adopted address/MAC, policy table %s (priority %s)\n' \
    "$CLIENT_IFACE" "$POLICY_TABLE" "$POLICY_PRIORITY"
  printf '  host NIC          <not used: a station cannot present a second MAC, so no macvlan>\n'
else
  printf '  client            %s on vif %s, mac %s, policy table %s (priority %s)\n' \
    "$CLIENT_IP" "$CLIENT_VIF" "$CLIENT_MAC" "$POLICY_TABLE" "$POLICY_PRIORITY"
  printf '  host NIC          %s\n' "${BENCH_NIC:-<auto-detect: first wired NIC on the router /24>}"
fi
if [ "$LANE" = token ]; then
  printf '  tokens            TOKEN_1=%s TOKEN_2=%s\n' "${TOKEN_1:-<unset>}" "${TOKEN_2:-<unset>}"
else
  printf '  tokens            <not used by LANE=ln>\n'
fi
printf '  exhaustion        %s rounds x %s parallel downloads\n' "$BURN_ROUNDS" "$BURN_PARALLEL"
printf '  log dir           %s\n' "$LOG_DIR"
printf '  phases            0 fresh-MAC baseline -> 1 buy#1 -> 2 exhaust -> 3 post-exhaustion\n'
printf '                    -> 4 ndsctl deauth discriminator -> 5 buy#2 (does the gate re-open?)\n'
printf '                    -> 5b FORCE the drift, then check the module converges on an address\n'
printf '                       nodogsplash no longer knows\n'
printf '  forcing step      PHASE 5b: %s\n' \
  "$([ "$FORCE_DRIFT" = restart ] \
      && printf 'a DELIBERATE, attributable nodogsplash restart (the deterministic reproducer)' \
      || printf 'ndsctl deauth only (the ablation — on its own it produced NO close attempt at all)')"

if [ "$PURCHASE" != 1 ]; then
  printf '\nDRY-RUN: nothing was purchased, no interface was created, no bench lock was taken.\n'
  if [ "$LANE" = token ]; then
    printf 'Re-run with --purchase (or PURCHASE=1) to spend TOKEN_1 and TOKEN_2.\n'
  else
    printf 'Re-run with --purchase (or PURCHASE=1) to buy on the Lightning-invoice lane.\n'
  fi
  exit "$EX_OK"
fi

# ---------------------------------------------------------------- preflight (no bench lock yet)

for t in curl ip awk sed mktemp python3; do
  command -v "$t" >/dev/null 2>&1 || die "missing required tool: $t"
done
[ -x "$SNAP" ] || die "missing helper: $SNAP"
[ -f "$TOKENTOOL" ] || die "missing helper: $TOKENTOOL"
case "$LANE" in token|ln) ;; *) die "LANE=$LANE is not a lane (token|ln)" ;; esac

if [ "$LANE" = token ]; then
  [ -n "$TOKEN_1" ] || die "TOKEN_1 is required for --purchase on LANE=token (path to a cashu token file)"
  [ -n "$TOKEN_2" ] || die "TOKEN_2 is required for --purchase on LANE=token (both tokens are spent; they must differ)"
  [ "$TOKEN_1" != "$TOKEN_2" ] || die "TOKEN_1 and TOKEN_2 are the same file; the second purchase needs a fresh token"
  for t in "$TOKEN_1" "$TOKEN_2"; do
    [ -s "$t" ] || die "token file is missing or empty: $t"
  done
fi

if [ "$EXISTING_IFACE" = 1 ]; then
  # The station ADOPTS its own interface: it cannot present a second MAC through an AP
  # association, so a macvlan would be dead on the wire (every probe 000). The MAC the
  # router authorises is therefore the interface's real, hardware MAC — stated plainly so
  # nobody reads the run as a fresh-MAC one.
  [ -d "/sys/class/net/$CLIENT_IFACE" ] || die "CLIENT_IFACE=$CLIENT_IFACE does not exist"
  [ -z "${CLIENT_MAC_EXPLICIT:-}" ] && CLIENT_MAC="$(cat "/sys/class/net/$CLIENT_IFACE/address")"
  if [ -z "${CLIENT_IP_EXPLICIT:-}" ]; then
    CLIENT_IP="$(ip -o -4 addr show dev "$CLIENT_IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)"
    [ -n "$CLIENT_IP" ] || die "CLIENT_IFACE=$CLIENT_IFACE has no IPv4 address — is the station associated (DHCP done)?"
  fi
  printf 'preflight: EXISTING interface %s: mac=%s ip=%s operstate=%s  <-- the router will authorise THIS mac\n' \
    "$CLIENT_IFACE" "$CLIENT_MAC" "$CLIENT_IP" "$(cat "/sys/class/net/$CLIENT_IFACE/operstate" 2>/dev/null || echo '?')"
else
  if [ -z "$BENCH_NIC" ]; then
    BENCH_NIC="$(detect_nic "${ROUTER_IP%.*}")" \
      || die "no wired NIC on ${ROUTER_IP%.*}.0/24 — set BENCH_NIC explicitly (Wi-Fi cannot carry a macvlan)"
  fi
  [ -d "/sys/class/net/$BENCH_NIC" ] || die "BENCH_NIC=$BENCH_NIC does not exist"
  [ "$CLIENT_MAC" != "$(cat "/sys/class/net/$BENCH_NIC/address" 2>/dev/null || true)" ] \
    || die "CLIENT_MAC equals the host NIC's MAC — the run would authenticate the host"
fi

# ---------------------------------------------------------------- take the bench lock
#
# Before the lock is taken: LOCAL checks only (tools, token files, the wired NIC). The
# lock comes next so that "another window owns the bench" is refused before we probe or
# touch anything, and so the state we then read is a state we own.

if [ "${BENCH_LOCK_HELD:-0}" != "1" ]; then
  printf 'preflight: taking the single-owner bench lock (re-exec under bench-lock.sh exec)\n'
  # --client-iface is only passed when it is set: an empty value would flip the run into
  # "adopt an interface that does not exist" instead of the macvlan mode.
  IFACE_ARG=""
  [ -n "$CLIENT_IFACE" ] && IFACE_ARG="--client-iface $CLIENT_IFACE"
  # shellcheck disable=SC2086
  exec "$HERE/bench-lock.sh" exec --purpose "second-purchase e2e" -- \
    "$SELF" --purchase --lane "$LANE" --log-dir "$LOG_DIR" --burn-rounds "$BURN_ROUNDS" \
    $IFACE_ARG --client-mac "$CLIENT_MAC" --client-ip "$CLIENT_IP" --nic "$BENCH_NIC"
fi
"$HERE/bench-lock.sh" require || exit "$EX_LOCK"

# Router liveness is a TCP/HTTP read — this box DROPS ICMP, so never ping it.
STATUS_JSON="$(curl -s -m 6 "$API_BASE/" 2>/dev/null || true)"
case "$STATUS_JSON" in
  *'"kind":10021'*) printf 'preflight: backend reports kind:10021 (FULL mode)\n' ;;
  *kind*) die "backend is NOT in full mode (degraded/other): $(printf '%s' "$STATUS_JSON" | head -c 200)" ;;
  *) die "no answer from $API_BASE/ — is the bench up on $ROUTER_IP? (do not trust ping: it drops ICMP)" ;;
esac

# ---------------------------------------------------------------- transcript

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/e2e-$TS.log"
DL_LOG="$LOG_DIR/dl-$TS.log"
BUY_LOG="$LOG_DIR/buy-$TS.log"
if [ "$LOG_TO_FILE" = 1 ]; then
  printf 'second-purchase-e2e: transcript %s (tail -f to follow); download bytes %s\n' "$LOG" "$DL_LOG" >&2
  exec >>"$LOG" 2>&1
fi
if [ "$DETACHED" = 1 ]; then printf 'second-purchase-e2e: detached run, log %s\n' "$LOG" >&2; fi
: > "$DL_LOG"
: > "$BUY_LOG"

# ---------------------------------------------------------------- tokens (NUT-07, fail closed)

if [ "$LANE" = token ]; then
  say "TOKEN PREFLIGHT (NUT-07 checkstate: an already-spent token burns the whole run)"
  for t in "$TOKEN_1" "$TOKEN_2"; do
    out="$("$TOKENTOOL" verify --token-file "$t" 2>&1)"; rc=$?
    printf '%s\n' "$out"
    if [ "$rc" -ne 0 ]; then printf 'FATAL: %s is not spendable (NUT-07)\n' "$t"; exit "$EX_TOKEN"; fi
  done
else
  say "TOKEN PREFLIGHT: skipped (LANE=ln spends an invoice, not a token)"
fi

# ---------------------------------------------------------------- client plumbing

teardown_client() {
  sudo ip rule del from "$CLIENT_IP" table "$POLICY_TABLE" 2>/dev/null || true
  sudo ip rule del from "$CLIENT_IP" lookup "$POLICY_TABLE" 2>/dev/null || true
  sudo ip route flush table "$POLICY_TABLE" 2>/dev/null || true
  if [ "$EXISTING_IFACE" = 1 ]; then
    # The interface, its DHCP address and its link routes belong to the station (and to
    # NetworkManager). Removing them would leave the client off the air — only the policy
    # state we added is ours to take away.
    printf '  (existing interface %s left as it was: link and address untouched)\n' "$CLIENT_IFACE"
    return 0
  fi
  sudo ip addr del "$CLIENT_IP/32" dev "$CLIENT_VIF" 2>/dev/null || true
  sudo ip addr del "$CLIENT_IP/24" dev "$CLIENT_VIF" 2>/dev/null || true
  sudo ip link set "$CLIENT_VIF" down 2>/dev/null || true
  sudo ip link del "$CLIENT_VIF" 2>/dev/null || true
}

# A NetworkManager profile for the vif with autoconnect=yes re-creates the macvlan the
# instant we delete it — with a RANDOM cloned MAC, so the run would silently use the wrong
# identity ("ip link add: File exists"). Release it for the duration and say so loudly.
NM_HAD=0
NM_AC_ORIG=""
nm_release() {
  # plain `nmcli` is not allowed to deactivate a connection here ("Not authorized"); sudo is.
  if sudo nmcli -g connection.uuid con show "$CLIENT_VIF" >/dev/null 2>&1; then
    NM_HAD=1
    NM_AC_ORIG="$(sudo nmcli -g connection.autoconnect con show "$CLIENT_VIF" 2>/dev/null)"
    printf -- '--- NetworkManager owns %s (autoconnect=%s) -> disabling + deactivating\n' \
      "$CLIENT_VIF" "$NM_AC_ORIG"
    sudo nmcli con mod "$CLIENT_VIF" connection.autoconnect no || true
    sudo nmcli con down "$CLIENT_VIF" || true
    sleep 3
  else
    printf -- '--- no NetworkManager profile for %s\n' "$CLIENT_VIF"
  fi
}

cleanup() {
  local rc=$?
  teardown_client
  if [ "$NM_HAD" = 1 ]; then
    printf 'NOTE: NM profile %s was left DISABLED (autoconnect=no) so it cannot recreate\n' "$CLIENT_VIF"
    printf '      a stale wrong-MAC macvlan. Restore: nmcli con mod %s connection.autoconnect yes\n' "$CLIENT_VIF"
  fi
  say "torn down (rc=$rc, log ${LOG:-<none>})"
  exit "$rc"
}
trap cleanup EXIT

if [ "$EXISTING_IFACE" = 1 ]; then
  # ---- a real station (wireless client): adopt the interface as it is ------------------
  say "SETUP EXISTING client interface $CLIENT_IFACE (mac $CLIENT_MAC, ip $CLIENT_IP) — no macvlan: a station cannot present a second MAC"
  teardown_client
  # The ON-LINK route for our own subnet is NOT optional. Without it, every reply from this
  # address to another host on the link (including a WireGuard/NetBird peer that shares this
  # L2) is sent to the router by the policy default route and dies there, while probes
  # THROUGH the router keep working — the client looks half-deaf and the user loses the box.
  sudo ip route add "${ROUTER_IP%.*}.0/24" dev "$CLIENT_IFACE" src "$CLIENT_IP" scope link table "$POLICY_TABLE" 2>/dev/null \
    || printf '  (policy on-link route already present)\n'
  sudo ip route add "$ROUTER_IP/32" dev "$CLIENT_IFACE" src "$CLIENT_IP" scope link table "$POLICY_TABLE" 2>/dev/null \
    || printf '  (policy host route already present)\n'
  sudo ip route add default via "$ROUTER_IP" dev "$CLIENT_IFACE" src "$CLIENT_IP" table "$POLICY_TABLE" 2>/dev/null \
    || printf '  (policy default route already present)\n'
  sudo ip rule add from "$CLIENT_IP" table "$POLICY_TABLE" priority "$POLICY_PRIORITY" 2>/dev/null \
    || printf '  (policy rule already present)\n'
  ip -br addr show "$CLIENT_IFACE"
  ip rule show | grep "$CLIENT_IP" || true
  ip route show table "$POLICY_TABLE"
  assert_eq "client MAC is the adopted interface MAC" "$CLIENT_MAC" \
    "$(cat "/sys/class/net/$CLIENT_IFACE/address" 2>/dev/null || true)"
  assert_contains "the adopted address is configured on $CLIENT_IFACE" "$CLIENT_IP" \
    "$(ip -o -4 addr show dev "$CLIENT_IFACE" 2>/dev/null)"
  printf -- '--- client egress to the internet must leave by %s: %s\n' \
    "$CLIENT_IFACE" "$(ip route get 1.1.1.1 from "$CLIENT_IP" 2>/dev/null | head -1)"
  printf -- '--- management path (whole-host default) must be unchanged: %s\n' "$(ip route get 1.1.1.1 | head -1)"
else
  say "SETUP fresh-MAC client $CLIENT_MAC on $CLIENT_VIF (link $BENCH_NIC)"
  # Leftover macvlan / ip-rule / ip-route state from a killed run makes the next run die with
  # "RTNETLINK answers: File exists" — delete first, tolerate everything.
  nm_release
  teardown_client
  i=1
  while [ "$i" -le 5 ]; do
    ip -br link show "$CLIENT_VIF" >/dev/null 2>&1 || break
    printf '  waiting for stale %s to disappear (try %s)\n' "$CLIENT_VIF" "$i"
    sleep 2
    i=$((i + 1))
  done
  if ip -br link show "$CLIENT_VIF" >/dev/null 2>&1; then
    die "stale $CLIENT_VIF will not delete; a straggler or NetworkManager still owns it"
  fi

  sudo ip link add "$CLIENT_VIF" link "$BENCH_NIC" type macvlan mode bridge || die "ip link add failed"
  sudo ip link set "$CLIENT_VIF" address "$CLIENT_MAC" || die "ip link set address failed"
  sudo ip link set "$CLIENT_VIF" up || die "ip link set up failed"
  sudo ip addr add "$CLIENT_IP/32" dev "$CLIENT_VIF" || die "ip addr add $CLIENT_IP/32 failed"
  sudo ip route add "$ROUTER_IP/32" dev "$CLIENT_VIF" src "$CLIENT_IP" scope link table "$POLICY_TABLE" 2>/dev/null \
    || printf '  (policy host route already present)\n'
  sudo ip route add default via "$ROUTER_IP" dev "$CLIENT_VIF" src "$CLIENT_IP" table "$POLICY_TABLE" 2>/dev/null \
    || printf '  (policy default route already present)\n'
  sudo ip rule add from "$CLIENT_IP" table "$POLICY_TABLE" priority "$POLICY_PRIORITY" 2>/dev/null \
    || printf '  (policy rule already present)\n'
  ip -br addr show "$CLIENT_VIF"
  ip rule show | grep "$CLIENT_IP" || true
  ip route show table "$POLICY_TABLE"

  assert_eq "client MAC is the requested fresh MAC" "$CLIENT_MAC" \
    "$(cat "/sys/class/net/$CLIENT_VIF/address" 2>/dev/null || true)"
  printf -- '--- host management path must still leave by %s: %s\n' "$BENCH_NIC" "$(ip route get "$ROUTER_IP" | head -1)"
fi

# ---------------------------------------------------------------- probes / purchases

# --interface is not decoration: it is what makes the router resolve the client's MAC.
probe() { curl -s --interface "$CLIENT_IP" -m 8 -o /dev/null -w '%{http_code}' "$PROBE_URL"; }
probe_verbose() {
  curl -s --interface "$CLIENT_IP" -m 8 -o /dev/null \
    -w 'probe code=%{http_code} redirect=%{redirect_url}\n' "$PROBE_URL"
}
egress() {
  curl -s --interface "$CLIENT_IP" -m 20 -o /dev/null -w 'egress code=%{http_code} bytes=%{size_download}\n' "$EGRESS_URL"
}
balance() { curl -s --interface "$CLIENT_IP" -m 6 "$API_BASE/balance" || true; }

buy() {   # $1 token file (LANE=token)  $2 label
  local body http
  if [ "$LANE" = ln ]; then ln_buy "$2"; return $?; fi
  say "PURCHASE $2 ($1)"
  body="$(curl -s --interface "$CLIENT_IP" -m 30 -X POST --data-binary "@$1" \
    -H 'Content-Type: text/plain' -w '\nHTTP=%{http_code}' "$API_BASE/")"
  http="$(printf '%s' "$body" | sed -n 's/^HTTP=//p' | tail -1)"
  printf '%s\n' "$body" | head -c 500
  printf '\n'
  { printf -- '--- buy %s %s\n' "$2" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"; printf '%s\n' "$body"; } >> "$BUY_LOG"
  LAST_HTTP="$http"
  LAST_BODY="$body"
}

# The portal's Lightning-invoice lane — the lane the operator reported. Faithful to the
# shipped SPA (index-YkGiQMp2.js): GET /whoami -> POST /ln-invoice?mac=<mac>
# {"amount":N,"mint_url":"..."} -> poll GET /ln-invoice?quote=<q>&mac=<mac> until
# access_granted. One step per purchase by default; testnut.cashu.exchange runs a
# FakeWallet, so the invoice settles itself in a few seconds at zero cost.
ln_buy() {   # $1 label
  local label="$1" w wmac body q j st ag al i t0
  say "PURCHASE $label — LANE=ln (portal Lightning invoice, $LN_AMOUNT sats = $LN_AMOUNT step(s))"
  printf -- '--- GET /whoami through %s (the purchase is issued FROM this socket)\n' "$CLIENT_IP"
  w="$(curl -s --interface "$CLIENT_IP" -m 10 "$API_BASE/whoami" || true)"
  printf '  /whoami -> %s\n' "$w"
  case "$w" in
    mac=*) ;;
    *) printf '!!! /whoami did not answer mac=<mac> — cannot buy\n'; LAST_HTTP=000; LAST_BODY="$w"; return 1 ;;
  esac
  wmac="${w#mac=}"
  t0="$(date +%s.%N)"
  printf -- '--- POST /ln-invoice?mac=%s  {"amount":%s,"mint_url":"%s"}\n' "$wmac" "$LN_AMOUNT" "$MINT_URL"
  body="$(curl -s --interface "$CLIENT_IP" -m 30 -X POST -H 'Content-Type: application/json' \
    --data "{\"amount\":$LN_AMOUNT,\"mint_url\":\"$MINT_URL\"}" \
    -w '\nHTTP=%{http_code}' "$API_BASE/ln-invoice?mac=$wmac")"
  LAST_HTTP="$(printf '%s' "$body" | sed -n 's/^HTTP=//p' | tail -1)"
  q="$(json_val quote "$body")"
  printf '  invoice POST: HTTP=%s quote=%s\n' "${LAST_HTTP:-?}" "${q:-<none>}"
  printf '%s\n' "$(printf '%s' "$body" | head -c 400)"
  { printf -- '--- buy %s (lane=ln) %s\n' "$label" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"; printf '%s\n' "$body"; } >> "$BUY_LOG"
  [ -n "$q" ] || { printf '!!! no quote in the invoice response — cannot buy\n'; LAST_BODY="$body"; return 1; }
  printf -- '--- poll GET /ln-invoice?quote=%s (settlement -> access_granted)\n' "$q"
  ag=""; al="none"; j=""
  i=1
  while [ "$i" -le "$LN_POLLS" ]; do
    j="$(curl -s --interface "$CLIENT_IP" -m 10 "$API_BASE/ln-invoice?quote=$q&mac=$wmac" || true)"
    st="$(json_val state "$j")"; ag="$(json_val access_granted "$j")"; al="$(json_val allotment "$j")"
    printf '    t=%3ss state=%-9s access_granted=%-5s allotment=%s\n' \
      "$((i * 2))" "${st:-?}" "${ag:-?}" "${al:-none}"
    [ "$ag" = "true" ] && break
    sleep 2
    i=$((i + 1))
  done
  printf '  final status json: %s\n' "$j"
  printf '  settle+grant wall time: %ss\n' \
    "$(awk -v a="$t0" -v b="$(date +%s.%N)" 'BEGIN { printf "%.1f", b - a }')"
  { printf '%s\n' "$j"; } >> "$BUY_LOG"
  LN_GRANT="$ag"
  LN_ALLOT="$al"
  LAST_BODY="$j"
  [ "$ag" = "true" ] || return 1
  return 0
}

snap() { "$SNAP" snapshot --label "$1" --out "$LOG_DIR/snapshot-$TS.log"; }
run_on_router() { "$SNAP" run "$1" --out "$LOG_DIR/onrouter-$TS.log"; }

# The decisive module-log greps, read through the transport (one place, both phases).
#
# THE ROUTER'S LOG IS COLOURED, AND THAT BROKE THE COUNTER ASSERTION SILENTLY. tollgate-wrt's
# logrus lines reach the ring buffer with ANSI escapes between a field NAME and its `=`, so on
# the real bench the line is
#   ERRO[..] Gate close NOT confirmed ... \x1b[31munconfirmed_closes\x1b[0m=2134
# and a pattern like `unconfirmed_closes=` matches NOTHING. Measured on the 2026-09-26 pre17
# forced-drift capture: 43 of its lines carry an escape, and the capture's own
# `grep -oE "unconfirmed_closes=[0-9]+"` returned EMPTY while the value 2134+ was right there —
# a counter read that returns 0 for ever is a false-PASS generator, which is the one thing a
# convergence assertion must not be. So the windows are normalised ONCE, locally (GNU sed; the
# router's busybox sed is never asked to interpret \x1b). Because the ROUTER-side grep runs
# before this, a caller's pattern must not depend on the `=`: use `unconfirmed_closes`, not
# `unconfirmed_closes=`.
strip_ansi() { sed -e 's/\x1b\[[0-9;]*[A-Za-z]//g' -e 's/\r$//'; }

router_log_grep() {   # $1 = extra grep -E pattern for the payload
  local sh out
  sh="$(mktemp "${TMPDIR:-/tmp}/loggrep.XXXXXX")"
  cat > "$sh" <<EOF
logread 2>/dev/null | grep -iE "$1" | tail -40
EOF
  out="$(run_on_router "$sh" 2>&1)"
  rm -f "$sh"
  printf '%s\n' "$out" | strip_ansi
}

# ---------------------------------------------------------------- zombie-session convergence helpers
#
# Used by PHASE 5b (see the convergence-contract comment below for the measured failure these
# assertions exist to catch). They live here, before their first use, because the run's shell reads
# this file top-down: a function defined next to PHASE 5b would not exist when buy#1 is asserted.

# The module lines that mean "this address has been settled".
settled_pattern() {
  printf '%s' "already gone|Reconciled the stale binding of $CLIENT_MAC|Removed unmeterable session for $CLIENT_MAC|Removed expired session for $CLIENT_MAC"
}

# PHASE 5b's evidence window starts at a MARKER written into the router's own log when the phase
# begins. Everything logged BEFORE it is not this phase's evidence: the exhaustion of buy#1 already
# logs "Removed expired session for $CLIENT_MAC", an earlier phase may have escalated the client,
# and a wedge line can survive from a PREVIOUS run. Reading the whole buffer instead is a false
# PASS generator — the settle search matches the exhaustion and the phase "passes" without the
# module having done anything. logread here is a ~500-line ring buffer with no epoch and no stable
# line numbering, so a marker line is the only reliable "since" anchor.
router_log_mark() {   # $1 = marker token
  local sh
  sh="$(mktemp "${TMPDIR:-/tmp}/logmark.XXXXXX")"
  cat > "$sh" <<EOF
logger -t tollgate-bench "LOG-ANCHOR $1"
EOF
  run_on_router "$sh" >/dev/null 2>&1
  rm -f "$sh"
}

# The lines the router logged AFTER the last "LOG-ANCHOR $1" marker, matching grep -E "$2".
# Identical to router_log_grep otherwise (same transport, same tail), so a caller that needs the
# whole buffer keeps using router_log_grep.
router_log_since() {   # $1 = marker token  $2 = grep -E pattern for the payload
  local sh out
  sh="$(mktemp "${TMPDIR:-/tmp}/logsince.XXXXXX")"
  cat > "$sh" <<EOF
logread 2>/dev/null | awk -v m="LOG-ANCHOR $1" 'index(\$0,m){n=NR} {a[NR]=\$0} END{for(i=n+1;i<=NR;i++) print a[i]}' | grep -iE "$2" | tail -40
EOF
  out="$(run_on_router "$sh" 2>&1)"
  rm -f "$sh"
  printf '%s\n' "$out" | strip_ansi
}

# The module's running total of unconfirmed closes, read from either log surface: the valve's
# `unconfirmed_closes=` (logrus, the close machinery) or the merchant's `unconfirmed gate
# closes=` (the sweep that drives it). 0 when the module reported none, which is the honest
# reading of "nothing was escalated in this window".
#
# The value is parsed AFTER strip_ansi (see router_log_grep): on the bench the logrus field is
# `unconfirmed_closes\x1b[0m=2134`, so a `unconfirmed_closes=` sed would have read 0 for ever.
unconfirmed_total() {
  local n
  n="$(printf '%s\n' "$1" | sed -n 's/.*unconfirmed_closes=\([0-9][0-9]*\).*/\1/p' | tail -1)"
  [ -n "$n" ] || n="$(printf '%s\n' "$1" | sed -n 's/.*unconfirmed gate closes=\([0-9][0-9]*\).*/\1/p' | tail -1)"
  printf '%s' "${n:-0}"
}

# How many of those lines name THIS client: a growth caused by another client's session is not
# this assertion's business, a growth caused by ours is.
unconfirmed_for_client() {
  printf '%s\n' "$1" | grep -c "$CLIENT_MAC" 2>/dev/null || true
}

# The module's error lines, WITHOUT the client MAC in the pattern — so "this window names the
# client" is a real assertion rather than a tautology.
grant_error_log() { router_log_grep 'ERROR|error:|error=|failed|not found|UNRESOLVED'; }

# A purchase that was NOT granted must at least say so, specifically, in the module log. The
# measured failure this guards against is the silent one: `state=PAID`, the merchant wallet +1
# sat, `access_granted` never true, and the customer staring at a portal with no explanation.
# Only the invoice lane reports `access_granted`; the token lane's grant evidence is kind:1022.
assert_grant_not_silent() {   # $1 = label, $2 = the module's error window
  [ "$LANE" = ln ] || return 0
  [ "${LN_GRANT:-}" = "true" ] && return 0

  printf -- '--- a purchase was NOT granted (access_granted=%s); the module said:\n%s\n' \
    "${LN_GRANT:-<none>}" "$2"
  assert_contains "$1 was a LOUD, specific error naming the client (never a silent no-op)" \
    "$CLIENT_MAC" "$2"
}

# ---------------------------------------------------------------- the convergence contract
#
# The measured defect (bench MT3000, 2026-09-26, module pin 2796d96c): a client LEAVES nodogsplash
# while the module still holds its PAID session. `ndsctl deauth` then answers `Client <mac> not
# found.` and exits 1 — the enforcement layer holds nothing for the MAC, so there is nothing left
# to close — and the module read that exit status as an UNCONFIRMED close: it kept the session
# tracked, retried the close at the sweep cadence for ever (`unconfirmed_closes` 113 -> 193 -> 195,
# monotonic), never retired the session, and logged the false warning "this client may still hold
# open, unmetered access" for a MAC nodogsplash did not hold. The storm drove ndsctl until its
# socket died ("Socket is not ready for communication : Bad file descriptor" every ~5 s) and a PAID
# purchase could then not be authorised at all (state=PAID, merchant wallet +1 sat,
# access_granted never true) — the operator's "the second purchase showed a new allotment, but no
# internet". The remedy that restored the box was an operator restart of nodogsplash.
#
# THE CLIENT LEAVING IS NOT ENOUGH — THE CLOSE HAS TO BE FORCED (measured on this bench the same
# day). With the client merely deauthed, a worker measured N_DEAUTH=0 N_UNCONF=0 in the window: the
# module holds the session and does nothing, because nothing has asked it to close yet. The
# deterministic reproducer is PHASE 5b's forcing step — a DELIBERATE nodogsplash restart — after
# which the module's sweeps find the session's counters unreadable and must close the gate, and
# `ndsctl deauth <mac>` answers `Client <mac> not found.` rc=1: the failing deauth the defect needs.
# On pre17 the close loop started within 30 s (the buffer showed 72 loop lines and
# `unconfirmed_closes` had reached 2141); with the fix binary the same scenario produced no new loop
# lines in 240 s, and the module logged its own positive statement (see converge_assert_state_change).
#
# So after the client is gone, the module must CONVERGE on that address:
#   * it RETIRES the binding — or re-establishes the gate deliberately — never the drift state, in
#     which /balance and the portal keep reporting a session nobody can use;
#   * it SAYS SO positively: for an address nodogsplash does not know, the module states that the
#     client is gone and that there is nothing left to deauthorize (the fix's own INFO line). A
#     "settled" state whose reason is only the ABSENCE of errors cannot be told apart from a ring
#     buffer that rotated the errors out — which is exactly why the 2026-09-26 AFTER capture could
#     not prove the fix in one run;
#   * it stops driving ndsctl about the address: no unconfirmed-close escalation names the client
#     inside the window, and the running total does not move across a settle window;
#   * it never claims the client holds unmetered access, because nodogsplash does not know it;
#   * it leaves the ndsctl socket ANSWERABLE, which is what the NEXT purchase depends on.
#
# The window is anchored with a marker in the router's own log (router_log_mark), written BEFORE
# the forcing step: the exhaustion of buy#1 ALSO logs "Removed expired session for <mac>", so an
# unanchored read reports the address as settled without the module having done anything — a false
# PASS this contract must not have.

# Write the anchor for one convergence window. $1 = token
converge_window_open() {
  router_log_mark "$1"
}

# The module must settle the address within the budget. $1 = label, $2 = token, $3 = budget seconds
converge_assert_settled() {
  local label="$1" token="$2" budget="$3" log="" settled=0 waited=0

  while [ "$waited" -le "$budget" ]; do
    log="$(router_log_since "$token" "$(settled_pattern)")"
    # A literal substring test in the shell, NOT `printf '%s\n' "$log" | grep -q`: `grep -q` exits
    # on the first match, the still-writing printf then dies of SIGPIPE (141) and this script's
    # `set -o pipefail` turns that into a non-zero pipeline — i.e. a window that DOES name the
    # client would be read as "not settled" and the phase would wait out its whole budget. The
    # `case` form has no second process to lose a race. (Measured 2026-09-26 in the kit's own
    # test harness: 166/300 false FAILs on a 51 KiB haystack with the needle at the top.)
    case "$log" in *"$CLIENT_MAC"*) settled=1; break ;; esac
    sleep 10
    waited=$((waited + 10))
  done
  printf -- '--- %s: module lines that settle %s (waited %ss of %ss; window starts at %s):\n%s\n' \
    "$label" "$CLIENT_MAC" "$waited" "$budget" "$token" "$log"
  if [ "$settled" = 1 ]; then
    printf 'ASSERT PASS  %s: the module settled the address nodogsplash no longer knows\n' "$label"
  else
    printf 'ASSERT FAIL  %s: the module never settled the address nodogsplash no longer knows (%ss budget)\n' "$label" "$budget"
    printf '             a session whose client left can be neither metered nor closed, so it stays\n'
    printf '             tracked for ever and /balance keeps reporting a session nobody can use\n'
    printf '             (measured on this bench: 170+ sweeps and unconfirmed_closes 113 -> 193)\n'
    FAILED=$((FAILED + 1))
  fi
}

# The running total is a cumulative gauge, so it is read from the WHOLE buffer (its last line is the
# current total); how many escalations NAME this client is read from the anchored window, so the
# assertion covers this window and not the phases that preceded it. $1 = label, $2 = token
#
# The ROUTER-side pattern is `unconfirmed_closes`, WITHOUT the `=`: on this bench the logrus field
# is `unconfirmed_closes\x1b[0m=2134`, so the `=` never matches there (strip_ansi fixes the local
# read, but the router's grep has already filtered the lines by then). A pattern with the `=` in it
# is a counter assertion that reads 0 for ever — a false PASS generator.
converge_assert_counters() {
  local label="$1" token="$2" total_a total_b client_a client_b

  total_a="$(unconfirmed_total "$(router_log_grep 'unconfirmed_closes|unconfirmed gate closes')")"
  client_a="$(unconfirmed_for_client "$(router_log_since "$token" 'unconfirmed_closes|unconfirmed gate closes')")"
  sleep "$SETTLE_WINDOW"
  total_b="$(unconfirmed_total "$(router_log_grep 'unconfirmed_closes|unconfirmed gate closes')")"
  client_b="$(unconfirmed_for_client "$(router_log_since "$token" 'unconfirmed_closes|unconfirmed gate closes')")"
  printf -- '--- %s: unconfirmed closes sample A=%s (naming %s in this window: %s) -> sample B=%s (naming %s in this window: %s)\n' \
    "$label" "$total_a" "$CLIENT_MAC" "$client_a" "$total_b" "$CLIENT_MAC" "$client_b"
  assert_eq "$label: unconfirmed_closes did not grow while the address was gone" "$total_a" "$total_b"
  assert_eq "$label: no unconfirmed-close escalation named the client after it left nodogsplash" "$client_a" "$client_b"
}

# The wording contract and the socket the gate depends on. For an address nodogsplash does not know,
# no line may claim the client still holds open, unmetered access: that claim sends an operator (or
# a reviewer) after free internet this state cannot have. And the measured failure needed an
# operator restart to clear, so a wedged socket — or a silent one — is a failure of its own.
# $1 = label, $2 = token
converge_assert_wording_and_socket() {
  local label="$1" token="$2" box_id nds_raw

  assert_not_contains "$label: the module does not claim unmetered access for an address nodogsplash does not know" \
    "may still hold open, unmetered access" "$(router_log_since "$token" 'unmetered access')"
  assert_not_contains "$label: no wedged ndsctl socket in the settle window" "Socket is not ready for communication" \
    "$(router_log_since "$token" 'Socket is not ready for communication|Bad file descriptor')"

  box_id="$(box_identity)"
  nds_raw="$(box_field "$box_id" nds_uptime_raw)"
  if [ -n "$nds_raw" ]; then
    printf 'ASSERT PASS  %s: ndsctl still answers after the settle window (nodogsplash uptime %s)\n' "$label" "$nds_raw"
  else
    printf 'ASSERT FAIL  %s: ndsctl stopped answering during the settle window: the socket is WEDGED, and a\n' "$label"
    printf '             wedged socket is the state in which a PAID purchase cannot be authorised at all\n'
    FAILED=$((FAILED + 1))
  fi
}

# The client leaves nodogsplash with a PAID session still open: nodogsplash drops its record for the
# MAC, the module keeps the session (that IS the drift), and the deauthorization the module will
# attempt cannot be confirmed, because nodogsplash does not know the address at all.
#
# ONE recorded ndsctl step. No service is bounced, so the run's restart guard is untouched, and the
# box identity is re-checked by the caller at the phase boundary.
#
# KEPT FOR THE ABLATION ONLY (FORCE_DRIFT=deauth). This step is what the lane used to call "the
# drift", and it is NOT sufficient: measured on this bench on 2026-09-26, a client that deauths
# itself while its paid allotment is open produces NO close attempt at all in the window
# (N_DEAUTH=0 N_UNCONF=0) — the module holds the session and does nothing, because a close has to
# be TRIGGERED before the failing deauth can happen. The deterministic trigger is the deliberate
# nodogsplash restart below.
client_leaves_nodsplash() {
  local sh out
  sh="$(mktemp "${TMPDIR:-/tmp}/deauth.XXXXXX")"
  cat > "$sh" <<EOF
echo "-- ndsctl deauth $CLIENT_MAC (the client leaves; the module's session is NOT told)"
ndsctl deauth $CLIENT_MAC; echo "rc=\$?"
sleep 3
ndsctl json 2>/dev/null | head -c 300; echo
EOF
  out="$(run_on_router "$sh" 2>&1)"
  rm -f "$sh"
  printf '%s\n' "$out"
}

# ---------------------------------------------------------------- PHASE 5b's FORCING STEP
#
# WHY A FORCING STEP EXISTS AT ALL — measured on this bench on 2026-09-26.
#   Having the client LEAVE nodogsplash (`ndsctl deauth`) while its PAID allotment stays open is
#   necessary but NOT sufficient: the module simply keeps holding the session and does nothing
#   (N_DEAUTH=0 N_UNCONF=0 in the window), because a close has to be TRIGGERED before the failing
#   deauth can happen. What reproduces the defect, in seconds, is a DELIBERATE restart of
#   nodogsplash after the purchase: NDS comes back not knowing the client, the module's sweeps then
#   find the session's counters unreadable and must close the gate, and `ndsctl deauth <mac>`
#   answers `Client <mac> not found.` rc=1 — the failing deauth the defect needs. On the pre17
#   build the close loop started within 30 s (its `unconfirmed_closes` had reached 2141); with the
#   fix binary the same scenario produced no new loop lines in 240 s.
#
# THREE THINGS THE FORCING STEP MUST NOT GET WRONG
#   1. It must be ATTRIBUTABLE. An unattributed restart once invalidated a sibling's run and was
#      mistaken for a product self-restart, so the step announces itself in the ROUTER'S OWN log
#      (logger -t tollgate-bench "BENCH ACTION ...") BEFORE it restarts and prints the pid change
#      in the transcript. The box guard then fires if the MODULE moved with it — that would be a
#      product self-restart, not our step — and the baseline is re-pinned afterwards.
#   2. The client must stay SILENT after it. Any packet FROM the client re-creates its nodogsplash
#      record within a sweep or two (`Adding <ip> <mac> ... to client list`) and the drift
#      dissolves, so nothing in PHASE 5b may probe THROUGH the client after the restart. That is
#      why the phase reads its evidence from the box and from the module's log, never with
#      probe()/balance()/egress().
#   3. It must PROVE it forced something. A restart that leaves nodogsplash still knowing the
#      client forces nothing, so the precondition (NDS does not list the MAC) is itself asserted:
#      a phase that "passes" without it would be evidence of nothing.

# The fix's own positive INFO line (module PR #595, src/valve/valve.go): for a MAC nodogsplash does
# not know, the gate is closed BY DEFINITION, there is nothing left to deauthorize, and no retry is
# armed. The two phrases below are the two halves of that ONE line.
state_change_pattern() {
  printf '%s' 'Client already gone|nothing left to deauthorize'
}

# What nodogsplash itself says about the client RIGHT NOW: 0 when it holds no record for the MAC,
# a count when it does, `?` when the question could not be asked (a wedged ndsctl is not a pass).
# The state the whole phase depends on, read from the box — this is the "never on the log alone"
# half of the assertion.
nds_knows_client() {
  local sh out
  sh="$(mktemp "${TMPDIR:-/tmp}/ndsknows.XXXXXX")"
  {
    printf 'mac=%s\n' "$CLIENT_MAC"
    cat <<'EOF'
ndsctl json 2>/dev/null | grep -c "$mac"
EOF
  } > "$sh"
  out="$(run_on_router "$sh" 2>&1)"
  rm -f "$sh"
  out="$(printf '%s\n' "$out" | sed -n 's/^\([0-9][0-9]*\)$/\1/p' | tail -1)"
  printf '%s' "${out:-?}"
}

# The forcing step, as PHASE 5b runs it. $FORCE_DRIFT selects it; `restart` is the default and the
# only one that reproduces the defect.
force_client_drift() {
  case "$FORCE_DRIFT" in
    restart) force_client_drift_by_nds_restart ;;
    deauth)
      printf -- '-- FORCING STEP = deauth (ABLATION ONLY): the client leaves nodogsplash with its PAID\n'
      printf -- '   allotment still open. Measured 2026-09-26: this state ALONE produces no close attempt\n'
      printf -- '   at all (N_DEAUTH=0 N_UNCONF=0), so the settle assertions below are EXPECTED to fail:\n'
      printf -- '   nothing settled because nothing TRIED. That is the ablation'"'"'s result, not a\n'
      printf -- '   regression — re-run with FORCE_DRIFT=restart for the real reproduction.\n'
      printf '%s\n' "$(client_leaves_nodsplash)"
      ;;
  esac
}

# The deliberate restart. One recorded, attributable bounce; the module must NOT move with it.
force_client_drift_by_nds_restart() {
  local sh out knows
  printf -- '-- THE FORCING STEP (deliberate, attributable): restart nodogsplash on the bench, so the\n'
  printf -- '   module finds the session of %s unreadable and has to close its gate.\n' "$CLIENT_MAC"

  sh="$(mktemp "${TMPDIR:-/tmp}/forcedrift.XXXXXX")"
  {
    printf 'mac=%s\n' "$CLIENT_MAC"
    cat <<'EOF'
pid_before="$(pgrep -f '[n]odogsplash' | head -1)"
logger -t tollgate-bench "BENCH ACTION: deliberate nodogsplash restart by the bench lane (second-purchase-e2e PHASE 5b, client $mac) — attributable to the test rig, NOT a product self-restart"
echo "-- before: nds_pid=${pid_before:-?} nds_uptime=$(ndsctl status 2>/dev/null | sed -n 's/^Uptime: //p' | head -1) clients=$(ndsctl json 2>/dev/null | grep -c '"mac"')"
/etc/init.d/nodogsplash restart
sleep 4
echo "-- restart issued. after: nds_pid=$(pgrep -f '[n]odogsplash' | head -1) nds_uptime=$(ndsctl status 2>/dev/null | sed -n 's/^Uptime: //p' | head -1) clients=$(ndsctl json 2>/dev/null | grep -c '"mac"')"
echo "-- nodogsplash records for $mac after the restart: $(ndsctl json 2>/dev/null | grep -c "$mac")"
logger -t tollgate-bench "BENCH ACTION: deliberate nodogsplash restart complete — the rig restarted NDS; NDS no longer knows client $mac"
EOF
  } > "$sh"
  out="$(run_on_router "$sh" 2>&1)"
  rm -f "$sh"
  printf '%s\n' "$out"

  # The module must NOT have moved with our bounce (a new tollgate-wrt pid would be a product
  # self-restart, and everything after it uninterpretable => exit 15), and the baseline is
  # re-pinned so the phase's own end check compares against the post-restart box.
  box_assert_module_stable "after-the-forced-nds-restart"
  box_record "phase5b-forced-nds-restart"

  # ANTI-VACUITY: the drift exists only if nodogsplash really forgot the client.
  knows="$(nds_knows_client)"
  if [ "$knows" = "0" ]; then
    printf 'ASSERT PASS  PHASE 5b precondition: nodogsplash no longer knows %s — the drift exists\n' "$CLIENT_MAC"
  else
    printf 'ASSERT FAIL  PHASE 5b precondition: nodogsplash still lists %s after the restart (records=%s)\n' "$CLIENT_MAC" "$knows"
    printf '             the forcing step forced NOTHING, so whatever this phase measures is not the\n'
    printf '             zombie-session path. Usual cause: something probed THROUGH the client after the\n'
    printf '             restart — a single packet re-creates its NDS record within a sweep or two.\n'
    FAILED=$((FAILED + 1))
  fi
}

# The state change, asserted on evidence that cannot be produced by a rotating ring buffer:
#   * the module's OWN statement that the client is gone and nothing is left to deauthorize,
#     NAMING the MAC, inside the window that starts at the forcing step's marker;
#   * nodogsplash's own table still not knowing the MAC at the end of the window (the drift held);
#   * and, in converge_assert_counters/wording_and_socket, the running total not moving and the
#     ndsctl socket still answering.
# $1 = label, $2 = token
converge_assert_state_change() {
  local label="$1" token="$2" log="" hits=0 named=0 waited=0 knows=""

  while [ "$waited" -le "$SETTLE_BUDGET" ]; do
    log="$(router_log_since "$token" "$(state_change_pattern)")"
    hits="$(printf '%s\n' "$log" | grep 'Client already gone' 2>/dev/null | grep -c 'nothing left to deauthorize' || true)"
    named="$(printf '%s\n' "$log" | grep 'Client already gone' 2>/dev/null | grep 'nothing left to deauthorize' | grep -c "$CLIENT_MAC" || true)"
    if [ "${hits:-0}" -gt 0 ] && [ "${named:-0}" -gt 0 ]; then break; fi
    sleep 10
    waited=$((waited + 10))
  done

  printf -- '--- %s: what the module said about the address nodogsplash does not know (waited %ss of %ss; window starts at %s):\n%s\n' \
    "$label" "$waited" "$SETTLE_BUDGET" "$token" "$log"
  if [ "${hits:-0}" -gt 0 ] && [ "${named:-0}" -gt 0 ]; then
    printf 'ASSERT PASS  %s: the module stated that the client is GONE and nothing is left to deauthorize (%s)\n' \
      "$label" "$CLIENT_MAC"
  else
    printf 'ASSERT FAIL  %s: the module never stated that nodogsplash does not know %s\n' "$label" "$CLIENT_MAC"
    printf '             wanted ONE line carrying both "Client already gone" and "nothing left to\n'
    printf '             deauthorize" and naming the MAC; saw %s matching line(s), %s of them naming it.\n' \
      "${hits:-0}" "${named:-0}"
    printf '             Without that line a "settled" state rests on the ABSENCE of error lines, and an\n'
    printf '             absence in a rotating ring buffer proves nothing: it is exactly why the 2026-09-26\n'
    printf '             AFTER measurement could not prove the fix in one run (the INFO had rotated out).\n'
    FAILED=$((FAILED + 1))
  fi

  knows="$(nds_knows_client)"
  assert_eq "$label: nodogsplash still does not know the client at the end of the window (state, not log)" "0" "$knows"
}

# ---------------------------------------------------------------- box identity (the restart guard)
#
# The failure this guards against, measured on the bench MT3000 on 2026-09-26: nodogsplash AND
# tollgate-wrt were restarted MID-RUN by an out-of-band remediation (`router-remediate.sh`,
# clearing a wedged ndsctl socket). Both daemons came back on new pids, the module's tracked
# sessions were orphaned (NDS client table empty while the module still tracked five MACs), the
# close path accumulated `unconfirmed_closes`, and the transcript went on looking exactly like a
# product result. The run had no way to notice, so it published a meaningless verdict.
#
# So the box that produced the result must be the SAME box at every phase boundary:
#   * router uptime must not go BACKWARDS          (a decrease = the router rebooted),
#   * nodogsplash uptime must not go BACKWARDS     (a decrease = nodogsplash restarted),
#   * the nodogsplash pid must be UNCHANGED        (a restart = a new pid),
#   * the tollgate-wrt pid must be UNCHANGED       (a restart = a new pid),
#   * both states must be READABLE                 (an unreadable ndsctl is not "stable").
# PHASE 0 records the baseline; every later boundary re-reads and compares. Any change is FATAL
# (exit 15) — everything measured after it is uninterpretable.
#
# THE ONE DELIBERATE BOUNCE IS PHASE 5b'S FORCING STEP, and it is handled by RE-PINNING: the step
# announces itself in the router's own log, box_assert_module_stable() proves the MODULE did not
# move with nodogsplash (a module restart there would be a product self-restart, not our step), and
# box_record() re-records the baseline so the end-of-phase check judges the post-restart box. Any
# OTHER service bounce during the run belongs in PHASE 0, BEFORE the baseline is recorded.

box_identity() {   # one ssh round-trip; the marker lines are the whole payload
  local sh out
  sh="$(mktemp "${TMPDIR:-/tmp}/boxid.XXXXXX")"
  cat > "$sh" <<'EOF'
echo "uptime_s=$(cut -d' ' -f1 /proc/uptime)"
echo "nds_uptime_raw=$(ndsctl status 2>/dev/null | sed -n 's/^Uptime: //p' | head -1)"
echo "nds_pid=$(pgrep -f '[n]odogsplash' 2>/dev/null | head -1)"
echo "wrt_pid=$(pgrep -f '[t]ollgate-wrt' 2>/dev/null | head -1)"
EOF
  out="$(run_on_router "$sh" 2>&1)"
  rm -f "$sh"
  # the transport prints its own bookkeeping line; keep only our markers
  printf '%s\n' "$out" | grep -E '^(uptime_s|nds_uptime_raw|nds_pid|wrt_pid)='
}

box_field() { printf '%s\n' "$1" | sed -n "s/^$2=//p" | head -1; }

# "7m 33s" / "45s" / "1h 2m 3s" -> seconds (0 when unreadable)
box_secs() {
  printf '%s' "$1" | awk '{ s=0
    for (i=1;i<=NF;i++) { v=$i; u=substr(v,length(v),1); num=substr(v,1,length(v)-1)+0
      if (u=="d") s+=num*86400; else if (u=="h") s+=num*3600; else if (u=="m") s+=num*60
      else if (u=="s") s+=num }
    printf "%d", s }'
}

BOX_UP_S=""; BOX_NDS_S=""; BOX_NDS_RAW=""; BOX_NDS_PID=""; BOX_WRT_PID=""

box_record() {   # $1 = label of the boundary that is being pinned down
  local id up nds_raw nds_s nds_pid wrt_pid
  id="$(box_identity)"
  up="$(box_field "$id" uptime_s)"
  nds_raw="$(box_field "$id" nds_uptime_raw)"
  nds_pid="$(box_field "$id" nds_pid)"
  wrt_pid="$(box_field "$id" wrt_pid)"
  nds_s="$(box_secs "$nds_raw")"
  printf 'BOX IDENTITY  %-26s router_uptime=%ss nds_uptime=%s (%ss) nds_pid=%s wrt_pid=%s\n' \
    "$1" "${up:-?}" "${nds_raw:-<unreadable>}" "$nds_s" "${nds_pid:-?}" "${wrt_pid:-?}"
  BOX_UP_S="$up"; BOX_NDS_S="$nds_s"; BOX_NDS_RAW="$nds_raw"
  BOX_NDS_PID="$nds_pid"; BOX_WRT_PID="$wrt_pid"
}

box_assert_stable() {   # $1 = label of the boundary being checked
  local id up nds_raw nds_s nds_pid wrt_pid why=""
  id="$(box_identity)"
  up="$(box_field "$id" uptime_s)"
  nds_raw="$(box_field "$id" nds_uptime_raw)"
  nds_pid="$(box_field "$id" nds_pid)"
  wrt_pid="$(box_field "$id" wrt_pid)"
  nds_s="$(box_secs "$nds_raw")"
  printf 'BOX CHECK     %-26s router_uptime=%ss nds_uptime=%s (%ss) nds_pid=%s wrt_pid=%s\n' \
    "$1" "${up:-?}" "${nds_raw:-<unreadable>}" "$nds_s" "${nds_pid:-?}" "${wrt_pid:-?}"
  [ -n "$up" ] || why="$why router-uptime-unreadable;"
  [ -n "$nds_raw" ] || why="$why ndsctl-status-unreadable(wedged?);"
  [ "$nds_pid" = "$BOX_NDS_PID" ] || why="$why nodogsplash-pid $BOX_NDS_PID->$nds_pid;"
  [ "$wrt_pid" = "$BOX_WRT_PID" ] || why="$why tollgate-wrt-pid $BOX_WRT_PID->$wrt_pid;"
  if [ -n "$up" ] && [ -n "$BOX_UP_S" ]; then
    awk -v a="$BOX_UP_S" -v b="$up" 'BEGIN{ exit !(b >= a) }' \
      || why="$why router-uptime-went-backwards $BOX_UP_S->$up(reboot);"
  fi
  if [ -n "$nds_s" ] && [ -n "$BOX_NDS_S" ]; then
    awk -v a="$BOX_NDS_S" -v b="$nds_s" 'BEGIN{ exit !(b >= a) }' \
      || why="$why nodogsplash-uptime-went-backwards $BOX_NDS_RAW->$nds_raw(restart);"
  fi
  [ -z "$why" ] && printf 'ASSERT PASS  the box stayed up for the whole run (through %s)\n' "$1"
  [ -z "$why" ] || box_broken "$1" "$why"
}

# The module-only variant of the check above, for the boundary where NODOGSPLASH WAS DELIBERATELY
# RESTARTED by PHASE 5b's forcing step. The router must not have rebooted and the MODULE must not
# have moved — if tollgate-wrt came back on a new pid, the box restarted itself, our step is not
# the only thing that changed and the run is invalid (exit 15), exactly as above. The nodogsplash
# pid and uptime are EXPECTED to have moved and are printed, not judged.
box_assert_module_stable() {   # $1 = label of the boundary being checked
  local id up nds_raw nds_s nds_pid wrt_pid why=""
  id="$(box_identity)"
  up="$(box_field "$id" uptime_s)"
  nds_raw="$(box_field "$id" nds_uptime_raw)"
  nds_pid="$(box_field "$id" nds_pid)"
  wrt_pid="$(box_field "$id" wrt_pid)"
  nds_s="$(box_secs "$nds_raw")"
  printf 'BOX CHECK     %-26s router_uptime=%ss nds_uptime=%s (%ss) nds_pid=%s (was %s: OUR deliberate restart) wrt_pid=%s\n' \
    "$1" "${up:-?}" "${nds_raw:-<unreadable>}" "$nds_s" "${nds_pid:-?}" "${BOX_NDS_PID:-?}" "${wrt_pid:-?}"
  [ -n "$up" ] || why="$why router-uptime-unreadable;"
  [ -n "$nds_raw" ] || why="$why ndsctl-status-unreadable(wedged?);"
  [ "$wrt_pid" = "$BOX_WRT_PID" ] || why="$why tollgate-wrt-pid $BOX_WRT_PID->$wrt_pid(module self-restart, NOT our step);"
  if [ -n "$up" ] && [ -n "$BOX_UP_S" ]; then
    awk -v a="$BOX_UP_S" -v b="$up" 'BEGIN{ exit !(b >= a) }' \
      || why="$why router-uptime-went-backwards $BOX_UP_S->$up(reboot);"
  fi
  [ -z "$why" ] && printf 'ASSERT PASS  only nodogsplash moved: the deliberate restart is the single attributable change (through %s)\n' "$1"
  [ -z "$why" ] || box_broken "$1" "$why"
}

box_broken() {   # $1 = boundary label, $2 = what moved
  printf '\n'
  printf '!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n'
  printf '!!! THE BOX RESTARTED UNDER THE TEST — RESULT INVALID  (%s)\n' "$1"
  printf '!!! changed:%s\n' "$2"
  printf '!!! nodogsplash and/or tollgate-wrt came back on new pids: the module lost its\n'
  printf '!!! tracked sessions mid-run, so NOTHING measured from here on means anything.\n'
  printf '!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n'
  printf -- '--- restart-cause lines on the box (who bounced it):\n'
  router_log_grep 'signal 15|signal: killed|Merchant Initializing|Detected gateway|termination' || true
  printf 'ASSERT FAIL  the box stayed up for the whole run (through %s): %s\n' "$1" "$2"
  FAILED=$((FAILED + 1))
  exit "$EX_BOX_CHANGED"
}

# ---------------------------------------------------------------- PHASE 0

say "PHASE 0 fresh-client baseline (expect 307 -> splash, and NO egress)"
probe_verbose
egress
printf -- '--- balance as seen BY THE CLIENT MAC: %s\n' "$(balance)"
snap "phase0-fresh-client-baseline"
# The baseline is recorded LAST in PHASE 0, on purpose: anything that bounces the box (a fresh
# client state, a cleared ndsctl socket) must have finished before this line, or it is not a rig
# precondition — it is a restart under the test.
box_record "phase0-baseline"

# ---------------------------------------------------------------- PHASE 1

say "PHASE 1 FIRST PURCHASE"
buy "$TOKEN_1" "buy#1"
assert_eq "buy#1 answered HTTP 200" "200" "$LAST_HTTP"
if [ "$LANE" = token ]; then
  assert_contains "buy#1 returned kind:1022" '"kind":1022' "$LAST_BODY"
  ALLOTMENT_1="$(json_val allotment "$LAST_BODY")"
else
  assert_eq "buy#1 granted access (access_granted:true)" "true" "${LN_GRANT:-}"
  assert_grant_not_silent "buy#1" "$(grant_error_log)"
  ALLOTMENT_1="${LN_ALLOT:-}"
fi
printf -- '--- allotment#1 = %s bytes\n' "${ALLOTMENT_1:-<none>}"

sleep 5
GATE=0
i=1
while [ "$i" -le "$PROBE_TRIES" ]; do
  p="$(probe)"
  printf '  gate probe %s: %s\n' "$i" "$p"
  case "$p" in 204|200) GATE=1; break ;; esac
  sleep 10
  i=$((i + 1))
done
if [ "$GATE" != 1 ]; then
  printf '!!! buy#1 did NOT open the gate — the setup/rail is broken, not the product\n'
  printf -- '--- balance by client MAC: %s\n' "$(balance)"
  snap "first-purchase-no-gate"
  exit "$EX_NO_GATE"
fi
probe_verbose
printf -- '--- balance as seen BY THE CLIENT MAC: %s\n' "$(balance)"
snap "after-first-purchase"
box_assert_stable "after-phase1-buy1"

# ---------------------------------------------------------------- PHASE 2

say "PHASE 2 EXHAUST the allotment THROUGH the router"
# shellcheck disable=SC2086
set -- $BURN_URLS
urls=("$@")
if [ "${#urls[@]}" -eq 0 ] || [ -z "${urls[0]}" ]; then die "BURN_URLS is empty"; fi
printf -- '--- %s rounds x up to %s parallel downloads through the client interface\n' \
  "$BURN_ROUNDS" "$BURN_PARALLEL"
downloaded_b() { awk -F'bytes=' '{ gsub(/ .*/, "", $2); b += $2 } END { printf "%d", b + 0 }' "$DL_LOG"; }

CLOSED_AT=""
round=1
while [ "$round" -le "$BURN_ROUNDS" ]; do
  printf -- '--- round %s start bal=%s downloaded=%s B\n' "$round" "$(balance)" "$(downloaded_b)"
  pids=""
  i=0
  while [ "$i" -lt "$BURN_PARALLEL" ]; do
    url="${urls[$((i % ${#urls[@]}))]}"
    ( curl -s --interface "$CLIENT_IP" -m 300 -o /dev/null \
        -w "r$round s$i code=%{http_code} bytes=%{size_download}\n" "$url" >> "$DL_LOG" 2>&1 ) &
    pids="$pids $!"
    i=$((i + 1))
  done
  # shellcheck disable=SC2086
  wait $pids

  STRIKES=0
  t=1
  while [ "$t" -le "$WATCH_TRIES" ]; do
    sleep 15
    p="$(probe)"
    printf '  r%s t=%ss probe=%s bal=%s\n' "$round" "$((t * 15))" "$p" "$(balance)"
    case "$p" in
      204|200) STRIKES=0 ;;
      *) STRIKES=$((STRIKES + 1)); printf '  >>> strike %s/%s (probe=%s)\n' "$STRIKES" "$GATE_STRIKES" "$p" ;;
    esac
    if [ "$STRIKES" -ge "$GATE_STRIKES" ]; then
      CLOSED_AT="round=$round t=$((t * 15))s"
      printf '  >>> GATE CLOSED (%s)\n' "$CLOSED_AT"
      break
    fi
    t=$((t + 1))
  done
  if [ -n "$CLOSED_AT" ]; then break; fi
  round=$((round + 1))
done

TOTAL="$(awk -F'bytes=' '{ gsub(/ .*/, "", $2); b += $2 } END { printf "%.1f", (b + 0) / 1048576 }' "$DL_LOG")"
printf 'downloaded through the router: %s MiB\n' "$TOTAL"
if [ -z "$CLOSED_AT" ]; then
  printf '!!! HONEST RESULT: the gate never closed inside the budget (%s rounds) — INCONCLUSIVE\n' "$BURN_ROUNDS"
  snap "never-exhausted"
  exit "$EX_NO_EXHAUST"
fi
EXHAUST_LOG="$(router_log_grep 'allotment|closed gate|Removed expired session')"
printf '%s\n' "$EXHAUST_LOG"
assert_contains "module logged the allotment being reached" "allotment reached" "$EXHAUST_LOG"
box_assert_stable "after-phase2-exhaust"

# ---------------------------------------------------------------- PHASE 3

say "PHASE 3 POST-EXHAUSTION (the reported symptom: no internet, no prompt)"
probe_verbose
probe_verbose
printf -- '--- balance BY THE CLIENT MAC: %s\n' "$(balance)"
printf -- '--- balance from the box: %s\n' "$(curl -s -m 6 "$API_BASE/balance" || true)"
snap "post-exhaustion"
box_assert_stable "after-phase3-post-exhaustion"

# ---------------------------------------------------------------- PHASE 4

say "PHASE 4 DISCRIMINATOR: ndsctl deauth $CLIENT_MAC (is a stale nodogsplash session keeping it shut?)"
DEAUTH_SH="$(mktemp "${TMPDIR:-/tmp}/deauth.XXXXXX")"
cat > "$DEAUTH_SH" <<EOF
echo "-- before"
ndsctl json 2>&1 | head -c 400; echo
ndsctl deauth $CLIENT_MAC; echo "rc=\$?"
sleep 3
echo "-- after"
ndsctl json 2>&1 | head -c 400; echo
EOF
DEAUTH_OUT="$(run_on_router "$DEAUTH_SH" 2>&1)"
rm -f "$DEAUTH_SH"
printf '%s\n' "$DEAUTH_OUT"
# The finding is rc=1 and "not found" in the answer: no stale nodogsplash session kept the gate
# shut. The MESSAGE is `Client <MAC> not found.` — the MAC sits INSIDE it, so the older literal
# `Client not found` matched NOTHING (measured 2026-09-26 on the wireless run: the discriminator
# answered `Client a8:a0:92:a5:39:7a not found. rc=1` and the assertion still failed). The
# discriminator was right and the expected string was wrong.
assert_contains "deauth discriminator found no stale NDS session" "not found" "$DEAUTH_OUT"
if [ "$EXISTING_IFACE" = 1 ]; then
  # A real station legitimately REMAINS in nodogsplash's client list (it is a live client), so
  # for this vantage the discriminator is the STATE, not the entry's absence: after the
  # allotment was reached nothing may still be Authenticated for the client.
  assert_not_contains "no AUTHENTICATED nodogsplash entry survived the exhaustion" \
    '"state":"Authenticated"' "$DEAUTH_OUT"
fi
sleep 4
probe_verbose
probe_verbose
box_assert_stable "after-phase4-deauth"

# ---------------------------------------------------------------- the convergence check (runs in PHASE 5b)

# The convergence check ("a client that leaves nodogsplash must not leave an unretirable session
# behind") runs in PHASE 5b, after buy#2 — see the contract there, and
# tests/mt3000-bench/zombie-settle-control.sh for the offline proof that it can fail.
#
# It is deliberately NOT run here, straight after the deauth discriminator. On the measured
# behaviour the exhaustion of buy#1 has already retired that session ("Removed expired session for
# $CLIENT_MAC"), so the module holds NOTHING for the address at this point and a check here would
# have nothing to converge on: the only way it could "pass" is by matching a line logged BEFORE it
# started. PHASE 5b anchors its window for exactly that reason, and FORCES the drift there (a
# deliberate nodogsplash restart, which is what makes the module attempt a close at all) while the
# module holds the PAID session of buy#2 — the drift state the bench measured, and the one a
# customer can actually be stuck in.

# ---------------------------------------------------------------- PHASE 5

say "PHASE 5 SECOND PURCHASE (the money path: does the gate re-open?)"
buy "$TOKEN_2" "buy#2"
assert_eq "buy#2 answered HTTP 200" "200" "$LAST_HTTP"
if [ "$LANE" = token ]; then
  assert_contains "buy#2 returned kind:1022" '"kind":1022' "$LAST_BODY"
  ALLOTMENT_2="$(json_val allotment "$LAST_BODY")"
else
  assert_eq "buy#2 granted access (access_granted:true)" "true" "${LN_GRANT:-}"
  assert_grant_not_silent "buy#2" "$(grant_error_log)"
  ALLOTMENT_2="${LN_ALLOT:-}"
fi
printf -- '--- allotment#2 = %s bytes (allotment#1 was %s)\n' "${ALLOTMENT_2:-<none>}" "${ALLOTMENT_1:-<none>}"

sleep 8
printf -- '--- DECISIVE PROBES (204/200 = gate re-opened, 307/000 = the bug is reproduced)\n'
P1="$(probe)"
P2="$(probe)"
printf '  probe1=%s probe2=%s\n' "$P1" "$P2"
egress
snap "after-second-purchase"
box_assert_stable "after-phase5-buy2"
assert_eq "post-buy#2 probe 1 re-opened the gate" "open" "$(norm_probe "$P1")"
assert_eq "post-buy#2 probe 2 re-opened the gate" "open" "$(norm_probe "$P2")"

# ---------------------------------------------------------------- PHASE 5b

# THE DRIFT, FORCED. The client leaves nodogsplash while the module still holds the PAID allotment
# of buy#2, and the module's sweeps now find the session's counters unreadable — so a close has to
# happen, and `ndsctl deauth` answers `Client <mac> not found.` rc=1 for it. On the pre17 build that
# single answer starts the retry loop within 30 s (unconfirmed_closes reached 2141 in the
# 2026-09-26 capture); with the fix the module states that the client is gone, retires the session
# and stops touching ndsctl.
#
# THE FORCING STEP IS THE WHOLE POINT OF THIS PHASE and it is recorded as OURS: FORCE_DRIFT=restart
# (the default) restarts nodogsplash deliberately, announced in the router's own log and re-pinned
# in the box guard. FORCE_DRIFT=deauth keeps the old ndsctl-only step for the ablation — measured to
# produce NO close attempt at all (N_DEAUTH=0 N_UNCONF=0), so it proves nothing on its own.
say "PHASE 5b CONVERGENCE (forcing step: $FORCE_DRIFT): the module must close a gate nodogsplash no longer knows"

# The window is opened BEFORE the forcing step, so every line the step provokes is inside it. The
# last boundary check with the PRE-restart baseline is taken here: from the next line on, the only
# thing allowed to move a pid is the step itself.
box_assert_stable "phase5b-before-forcing-step"
CONVERGE_TOKEN="phase5b-$TS"
converge_window_open "$CONVERGE_TOKEN"
force_client_drift

# NO probe()/balance()/egress() here when the step is a restart: any packet FROM the client
# re-creates its nodogsplash record within a sweep or two, and the drift dissolves. The evidence is
# read from the box (ndsctl, the box identity) and from the module's own log window.
if [ "$FORCE_DRIFT" = deauth ]; then
  printf -- '--- the module still reports the session it is holding for %s: %s\n' "$CLIENT_MAC" "$(balance)"
else
  printf -- '--- the client stays SILENT for the rest of the phase (a packet from it would re-create its\n'
  printf -- '    nodogsplash record and dissolve the drift); evidence is read from the box and the log.\n'
fi

converge_assert_settled "PHASE 5b" "$CONVERGE_TOKEN" "$SETTLE_BUDGET"
converge_assert_state_change "PHASE 5b" "$CONVERGE_TOKEN"
converge_assert_counters "PHASE 5b" "$CONVERGE_TOKEN"
converge_assert_wording_and_socket "PHASE 5b" "$CONVERGE_TOKEN"
box_assert_stable "after-phase5b-settle"

# ---------------------------------------------------------------- end of PHASE 5b
#
# This terminator is load-bearing, not decoration: the offline control
# (tests/mt3000-bench/zombie-settle-control.sh) extracts the phase text BETWEEN these two
# separator lines, so the control can never drift out of sync with the run. A terminator that is
# a blanket `say "MODULE LOG"` lets the NEXT phase be swallowed into the extracted block: the
# control then ran its assertions over a neighbour's text (and died on that neighbour's variables)
# instead of over the settle phase. Keep the pair unique in this file:
#   start: `# ---- PHASE 5b`   end: `# ---- end of PHASE 5b`

# ---------------------------------------------------------------- epilogue: leave a clean pair
#
# PHASE 5b's forcing step restarted nodogsplash, and on a build that still retries the close the box
# is left with the loop hammering ndsctl — the very state in which the NEXT run's purchase cannot be
# authorised. So the lane ends by putting the bench back to a clean pair. This bounce is OURS, it is
# announced in the router's own log exactly like the forcing step, and it happens AFTER the evidence
# window, so it cannot contaminate the phase's evidence.
# CLEAN_PAIR_EPILOGUE=0 leaves the box exactly as the phase left it — use it for a run whose whole
# point is to capture the leftover state, and say so when you report the numbers.
if [ "$CLEAN_PAIR_EPILOGUE" = 1 ]; then
  say "EPILOGUE clean pair (nodogsplash + tollgate-wrt restart — attributable, AFTER the evidence)"
  EPI_SH="$(mktemp "${TMPDIR:-/tmp}/cleanpair.XXXXXX")"
  cat > "$EPI_SH" <<'EOF'
logger -t tollgate-bench "BENCH ACTION: end-of-run clean pair (nodogsplash + tollgate-wrt restart by the bench lane) — attributable to the test rig"
/etc/init.d/nodogsplash restart; sleep 2
/etc/init.d/tollgate-wrt restart
echo "clean pair: nds_pid=$(pgrep -f '[n]odogsplash' | head -1) wrt_pid=$(pgrep -f '[t]ollgate-wrt' | head -1)"
EOF
  run_on_router "$EPI_SH" || true
  rm -f "$EPI_SH"
  # The module needs 20-40 s to load its wallet before it answers again: a purchase attempted in
  # that window returns HTTP=000 and looks like a product failure. Wait for it, visibly.
  API_WAITED=0
  while [ "$API_WAITED" -le 90 ]; do
    API_CODE="$(curl -s -m 6 -o /dev/null -w '%{http_code}' "$API_BASE/" 2>/dev/null || true)"
    printf '  %ss after the clean pair: %s/ -> HTTP %s\n' "$API_WAITED" "$API_BASE" "${API_CODE:-000}"
    [ "$API_CODE" = "200" ] && break
    sleep 10
    API_WAITED=$((API_WAITED + 10))
  done
  if [ "$API_CODE" = "200" ]; then
    printf 'CLEAN PAIR OK: the bench answers again (waited %ss); the next run starts from a pair that is up\n' "$API_WAITED"
  else
    printf 'WARNING: the bench did not answer on %s/ within 90 s of the clean pair (HTTP %s) — the next\n' "$API_BASE" "${API_CODE:-000}"
    printf '         window must check the module itself before spending anything\n'
  fi
fi

say "MODULE LOG (decisive greps)"
router_log_grep 'baseline|allotment|closed gate|raced|restore|unconfirmed|grant|authoriz'

# ---------------------------------------------------------------- verdict

say "VERDICT"
printf 'allotment#1=%s  allotment#2=%s  closed_at=%s  downloaded=%s MiB\n' \
  "${ALLOTMENT_1:-<none>}" "${ALLOTMENT_2:-<none>}" "$CLOSED_AT" "$TOTAL"
printf 'box: pinned at PHASE 0 as router_uptime=%ss nds_pid=%s wrt_pid=%s; stable at every boundary\n' \
  "${BOX_UP_S:-?}" "${BOX_NDS_PID:-?}" "${BOX_WRT_PID:-?}"
printf 'forcing step: FORCE_DRIFT=%s (%s)\n' "$FORCE_DRIFT" \
  "$([ "$FORCE_DRIFT" = restart ] \
      && printf 'the deliberate NDS restart: the phase forced the close, so a PASS here covers the zombie-session path' \
      || printf 'the deauth-only ABLATION: measured to produce NO close attempt at all, so a PASS here does NOT cover the zombie-session path')"
printf 'epilogue: CLEAN_PAIR_EPILOGUE=%s (%s)\n' "$CLEAN_PAIR_EPILOGUE" \
  "$([ "$CLEAN_PAIR_EPILOGUE" = 1 ] && printf 'the bench was left as a clean pair' || printf 'the box was left exactly as PHASE 5b left it — say so in the report')"
printf 'log=%s\n' "$LOG"
if [ "$(norm_probe "$P1")" != "open" ]; then
  printf 'RESULT: SECOND PURCHASE DID NOT RE-OPEN THE GATE (probe=%s) — bug reproduced.\n' "$P1"
  exit "$EX_NO_REOPEN"
fi
if [ "$FAILED" -ne 0 ]; then
  printf 'RESULT: assertions failed (%s) — see ASSERT FAIL lines above.\n' "$FAILED"
  exit "$EX_ASSERT"
fi
printf 'RESULT: PASS — in THIS configuration the second purchase re-opened the gate.\n'
printf 'NOTE: lane=%s  client=%s  mac=%s  ip=%s  (vantage: %s)\n' \
  "$LANE" "${CLIENT_IFACE:-$CLIENT_VIF}" "$CLIENT_MAC" "$CLIENT_IP" \
  "$([ "$EXISTING_IFACE" = 1 ] && printf 'an EXISTING interface, i.e. a real station the AP already knew' || printf 'a fresh macvlan MAC')"
printf '      A PASS covers THIS lane, THIS allotment size and THIS client vantage only. The\n'
printf '      other lane, a different allotment size, and a client that already carries its own\n'
printf '      pre-existing state are SEPARATE runs — do NOT read one green run as "the operator\n'
printf '      report is fixed".\n'
exit "$EX_OK"
