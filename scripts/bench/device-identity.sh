#!/usr/bin/env bash
# =============================================================================
# scripts/bench/device-identity.sh — the bench DEVICE-IDENTITY guard (FAIL CLOSED)
#
# WHY THIS EXISTS (measured 2026-09-28, bench host)
#   Two routers both answered on 192.168.1.1: the GL-MT3000 (via the NIC
#   `enp0s31f6`, host address 192.168.1.200) and a Cudy WR3000 (via a USB
#   dongle). The first two WAN-less offline-install attempts SILENTLY ADDRESSED
#   THE WRONG DEVICE, and the Cudy may have been flashed as a side effect — a
#   destructive cross-workstream accident, not a cosmetic bug. The lane that
#   finally got a trustworthy run did it by SOURCE-BINDING
#   (`ssh -o BindAddress=192.168.1.200`) and pinning the target by source
#   address. That ad-hoc fix is this script.
#
# WHAT IT DOES
#   * `claim`  — over the interface you NAME, prove the box at the address is
#                reachable there, read its LAN (br-lan) MAC, and write a small
#                identity record. It refuses to write anything it could not
#                read, and refuses when the address answers on another iface.
#   * `verify` — the important one. Given the record it (a) forces all traffic
#                out of the claimed interface/source address, (b) re-reads the
#                box's LAN MAC, (c) compares it with the record, and (d) FAILS
#                CLOSED, naming expected + observed MAC and the interface, when
#                they differ. Missing/ambiguous record, unreachable address or
#                an unreadable MAC are all refusals — never a pass by silence.
#   * `show`   — print what the record says (read-only; no network, no MAC read).
#
# THE ONE-LINE PREFLIGHT (call this before ANY destructive step: flash, install,
# reboot, sysupgrade — and before a paid E2E):
#
#     scripts/bench/device-identity.sh verify --name bench-mt3000 || exit $?
#
#   or, inside a script that already knows the rig dir:
#
#     "$(dirname "$0")/../bench/device-identity.sh" verify --name "$BENCH_BOX" || exit $?
#
#   Records live in scripts/bench/boxes/<name>.identity (override the directory
#   with DI_BOXES_DIR, or point at one record with --file PATH).
#
#   Exit code 0 means "the box answering at this address, on this interface, IS
#   the box that was claimed" — nothing more. Any non-zero code is a refusal and
#   the destructive step must not run.
#
# HOW THE MAC IS READ — and what each method PROVES
#   --method neigh (default, no credentials, host-side):
#       1. `ip route get <ip> from <src>` must resolve to the CLAIMED interface,
#          otherwise the box is not where we think it is -> refuse (exit 5);
#       2. a TCP touch is forced out of the claimed source address
#          (`curl --interface <src>`), which makes the kernel resolve the L2
#          address of the *claimed* path — ICMP is not used: this bench drops ping;
#       3. `ip -4 neigh show dev <iface>` is read back.
#       It proves: "the box that MY CLAIMED SOURCE ADDRESS hits at this IP, on
#       this interface, has this LAN MAC" — i.e. exactly the wrong-device
#       accident. It is credential-free. It needs a successful TCP touch to make
#       the neighbour entry REACHABLE (a stale entry is not trusted unless it
#       carries a lladdr and a non-FAILED state).
#   --method ssh (credential-gated, authoritative on the box):
#       `ssh -o BindAddress=<src> ... root@<ip> cat /sys/class/net/br-lan/address`
#       It proves: the box answering over the BOUND path says its own br-lan MAC
#       is X, AND that credentials reach that box. Stronger attribute, weaker
#       availability (needs the lab credential) — use it when it works, and note
#       that a refused key/password is NOT a pass.
#   --check-hostname (optional, with --method ssh): also compares
#       `uci get system.@system[0].hostname` with `hostname=` in the record.
#
# SOURCE BINDING (never "whatever the default route picks")
#   ssh  : `-o BindAddress=<src_addr>`
#   probe: `curl --interface <src_addr>` (the source address, not the iface name)
#   route: `ip route get <ip> from <src_addr>` must say `dev <iface>`
#   A verification that cannot bind is a REFUSAL, not a best-effort check.
#
# EXIT CODES (any non-zero = do not touch the box)
#   0 ok
#   2 usage / bad arguments
#   3 LAN MAC MISMATCH — the address answered with a DIFFERENT box (fail closed)
#   4 address unreachable over the claimed interface/source (fail closed)
#   5 claimed interface/source cannot be used: source not on the iface, or the
#     route to the address goes out a DIFFERENT interface (fail closed)
#   6 identity record missing / incomplete / ambiguous (fail closed)
#   7 the LAN MAC could not be read (fail closed)
#   8 `claim`: an existing record for this box disagrees and was NOT overwritten
#     (re-run with --force once you are sure which box you are on)
#   9 optional attribute mismatch (hostname / device_code)
#   10 internal: a required tool is missing, or state could not be established
#
# RECORD FORMAT (`<boxes>/<name>.identity`, plain key=value, `#` comments)
#   box=<name>            iface=<host interface, e.g. enp0s31f6>
#   src_addr=<host addr on that iface, e.g. 192.168.1.200>
#   router_ip=<address of the box on that iface, e.g. 192.168.1.1>
#   lan_mac=<the box's own br-lan MAC, lowercase colon form>
#   hostname=<optional, e.g. GL-MT3000>
#   device_code=<optional, e.g. mt3000 | cudy-wr3000>
#   claimed_at=<iso8601>  claimed_by=<host>  claimed_via=<mac read method>
#
# ENV
#   DI_BOXES_DIR    where records live (default: <repo>/scripts/bench/boxes)
#                   `--file PATH` overrides for a one-off record.
#   DI_PROBE_PORTS  TCP ports tried by the credential-free touch (default "2050 80 22")
#   DI_PROBE_TIMEOUT / DI_SSH_TIMEOUT   seconds
#
# TEST SEAM (offline harness only, tests/bench-device-identity/)
#   `ip`, `curl` and `ssh` are resolved through PATH, so the no-router suite puts
#   PATH DOUBLES in front of them and runs this script unmodified. There is no
#   test-aware branch in this file.
#
# usage: device-identity.sh claim  --name <box> (--iface IFACE | --src ADDR) [--router-ip IP] [--hostname H] [--device-code C] [--force] [--file PATH]
#        device-identity.sh verify --name <box> | --file PATH  [--method neigh|ssh] [--check-hostname] [--json]
#        device-identity.sh show   --name <box> | --file PATH
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
BOXES_DIR="${DI_BOXES_DIR:-$HERE/boxes}"

EX_OK=0
EX_USAGE=2
EX_MISMATCH=3
EX_UNREACHABLE=4
EX_IFACE=5
EX_IDENTITY=6
EX_NOMAC=7
EX_CONFLICT=8
EX_ATTR=9
EX_INTERNAL=10

PROBE_PORTS="${DI_PROBE_PORTS:-2050 80 22}"
PROBE_TIMEOUT="${DI_PROBE_TIMEOUT:-4}"
SSH_TIMEOUT="${DI_SSH_TIMEOUT:-8}"

# state, filled in as we learn it (used by the loud refusals and by --json)
DI_NAME=""; DI_IFACE=""; DI_SRC=""; DI_IP=""; DI_EXPECTED=""; DI_OBSERVED=""; DI_METHOD=""

say()  { printf 'device-identity: %s\n' "$*"; }
note() { printf 'device-identity: %s\n' "$*" >&2; }

json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

emit_json() {   # $1=status $2=reason
  [ "${JSON:-0}" = 1 ] || return 0
  printf '{"status":"%s","exit":%s,"reason":"%s","box":"%s","iface":"%s","src_addr":"%s","router_ip":"%s","expected_mac":"%s","observed_mac":"%s","method":"%s"}\n' \
    "$(json_escape "$1")" "$3" "$(json_escape "$2")" \
    "$(json_escape "$DI_NAME")" "$(json_escape "$DI_IFACE")" "$(json_escape "$DI_SRC")" \
    "$(json_escape "$DI_IP")" "$(json_escape "$DI_EXPECTED")" "$(json_escape "$DI_OBSERVED")" \
    "$(json_escape "$DI_METHOD")"
}

# The loud, actionable refusal. Everything the operator (or the next agent) needs
# to decide is in here: what was claimed, what was observed, what NOT to do.
refuse() {   # $1=rc $2=status-word ; rest = message lines
  local rc="$1" word="$2"; shift 2
  emit_json "$word" "$*" "$rc"
  printf '\n' >&2
  printf '!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n' >&2
  printf '!!! device-identity: REFUSED (exit %s) — %s\n' "$rc" "$word" >&2
  printf '!!! DO NOT FLASH, INSTALL, REBOOT OR sysupgrade ANYTHING ON THIS BENCH.\n' >&2
  printf '!!! claimed box  : %s\n' "${DI_NAME:-<none>}" >&2
  printf '!!! interface    : %s\n' "${DI_IFACE:-<none>}" >&2
  printf '!!! host source  : %s\n' "${DI_SRC:-<none>}" >&2
  printf '!!! router ip    : %s\n' "${DI_IP:-<none>}" >&2
  printf '!!! expected LAN MAC : %s\n' "${DI_EXPECTED:-<unknown>}" >&2
  printf '!!! observed LAN MAC : %s\n' "${DI_OBSERVED:-<unknown>}" >&2
  printf '!!! method       : %s\n' "${DI_METHOD:-<none>}" >&2
  local l
  for l in "$@"; do printf '!!! %s\n' "$l" >&2; done
  printf '!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n' >&2
  exit "$rc"
}

usage() {
  awk '
    /^# THE ONE-LINE PREFLIGHT/ { p=1 }
    p && /^# EXIT CODES/ { p=0 }
    p { sub(/^# ?/, ""); print }
    /^# usage: device-identity/ { q=1 }
    q && /^# =====/ { exit }
    q { sub(/^# ?/, ""); print }
  ' "$0" >&2
  exit "$EX_USAGE"
}

# ---------------------------------------------------------------- small helpers

norm_mac() {   # stdout: lowercase colon form, or nothing if it is not a MAC
  local m
  m="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[^0-9a-f]//g' | tr -cd '0-9a-f')"
  [ "${#m}" = 12 ] || return 1
  printf '%s' "$m" | sed 's/\(..\)/\1:/g; s/:$//'
}

have() { command -v "$1" >/dev/null 2>&1; }

require_tools() {
  have ip || refuse "$EX_INTERNAL" "INTERNAL" \
    "the 'ip' tool is missing" \
    "This guard never falls back to a guess about which box is where."
  return 0
}

# `ip -o link show dev <iface>` -> is the iface UP (and does it exist)?
iface_up() {   # $1=iface ; rc 0 = exists and UP, 1 = exists but down, 2 = no such iface
  local out
  out="$(ip -o link show dev "$1" 2>/dev/null)" || return 2
  case "$out" in
    *"<"*"UP"*">"*) return 0 ;;
    *) return 1 ;;
  esac
}

iface_addrs() {   # $1=iface ; stdout: one IPv4 address per line (CIDR stripped)
  ip -o -4 addr show dev "$1" 2>/dev/null | awk '{print $4}' | cut -d/ -f1
}

iface_for_addr() {   # $1=addr ; stdout: the iface that carries it (empty if none/many)
  local line count=0 found=""
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    count=$((count + 1)); found="${line%% *}"
  done < <(ip -o -4 addr show 2>/dev/null | awk -v a="$1" '{split($4,c,"/"); if (c[1]==a) print $2}')
  [ "$count" = 1 ] || return 1
  printf '%s' "$found"
}

# Which interface does the kernel actually use for <ip>, from <src> (if given)?
# This is the "the address answers on a different interface" detector.
route_dev() {   # $1=ip $2=src(optional) ; stdout: dev name, empty if undeterminable
  local out
  if [ -n "${2:-}" ]; then
    out="$(ip route get "$1" from "$2" 2>/dev/null)"
    [ -n "$out" ] && printf '%s' "$out" | sed -n 's/.* dev \([^ ]*\).*/\1/p' && return 0
  fi
  out="$(ip route get "$1" 2>/dev/null)"
  [ -n "$out" ] || return 1
  printf '%s' "$out" | sed -n 's/.* dev \([^ ]*\).*/\1/p'
}

# Force a TCP touch out of the claimed SOURCE ADDRESS (never the default route).
# ICMP is not used: this bench drops ping (a ping-based "reachability" check is a
# false negative, and an unbound TCP check is a false positive).
touch_src() {   # $1=src $2=ip ; rc 0 = some port answered over the bound source
  local port out rc=1
  for port in $PROBE_PORTS; do
    if have curl; then
      out="$(curl -s -o /dev/null -m "$PROBE_TIMEOUT" --interface "$1" \
                 --connect-timeout "$PROBE_TIMEOUT" "http://$2:$port/" 2>/dev/null)"
      rc=$?
      # 0 = HTTP answered; 52/55/56 = the TCP connection happened but the peer
      # said nothing / not HTTP (that is an ssh or odd port, still REACHABLE).
      case "$rc" in 0|52|55|56) return 0 ;; esac
    elif have python3; then
      if python3 - "$1" "$2" "$port" "$PROBE_TIMEOUT" <<'PY' >/dev/null 2>&1
import socket, sys
src, ip, port, t = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind((src, 0))
s.settimeout(t)
s.connect((ip, port))
s.close()
PY
      then return 0; fi
    else
      return 2
    fi
  done
  return 1
}

read_mac_neigh() {   # $1=iface $2=ip -> stdout: MAC from the neighbour table
  local out mac state
  out="$(ip -4 neigh show dev "$1" 2>/dev/null)"
  [ -n "$out" ] || return 1
  # keep only the entries for our address, then require a lladdr and a usable state
  out="$(printf '%s\n' "$out" | grep -F -- "$2 " || true)"
  [ -n "$out" ] || return 1
  case "$out" in *FAILED*|*INCOMPLETE*) : ;; esac
  mac="$(printf '%s\n' "$out" | head -1 | sed -n 's/.* lladdr \([0-9A-Fa-f:]\{17\}\).*/\1/p')"
  state="$(printf '%s\n' "$out" | head -1 | awk '{print $NF}')"
  [ -n "$mac" ] || return 1
  case "$state" in FAILED|INCOMPLETE) return 1 ;; esac
  norm_mac "$mac"
}

read_mac_ssh() {   # $1=src $2=ip [3=hostname wanted] -> stdout: "$mac" or "$mac hostname"
  local out
  out="$(ssh -o "BindAddress=$1" -o BatchMode=yes -o "ConnectTimeout=$SSH_TIMEOUT" \
             -o StrictHostKeyChecking=accept-new "root@$2" \
             'cat /sys/class/net/br-lan/address; echo "HOSTNAME=$(uci get system.@system[0].hostname 2>/dev/null)"' 2>/dev/null)"
  [ -n "$out" ] || return 1
  printf '%s\n' "$out" | tail -n +1
}

read_mac() {   # sets DI_OBSERVED ; $1=iface $2=src $3=ip $4=method(wanted) ; rc 0 = read
  local raw m
  case "$4" in
    neigh)
      DI_METHOD="neigh"
      touch_src "$2" "$3" || return 1
      m="$(read_mac_neigh "$1" "$3")" || return 1
      [ -n "$m" ] || return 1
      DI_OBSERVED="$m"
      return 0
      ;;
    ssh)
      DI_METHOD="ssh(bind:$2)"
      raw="$(read_mac_ssh "$2" "$3")" || return 1
      m="$(norm_mac "$(printf '%s\n' "$raw" | head -1)")" || return 1
      [ -n "$m" ] || return 1
      DI_OBSERVED="$m"
      DI_SSH_HOSTNAME="$(printf '%s\n' "$raw" | sed -n 's/^HOSTNAME=//p' | head -1)"
      return 0
      ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------- record file

idfile_read() {   # $1=file $2=key -> stdout: value (last wins), "" if absent
  awk -v k="$2" -F= '
    /^[[:space:]]*#/ { next }
    { key=$1; sub(/^[[:space:]]*[^=]*=/, "", $0); if (key==k) { v=$0 } }
    END { print v }' "$1" 2>/dev/null
}

# Resolve WHICH record we are talking about. Missing or ambiguous is a refusal:
# "two records claim the same box" is exactly how a rig silently pins the wrong one.
resolve_record() {   # sets DI_FILE ; $1 = name or "" , $2 = explicit file or ""
  local name="$1" file="$2" cands="" f b
  if [ -n "$file" ]; then
    [ -e "$file" ] || refuse "$EX_IDENTITY" "IDENTITY-FILE-MISSING" \
      "no identity record at $file" \
      "Create one first:  scripts/bench/device-identity.sh claim --name <box> --iface <iface> --src <addr>"
    [ -f "$file" ] || refuse "$EX_IDENTITY" "IDENTITY-FILE-NOT-A-FILE" "$file is not a regular file"
    DI_FILE="$file"
    [ -n "$DI_NAME" ] || DI_NAME="$(idfile_read "$DI_FILE" box)"
    return 0
  fi
  [ -n "$name" ] || refuse "$EX_USAGE" "USAGE" "one of --name <box> or --file <path> is mandatory"
  [ -d "$BOXES_DIR" ] || refuse "$EX_IDENTITY" "IDENTITY-DIR-MISSING" \
    "no box-record directory at $BOXES_DIR" \
    "Create the first record with:  device-identity.sh claim --name $name --iface <iface> --src <addr>"
  for f in "$BOXES_DIR"/*.identity; do
    [ -e "$f" ] || continue
    b="$(idfile_read "$f" box)"
    base="$(basename "$f" .identity)"
    # a record counts for this box if it SAYS so, or if it is NAMED after it; two
    # matching files is ambiguity, and ambiguity is refused — "two records claim
    # the same box" is exactly how a rig silently pins the wrong one.
    case "$b" in "$name") cands="$cands
$f" ;; esac
    case "$base" in "$name") cands="$cands
$f" ;; esac
  done
  cands="$(printf '%s\n' "$cands" | sed '/^$/d' | sort -u)"
  # shellcheck disable=SC2046  # deliberate word splitting: one path per candidate
  set -- $(printf '%s' "$cands")
  if [ "$#" -eq 0 ]; then
    refuse "$EX_IDENTITY" "IDENTITY-MISSING" \
      "no identity record for box '$name' in $BOXES_DIR" \
      "The bench is NOT pinned. Claim it first:" \
      "  scripts/bench/device-identity.sh claim --name $name --iface <iface> --src <addr>" \
      "Never flash/install while the target identity is unknown."
  fi
  if [ "$#" -gt 1 ]; then
    refuse "$EX_IDENTITY" "IDENTITY-AMBIGUOUS" \
      "$# identity records claim box '$name' — refusing to guess which one is the bench:" \
      "$(printf '%s' "$cands" | tr '\n' ' ')" \
      "Keep exactly one record per box (delete or rename the stale ones)."
  fi
  DI_FILE="$1"
  DI_NAME="$name"
}

# Sets REC_VAL. Never called inside $( ) — a refusal here must really exit.
record_require() {   # $1=key
  REC_VAL="$(idfile_read "$DI_FILE" "$1")"
  [ -n "$REC_VAL" ] || refuse "$EX_IDENTITY" "IDENTITY-INCOMPLETE" \
    "the identity record $DI_FILE has no '$1='" \
    "A record that cannot pin the interface, the source address and the LAN MAC is not an identity." \
    "Re-claim it:  scripts/bench/device-identity.sh claim --name ${DI_NAME:-<box>} --iface <iface> --src <addr> --force"
  return 0
}

write_record() {   # $1=file ; uses DI_* + DI_EXTRA_*
  local tmp
  tmp="$(mktemp "${DI_FILE%/*}/.identity.XXXXXX")" || refuse "$EX_INTERNAL" "INTERNAL" "cannot write next to $DI_FILE"
  {
    printf '# bench box identity record — written by scripts/bench/device-identity.sh claim\n'
    printf '# verify it before ANY destructive step:\n'
    printf '#   scripts/bench/device-identity.sh verify --name %s || exit $?\n' "$DI_NAME"
    printf 'box=%s\n' "$DI_NAME"
    printf 'iface=%s\n' "$DI_IFACE"
    printf 'src_addr=%s\n' "$DI_SRC"
    printf 'router_ip=%s\n' "$DI_IP"
    printf 'lan_mac=%s\n' "$DI_EXPECTED"
    [ -n "${DI_EXTRA_HOSTNAME:-}" ] && printf 'hostname=%s\n' "$DI_EXTRA_HOSTNAME"
    [ -n "${DI_EXTRA_DEVICE_CODE:-}" ] && printf 'device_code=%s\n' "$DI_EXTRA_DEVICE_CODE"
    printf 'claimed_at=%s\n' "$(date -u +%FT%TZ)"
    printf 'claimed_by=%s\n' "$(hostname 2>/dev/null || echo unknown-host)"
    printf 'claimed_via=%s\n' "$DI_METHOD"
  } > "$tmp"
  chmod 0644 "$tmp"
  mv -f "$tmp" "$1"
}

# ---------------------------------------------------------------- modes

cmd_claim() {
  local want_iface="${OPT_IFACE:-}" want_src="${OPT_SRC:-}" want_ip="${OPT_ROUTER_IP:-192.168.1.1}"
  local file="${OPT_FILE:-}" name="${OPT_NAME:-}"
  [ -n "$name" ] || refuse "$EX_USAGE" "USAGE" "--name <box> is mandatory for claim"
  DI_NAME="$name"; DI_IP="$want_ip"

  # --- pick the interface: named, or the one carrying the named source address.
  if [ -n "$want_iface" ]; then
    DI_IFACE="$want_iface"
  elif [ -n "$want_src" ]; then
    DI_IFACE="$(iface_for_addr "$want_src")" || refuse "$EX_IFACE" "SOURCE-NOT-ON-ONE-IFACE" \
      "the host source address $want_src is not carried by exactly one interface" \
      "claim must name the interface explicitly:  --iface <iface>"
  else
    refuse "$EX_USAGE" "USAGE" \
      "claim must name the interface (--iface) or the source address (--src)" \
      "Guessing (the default route, the Wi-Fi card, 'the address that answers') is the bug this guard exists for."
  fi

  iface_up "$DI_IFACE"; case "$?" in
    0) : ;;
    1) refuse "$EX_IFACE" "IFACE-DOWN" "interface $DI_IFACE exists but is not UP" ;;
    *) refuse "$EX_IFACE" "IFACE-MISSING" "no such interface: $DI_IFACE" ;;
  esac

  local addrs
  addrs="$(iface_addrs "$DI_IFACE")"
  if [ -n "$want_src" ]; then
    printf '%s\n' "$addrs" | grep -qxF -- "$want_src" || refuse "$EX_IFACE" "SOURCE-NOT-ON-IFACE" \
      "host source address $want_src is not on interface $DI_IFACE" \
      "addresses on $DI_IFACE: $(printf '%s' "$addrs" | tr '\n' ' ')"
    DI_SRC="$want_src"
  else
    local n
    n="$(printf '%s\n' "$addrs" | grep -c . || true)"
    [ "$n" = 1 ] || refuse "$EX_IFACE" "SOURCE-AMBIGUOUS" \
      "interface $DI_IFACE carries $n IPv4 addresses — name the one to bind: --src <addr>" \
      "addresses on $DI_IFACE: $(printf '%s' "$addrs" | tr '\n' ' ')"
    DI_SRC="$(printf '%s\n' "$addrs" | head -1)"
  fi

  # --- does the address even answer over THIS interface/source?
  local dev
  dev="$(route_dev "$DI_IP" "$DI_SRC")"
  if [ -z "$dev" ]; then
    refuse "$EX_IFACE" "ROUTE-UNKNOWN" \
      "cannot determine which interface the kernel would use for $DI_IP from $DI_SRC" \
      "Refusing rather than writing a record that pins nothing."
  fi
  if [ "$dev" != "$DI_IFACE" ]; then
    refuse "$EX_IFACE" "ADDRESS-ON-A-DIFFERENT-INTERFACE" \
      "$DI_IP is reached out of interface '$dev', NOT the claimed '$DI_IFACE'" \
      "(from source $DI_SRC). Writing this record would pin the WRONG box — this is the" \
      "2026-09-28 accident: two routers answering on the same address." \
      "Either claim over '$dev' (and record that box there), or pin a route: --src that forces $DI_IFACE."
  fi

  # --- read the box's LAN MAC over the bound path
  local warn_ssh=""
  DI_OBSERVED=""
  if read_mac "$DI_IFACE" "$DI_SRC" "$DI_IP" "${OPT_METHOD:-neigh}"; then
    :
  elif [ "${OPT_METHOD:-neigh}" = neigh ]; then
    if [ "${OPT_SSH_FALLBACK:-0}" = 1 ]; then
      warn_ssh="reachable, but no neighbour entry — falling back to --method ssh"
      read_mac "$DI_IFACE" "$DI_SRC" "$DI_IP" ssh || true
    fi
  fi
  if [ -n "$warn_ssh" ]; then note "WARNING: $warn_ssh"; fi

  if [ -z "$DI_OBSERVED" ]; then
    # distinguish "no answer at all" (exit 4) from "answered, but no MAC" (exit 7)
    if ! touch_src "$DI_SRC" "$DI_IP"; then
      refuse "$EX_UNREACHABLE" "UNREACHABLE" \
        "nothing answered at $DI_IP over $DI_IFACE from $DI_SRC (ports tried: $PROBE_PORTS)" \
        "The address is NOT reachable over the claimed interface/source." \
        "A box on another interface (or another cable) does not count — this is the accident this guard blocks."
    fi
    refuse "$EX_NOMAC" "NO-LAN-MAC-READABLE" \
      "the box at $DI_IP answered over $DI_IFACE but no LAN MAC could be read (method=${OPT_METHOD:-neigh})" \
      "A record without a MAC pins nothing. Do not write one."
  fi
  DI_EXPECTED="$DI_OBSERVED"

  # --- refuse to overwrite a record for the SAME box that disagrees
  local file_to_write
  if [ -n "$file" ]; then file_to_write="$file"
  else mkdir -p "$BOXES_DIR"; file_to_write="$BOXES_DIR/$DI_NAME.identity"; fi
  if [ -e "$file_to_write" ] && [ "${OPT_FORCE:-0}" != 1 ]; then
    local old; old="$(norm_mac "$(idfile_read "$file_to_write" lan_mac)" 2>/dev/null || true)"
    if [ -n "$old" ] && [ "$old" != "$DI_EXPECTED" ]; then
      refuse "$EX_CONFLICT" "RECORD-DISAGREES" \
        "$file_to_write says lan_mac=$old, but the box at $DI_IP over $DI_IFACE is $DI_OBSERVED" \
        "Not overwriting an existing pin with a different one. If you really moved the rig," \
        "re-run claim with --force (and say so in the log)." 
    fi
  fi
  DI_FILE="$file_to_write"
  DI_EXTRA_HOSTNAME="${OPT_HOSTNAME:-}"; DI_EXTRA_DEVICE_CODE="${OPT_DEVICE_CODE:-}"
  write_record "$DI_FILE"
  say "CLAIMED box=$DI_NAME iface=$DI_IFACE src=$DI_SRC ip=$DI_IP lan_mac=$DI_EXPECTED method=$DI_METHOD"
  say "record written: $DI_FILE"
  say "preflight from now on:  $0 verify --name $DI_NAME || exit \$?"
  emit_json "claimed" "record written" "$EX_OK"
  return "$EX_OK"
}

cmd_verify() {
  resolve_record "${OPT_NAME:-}" "${OPT_FILE:-}"
  DI_NAME="${DI_NAME:-$(idfile_read "$DI_FILE" box)}"
  record_require iface;      DI_IFACE="$REC_VAL"
  record_require src_addr;   DI_SRC="$REC_VAL"
  record_require router_ip;  DI_IP="$REC_VAL"
  record_require lan_mac;    DI_EXPECTED="$REC_VAL"
  local m
  m="$(norm_mac "$DI_EXPECTED")" || refuse "$EX_IDENTITY" "IDENTITY-INCOMPLETE" \
    "the record's lan_mac='$DI_EXPECTED' is not a MAC address ($DI_FILE)"
  DI_EXPECTED="$m"
  local want_hostname; want_hostname="$(idfile_read "$DI_FILE" hostname)"

  # (a) the binding must be usable at all — otherwise every later step is a guess
  iface_up "$DI_IFACE"; case "$?" in
    0) : ;;
    1) refuse "$EX_IFACE" "IFACE-DOWN" "the claimed interface $DI_IFACE is not UP — the pin cannot be honoured" ;;
    *) refuse "$EX_IFACE" "IFACE-MISSING" "the claimed interface $DI_IFACE does not exist on this host" ;;
  esac
  local addrs; addrs="$(iface_addrs "$DI_IFACE")"
  printf '%s\n' "$addrs" | grep -qxF -- "$DI_SRC" || refuse "$EX_IFACE" "SOURCE-NOT-ON-IFACE" \
    "the claimed source address $DI_SRC is not on interface $DI_IFACE (record: $DI_FILE)" \
    "addresses on $DI_IFACE: $(printf '%s' "$addrs" | tr '\n' ' ')" \
    "Without the source address this host cannot force traffic out of $DI_IFACE, so the check would be meaningless."

  # (b) which interface does the kernel actually use, FROM the claimed source?
  local dev; dev="$(route_dev "$DI_IP" "$DI_SRC")"
  if [ -z "$dev" ]; then
    refuse "$EX_IFACE" "ROUTE-UNKNOWN" \
      "cannot determine which interface the kernel would use for $DI_IP from $DI_SRC" \
      "An unprovable binding is a refusal."
  fi
  if [ "$dev" != "$DI_IFACE" ]; then
    DI_OBSERVED="(not read: the address is not on $DI_IFACE at all)"
    refuse "$EX_IFACE" "ADDRESS-ON-A-DIFFERENT-INTERFACE" \
      "$DI_IP is reached out of interface '$dev', NOT the claimed '$DI_IFACE' (from source $DI_SRC)" \
      "This is the 2026-09-28 accident: two routers on the same address. The box you would" \
      "flash is NOT the claimed box. Fix the rig/record before ANY destructive step." \
      "Either claim the box over '$dev' (--iface $dev --src <addr on $dev>) or pin a source that holds $DI_IFACE."
  fi

  # (c) read the MAC over the bound path
  local method="${OPT_METHOD:-neigh}" fallback_used=""
  if ! read_mac "$DI_IFACE" "$DI_SRC" "$DI_IP" "$method"; then
    if [ "$method" = neigh ]; then
      # no neighbour entry: is the box even there? (reachability decides 4 vs 7)
      if ! touch_src "$DI_SRC" "$DI_IP"; then
        refuse "$EX_UNREACHABLE" "UNREACHABLE" \
          "nothing answered at $DI_IP over $DI_IFACE from $DI_SRC (ports tried: $PROBE_PORTS)" \
          "The claimed box is not reachable over the claimed interface/source." \
          "Check the cable, the address on $DI_IFACE, and that the box is powered — never 'just flash it anyway'."
      fi
      if [ "${OPT_SSH_FALLBACK:-0}" = 1 ]; then
        if read_mac "$DI_IFACE" "$DI_SRC" "$DI_IP" ssh; then
          fallback_used="(neigh had no entry; read over ssh -o BindAddress=$DI_SRC)"
        fi
      fi
    fi
    if [ -z "$DI_OBSERVED" ]; then
      if [ "$method" = ssh ]; then
        refuse "$EX_NOMAC" "NO-LAN-MAC-READABLE" \
          "ssh -o BindAddress=$DI_SRC root@$DI_IP cat /sys/class/net/br-lan/address produced no usable MAC" \
          "That is a FAILED identity check, not a pass: a credential the box refuses proves nothing" \
          "about which box it is. Read the LAN MAC another way (or fix the credential) and retry."
      fi
      refuse "$EX_NOMAC" "NO-LAN-MAC-READABLE" \
        "the box at $DI_IP answered over $DI_IFACE but its LAN MAC could not be read (method=$method)" \
        "Cannot compare the box against the record — refusing (an unreadable box is not the pinned box)."
    fi
  fi
  [ -n "$fallback_used" ] && note "NOTE: $fallback_used"

  # (d) the comparison — the whole point
  if [ "$DI_OBSERVED" != "$DI_EXPECTED" ]; then
    refuse "$EX_MISMATCH" "WRONG-DEVICE" \
      "INTERFACE $DI_IFACE (source $DI_SRC) answers $DI_IP with a DIFFERENT BOX." \
      "expected LAN MAC $DI_EXPECTED (record $DI_FILE, box ${DI_NAME:-?})" \
      "observed LAN MAC $DI_OBSERVED" \
      "The 2026-09-28 accident: two routers share 192.168.1.1; flashing here hits the wrong one." \
      "Do NOT flash/install/reboot. Identify which box is really on $DI_IFACE, then re-claim" \
      "(--force) or pick the interface of the box you actually mean."
  fi

  # (e) optional second attribute
  if [ "$CHECK_HOSTNAME" = 1 ]; then
    local want="${want_hostname:-}" got="${DI_SSH_HOSTNAME:-}"
    if [ -z "$want" ]; then
      note "WARNING: --check-hostname asked for, but the record has no hostname= — skipping that attribute"
    elif [ "$method" != ssh ]; then
      note "WARNING: --check-hostname needs --method ssh (it reads the box's hostname over the bound path) — skipped"
    elif [ "$got" != "$want" ]; then
      DI_OBSERVED="$DI_OBSERVED (hostname '$got')"
      refuse "$EX_ATTR" "ATTRIBUTE-MISMATCH" \
        "LAN MAC matches but the hostname does not: expected '$want', observed '$got'" \
        "(over $DI_IFACE from $DI_SRC; record $DI_FILE)"
    fi
  fi

  say "OK box=$DI_NAME iface=$DI_IFACE src=$DI_SRC ip=$DI_IP lan_mac=$DI_EXPECTED method=${DI_METHOD:-neigh} record=$DI_FILE"
  emit_json "ok" "identity verified" "$EX_OK"
  return "$EX_OK"
}

cmd_show() {
  resolve_record "${OPT_NAME:-}" "${OPT_FILE:-}"
  DI_NAME="${DI_NAME:-$(idfile_read "$DI_FILE" box)}"
  say "record: $DI_FILE"
  local k
  for k in box iface src_addr router_ip lan_mac hostname device_code claimed_at claimed_by claimed_via; do
    local v; v="$(idfile_read "$DI_FILE" "$k")"
    [ -n "$v" ] && printf 'device-identity:   %-11s = %s\n' "$k" "$v"
  done
  return 0
}

# ---------------------------------------------------------------- cli

MODE="${1:-}"; shift || true
OPT_NAME=""; OPT_IFACE=""; OPT_SRC=""; OPT_ROUTER_IP=""; OPT_HOSTNAME=""; OPT_DEVICE_CODE=""
OPT_FILE=""; OPT_METHOD="neigh"; OPT_FORCE=0; OPT_SSH_FALLBACK=0
JSON=0; CHECK_HOSTNAME=0

while [ $# -gt 0 ]; do
  case "$1" in
    --name) OPT_NAME="$2"; shift 2 ;;
    --file) OPT_FILE="$2"; shift 2 ;;
    --iface) OPT_IFACE="$2"; shift 2 ;;
    --src|--source) OPT_SRC="$2"; shift 2 ;;
    --router-ip) OPT_ROUTER_IP="$2"; shift 2 ;;
    --hostname) OPT_HOSTNAME="$2"; shift 2 ;;
    --device-code) OPT_DEVICE_CODE="$2"; shift 2 ;;
    --method) OPT_METHOD="$2"; shift 2 ;;
    --ssh-fallback) OPT_SSH_FALLBACK=1; shift ;;
    --check-hostname) CHECK_HOSTNAME=1; shift ;;
    --force) OPT_FORCE=1; shift ;;
    --json) JSON=1; shift ;;
    -h|--help) usage ;;
    *) refuse "$EX_USAGE" "USAGE" "unknown option '$1' (see --help)" ;;
  esac
done

require_tools

case "$MODE" in
  claim)  cmd_claim ;;
  verify) cmd_verify ;;
  show)   cmd_show ;;
  ""|-h|--help|help) usage ;;
  *) usage ;;
esac
