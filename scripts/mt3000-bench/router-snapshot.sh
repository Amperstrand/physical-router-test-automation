#!/usr/bin/env bash
#
# router-snapshot.sh — the ONE reusable router-side snapshot / ssh transport for the bench.
#
# It folds the two ad-hoc helpers that the 2026-09-26 second-purchase session needed:
#
#   snapshot (default)      the read-only on-router state dump: ndsctl json/status, the two
#                           nft guard chains and their packet counters, /balance + /usage as
#                           the ROUTER sees them, and the decisive module-log greps.
#   run <local-script>      the ssh transport: pipe ANY local script to `sh -s` on the router
#                           (one self-contained invocation, options included).
#   render                  print the snapshot payload LOCALLY, no ssh and no lock — for
#                           review ("what are you about to run on my router?") and for the
#                           offline syntax checks in tests/mt3000-bench/.
#
# Both ssh modes are the same transport, which is why they are one file and not two — the
# session that produced them had `snap-remote.sh` (payload) and `bench-run-diag.sh`
# (transport) and kept getting them out of sync.
#
# WHY THE VANTAGE MATTERS
#   `snapshot` reads /balance and /usage with uclient-fetch ON THE ROUTER (127.0.0.1), so it
#   shows the session the BOX believes in — that is the point. It is NOT a client-vantage
#   probe: for "does a fresh MAC really get no internet" you must walk the guest path from
#   the client's own interface (scripts/mt3000-bench/second-purchase-e2e.sh).
#
# THE BENCH IS A SINGLE-OWNER RESOURCE
#   Every router-touching step runs under the sanctioned bench lock
#   (`bench-lock.sh`): this script calls `require` and refuses (exit 4) with the current
#   holder's identity unless it is already inside a lock window. A read-only triage while
#   another window owns the bench is possible but EXPLICIT: --allow-unlocked.
#
# USAGE
#   scripts/mt3000-bench/router-snapshot.sh snapshot [--label L] [--out FILE] [--allow-unlocked]
#   scripts/mt3000-bench/router-snapshot.sh run <local-script> [--out FILE] [--allow-unlocked]
#   scripts/mt3000-bench/router-snapshot.sh render [--label L]
#
#   # inside a bench window
#   bench-with-lock.sh --purpose "triage" -- scripts/mt3000-bench/router-snapshot.sh snapshot
#   bench-with-lock.sh --purpose "triage" -- scripts/mt3000-bench/router-snapshot.sh run ./probe.sh
#
# EXIT CODES
#   0 ok | 2 usage | 3 ssh transport failed | 4 not holding the bench lock | 6 local script missing
#
# ENV (all overridable)
#   ROUTER_IP        192.168.1.1
#   ROUTER_PW_FILE   ~/.tg-e2e/pw        (used with sshpass -f; never a password on argv)
#   ROUTER_PW        unset               (used with `sshpass -e` when set)
#   ROUTER_KNOWN_HOSTS  ~/.hermes/state/bench-mt3000.known_hosts
#   SSH_TIMEOUT      8                   (ConnectTimeout seconds)
#   ROUTER_USER      root
#
# TRAP (paid for on 2026-09-26): `> /dev/stdout` in a helper TRUNCATES a log file that
# stdout is already redirected to. This script therefore captures into a temp file and cats
# it, or appends with `>>`, and never writes to /dev/stdout.
#
set -uo pipefail

EX_OK=0
EX_USAGE=2
EX_SSH=3
EX_NO_LOCK=4
EX_NOSCRIPT=6

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
HERE="$(cd "$(dirname "$SELF")" && pwd)"

ROUTER_IP="${ROUTER_IP:-192.168.1.1}"
ROUTER_USER="${ROUTER_USER:-root}"
ROUTER_PW_FILE="${ROUTER_PW_FILE:-$HOME/.tg-e2e/pw}"
ROUTER_KNOWN_HOSTS="${ROUTER_KNOWN_HOSTS:-$HOME/.hermes/state/bench-mt3000.known_hosts}"
SSH_TIMEOUT="${SSH_TIMEOUT:-8}"

die() { printf 'router-snapshot: %s\n' "$*" >&2; exit "${EX_USAGE}"; }

# The usage text is the file's own header comment: it cannot drift from the code.
usage() {
  grep -E '^#' "$SELF" | grep -v '^#!' | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------- transport

# A function, never a string: `SSH="sshpass ... ssh ..."` then `"$SSH" cmd` makes the whole
# thing ONE word (measured live on the user's machine — "command not found").
rssh() {
  local -a opts=(
    -o "StrictHostKeyChecking=accept-new"
    -o "UserKnownHostsFile=$ROUTER_KNOWN_HOSTS"
    -o "ConnectTimeout=$SSH_TIMEOUT"
    -o "ServerAliveInterval=10"
  )
  local target="$ROUTER_USER@$ROUTER_IP"
  if command -v sshpass >/dev/null 2>&1; then
    if [ -n "${ROUTER_PW:-}" ]; then
      SSHPASS="$ROUTER_PW" sshpass -e ssh "${opts[@]}" "$target" "$@"
      return
    fi
    if [ -s "$ROUTER_PW_FILE" ]; then
      sshpass -f "$ROUTER_PW_FILE" ssh "${opts[@]}" "$target" "$@"
      return
    fi
  fi
  ssh "${opts[@]}" "$target" "$@"
}

# ---------------------------------------------------------------- payload

# The read-only snapshot, verbatim from the session's snap-remote.sh. BusyBox-safe: this box
# has no stat(1), no curl(1) and drops ICMP; log greps are the decisive ones.
payload_snapshot() {
  cat <<'EOS'
LABEL="@@LABEL@@"
echo "--- label: ${LABEL:-none}"
echo "--- ts: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "--- identity"
cat /etc/openwrt_release 2>/dev/null | grep -E '^(DISTRIB_|OPENWRT_)' || true
uname -a
apk info -v 2>/dev/null | grep -i 'tollgate\|nodogsplash' || true
echo "--- ndsctl json (full client table)"
ndsctl json 2>&1
echo "--- ndsctl status (head)"
ndsctl status 2>&1 | sed -n '1,25p'
echo "--- guard chain counters (data allotment: nds_enforce_forward)"
nft list chain inet fw4 nds_enforce_forward 2>&1
echo "--- guard chain counters (admin board: admin_board_input_guard)"
nft list chain inet fw4 admin_board_input_guard 2>&1 | sed -n '1,8p'
echo "--- guard fragments on disk"
ls -1 /etc/nftables.d/ 2>&1
echo "--- balance/usage AS THE ROUTER SEES THEM (loopback)"
uclient-fetch -q -O - -T 5 http://127.0.0.1:2121/balance 2>&1 | head -c 300; echo
uclient-fetch -q -O - -T 5 http://127.0.0.1:2121/usage 2>&1 | head -c 300; echo
echo "--- module log (decisive greps)"
logread 2>/dev/null | grep -iE "baseline|allotment|closed gate|close|raced|restore|unconfirmed|grant|authoriz|session|exhaust|meter|openGates" | tail -30
echo "--- last 8 raw log lines"
logread 2>/dev/null | tail -8 | cut -c1-200
echo "SNAP_DONE"
EOS
}

# $1 = label. The label is interpolated (not exported) so quoting stays simple, and it is
# sanitised because it ends up inside the remote shell's double quotes.
render_snapshot() {
  local label payload
  label="$(printf '%s' "${1:-}" | tr -d '"\\$`' | tr -c 'A-Za-z0-9_.:-' '_')"
  payload="$(payload_snapshot)"
  printf '%s\n' "${payload//@@LABEL@@/$label}"
}

# ---------------------------------------------------------------- main

MODE=""
LABEL=""
OUT=""
LOCAL_SCRIPT=""
ALLOW_UNLOCKED=0

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit "$EX_OK" ;;
    snapshot|run|render) [ -n "$MODE" ] && die "mode already set to '$MODE'"; MODE="$1"; shift ;;
    --label) [ $# -ge 2 ] || die "--label needs a value"; LABEL="$2"; shift 2 ;;
    --out)   [ $# -ge 2 ] || die "--out needs a value";   OUT="$2";   shift 2 ;;
    --allow-unlocked) ALLOW_UNLOCKED=1; shift ;;
    -*) die "unknown option '$1' (see --help)" ;;
    *)
      if [ "$MODE" = "run" ] && [ -z "$LOCAL_SCRIPT" ]; then LOCAL_SCRIPT="$1"; shift
      elif [ -z "$MODE" ]; then MODE="$1"; shift
      else die "unexpected argument '$1' (see --help)"; fi
      ;;
  esac
done

: "${MODE:=snapshot}"
if [ "$MODE" = "run" ] && [ -z "$LOCAL_SCRIPT" ]; then die "run needs a local script path"; fi

# ---- lock: refuse (naming the holder) unless we are inside a bench window.
#      `render` never touches the router, so it needs no lock and no ssh.
if [ "$MODE" != "render" ]; then
  if [ "$ALLOW_UNLOCKED" != 1 ]; then
    if ! "$HERE/bench-lock.sh" require; then
      exit "$EX_NO_LOCK"
    fi
  else
    printf 'router-snapshot: NOTE: --allow-unlocked — read-only triage outside the bench lock.\n' >&2
  fi
fi

# ---- run the payload over ssh, capture into a temp file then show it
TMP_OUT="$(mktemp "${TMPDIR:-/tmp}/router-snapshot.XXXXXX")"
trap 'rm -f "$TMP_OUT"' EXIT

if [ "$MODE" = "render" ]; then
  render_snapshot "$LABEL" > "$TMP_OUT"
  rc=0
elif [ "$MODE" = "run" ]; then
  [ -f "$LOCAL_SCRIPT" ] || { printf 'router-snapshot: no such local script: %s\n' "$LOCAL_SCRIPT" >&2; exit "$EX_NOSCRIPT"; }
  rssh 'sh -s' < "$LOCAL_SCRIPT" > "$TMP_OUT" 2>&1
  rc=$?
else
  render_snapshot "$LABEL" | rssh 'sh -s' > "$TMP_OUT" 2>&1
  rc=$?
fi

cat "$TMP_OUT"
if [ -n "$OUT" ]; then
  mkdir -p "$(dirname "$OUT")"
  cat "$TMP_OUT" >> "$OUT"          # append: never truncate someone else's log
  printf 'router-snapshot: appended %s bytes to %s\n' "$(wc -c < "$TMP_OUT" | tr -d ' ')" "$OUT" >&2
fi

# A snapshot that never printed its completion marker is a TRANSPORT failure, not a clean
# empty result — an unanswered ssh must never read as "the router is idle".
if [ "$MODE" = "snapshot" ] && ! grep -q 'SNAP_DONE' "$TMP_OUT"; then
  printf 'router-snapshot: the snapshot never reached SNAP_DONE (transport failure?)\n' >&2
  exit "$EX_SSH"
fi
[ "$rc" -eq 0 ] || exit "$EX_SSH"
exit "$EX_OK"
