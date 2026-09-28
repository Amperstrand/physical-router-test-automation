#!/usr/bin/env bash
# Shared helpers for tests/bench-device-identity/run-tests.sh.
#
# NOTHING here touches a router or a real network. The "rig" is a throw-away
# directory ($DI_FAKE_ROOT) and `ip`, `curl` and `ssh` are PATH doubles in
# harness/bin/. The production guard text runs UNMODIFIED:
#   scripts/bench/device-identity.sh
# The doubles only translate host-side network primitives into that directory:
#
#   ifaces          <name> <UP|DOWN> <addr/len>[,<addr/len>…]
#   routes          <ip> <src|- > <dev>          (which iface reaches <ip>, from <src>)
#   boxmac-<iface>  the br-lan MAC of the box present on that interface
#   ports-<iface>   TCP ports of that box that answer (default "2050 22")
#   hostname-<iface>  what `uci get system.@system[0].hostname` answers there
#   neigh-<iface>   the kernel neighbour table, written by the curl double
#   curl.log / ssh.log  every invocation, so a case can PROVE the traffic was
#                       forced out of the claimed source address
#
# The model is the 2026-09-28 accident: two routers can both answer on
# 192.168.1.1 — one on enp0s31f6, one on a USB dongle — so `boxmac-<iface>` is
# per-interface and the routes file decides which iface a given (ip, src) uses.
#
# Precedent: tests/mt3000-bench/run-tests.sh and tests/offline-install/run-tests.sh
# (production script text + PATH doubles + a fake root, no hardware).
set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HARNESS_DIR/../../.." && pwd)"
GUARD="$REPO_ROOT/scripts/bench/device-identity.sh"
FAKE_FLASH="$HARNESS_DIR/fake-flash.sh"

TESTS_RUN=0; TESTS_FAILED=0; TESTS_SKIPPED=0; TESTS_TIMEOUTS=0
_CUR="(no test)"

t_begin() { _CUR="$1"; TESTS_RUN=$((TESTS_RUN + 1)); printf '\n== %s %s\n' "$(printf '%02d' "$TESTS_RUN")" "$1"; }
pass() { printf '   ok   - %s\n' "$1"; }
fail() { TESTS_FAILED=$((TESTS_FAILED + 1)); printf '   FAIL - %s\n' "$1"; }
skip() { TESTS_SKIPPED=$((TESTS_SKIPPED + 1)); printf '   SKIP - %s\n' "$1"; }

check_rc() {   # $1 desc, $2 expected rc, $3 actual rc
  if [ "$2" = "$3" ]; then pass "$1 (rc=$3)"; else fail "$1: expected rc=$2, got rc=$3"; fi
}

# Literal substring match in pure bash: `printf | grep -q` under `set -o pipefail`
# is a false-FAIL generator (grep exits at the first match and the writer is
# SIGPIPEd) — see tests/mt3000-bench/harness/lib.sh for the measurement.
check_contains() {   # $1 desc, $2 needle, $3 haystack
  case "$3" in
    *"$2"*) pass "$1" ;;
    *)
      fail "$1: output does not contain '$2'"
      printf '        --- output was ---\n%s\n        ------------------\n' "$3"
      ;;
  esac
}

check_not_contains() {   # $1 desc, $2 needle, $3 haystack
  case "$3" in
    *"$2"*)
      fail "$1: output unexpectedly contains '$2'"
      printf '        --- output was ---\n%s\n        ------------------\n' "$3"
      ;;
    *) pass "$1" ;;
  esac
}

check_eq() {   # $1 desc, $2 expected, $3 actual
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1: expected '$2', got '$3'"; fi
}

# Every command is BOUNDED: a case that blocks is a FAIL with a verdict, never a hang.
RUN_CMD_TIMEOUT_DEFAULT=60
run_cmd() {   # OUT=..., RC=...
  local t="${DI_TEST_CMD_TIMEOUT:-$RUN_CMD_TIMEOUT_DEFAULT}"
  if command -v timeout >/dev/null 2>&1; then
    OUT="$(timeout "$t" "$@" 2>&1)"; RC=$?
  else
    OUT="$("$@" 2>&1)"; RC=$?
  fi
  if [ "$RC" = 124 ]; then
    TESTS_TIMEOUTS=$((TESTS_TIMEOUTS + 1))
    OUT="$OUT
[harness] TIMEOUT: '$*' did not finish within ${t}s — counted as a failure, not a hang"
    printf '[harness] TIMEOUT after %ss: %s\n' "$t" "$*" >&2
  fi
}

summary() {
  printf '\n==== %s: tests=%s failed=%s skipped=%s timeouts=%s ====\n' \
    "$(basename "$0")" "$TESTS_RUN" "$TESTS_FAILED" "$TESTS_SKIPPED" "$TESTS_TIMEOUTS"
  [ "$TESTS_FAILED" -eq 0 ] || return 1
  [ "$TESTS_TIMEOUTS" -eq 0 ] || return 1
  return 0
}

# ---------------------------------------------------------------- the fake rig

fake_reset() {   # a clean rig: one wired NIC, the box on it, the USB dongle empty
  rm -rf "$DI_FAKE_ROOT" "$DI_BOXES_DIR"
  mkdir -p "$DI_FAKE_ROOT" "$DI_BOXES_DIR"
  : > "$DI_FAKE_ROOT/curl.log"
  : > "$DI_FAKE_ROOT/ssh.log"
  fake_iface enp0s31f6 UP 192.168.1.200/24
  fake_iface "enx00e04c680001" UP 192.168.7.10/24     # the USB dongle from the incident
  fake_route 192.168.1.1 192.168.1.200 enp0s31f6
  fake_route 192.168.1.1 192.168.7.10  "enx00e04c680001"
  fake_box enp0s31f6 94:83:c4:aa:11:22 "2050 80 22" "GL-MT3000"
  fake_box "enx00e04c680001" 04:ab:18:de:ad:01 "2050 80 22" "Cudy-WR3000"
}

fake_iface() {   # $1=name $2=UP|DOWN $3=addr/len[,addr/len]
  printf '%s %s %s\n' "$1" "$2" "$3" >> "$DI_FAKE_ROOT/ifaces"
}

fake_route() {   # $1=ip $2=src|"-" $3=dev
  printf '%s %s %s\n' "$1" "$2" "$3" >> "$DI_FAKE_ROOT/routes"
}

# Which box is really on an interface (its br-lan MAC). Omitting the MAC removes
# the box from that interface: unreachable there.
fake_box() {   # $1=iface $2=mac|"" [$3=ports] [$4=hostname]
  local iface="$1" mac="$2" ports="${3:-2050 22}" host="${4:-}"
  if [ -z "$mac" ]; then
    rm -f "$DI_FAKE_ROOT/boxmac-$iface" "$DI_FAKE_ROOT/ports-$iface" "$DI_FAKE_ROOT/hostname-$iface"
    rm -f "$DI_FAKE_ROOT/neigh-$iface"
    return 0
  fi
  printf '%s\n' "$mac" > "$DI_FAKE_ROOT/boxmac-$iface"
  printf '%s\n' "$ports" > "$DI_FAKE_ROOT/ports-$iface"
  [ -n "$host" ] && printf '%s\n' "$host" > "$DI_FAKE_ROOT/hostname-$iface"
  rm -f "$DI_FAKE_ROOT/neigh-$iface"
}

# Seed the neighbour table with a stale-but-usable entry (the box was on this
# interface a minute ago). Used to prove the guard still re-reads over the bound
# path rather than trusting what is lying around.
fake_neigh_seed() {   # $1=iface $2=ip $3=mac
  printf '%s dev %s lladdr %s REACHABLE\n' "$2" "$1" "$3" > "$DI_FAKE_ROOT/neigh-$1"
}

fake_neigh_clear() { rm -f "$DI_FAKE_ROOT/neigh-$1"; }
fake_neigh_mac() {   # $1=iface $2=ip -> the MAC the neighbour table holds now
  [ -f "$DI_FAKE_ROOT/neigh-$1" ] || return 1
  sed -n "s/^$2 .* lladdr \([0-9a-fA-F:]\{17\}\).*/\1/p" "$DI_FAKE_ROOT/neigh-$1" | head -1
}
fake_curl_log() { cat "$DI_FAKE_ROOT/curl.log" 2>/dev/null; }
fake_ssh_log() { cat "$DI_FAKE_ROOT/ssh.log" 2>/dev/null; }

# The box the guard/fake-flash would actually hit, as recorded by the doubles.
FAKE_MT3000="94:83:c4:aa:11:22"
FAKE_CUDY="04:ab:18:de:ad:01"

record_write() {   # $1=path $2=box $3=iface $4=src $5=ip $6=mac [$7=hostname]
  mkdir -p "$(dirname "$1")"
  {
    printf '# test record — written by the harness\n'
    printf 'box=%s\n' "$2"
    printf 'iface=%s\n' "$3"
    printf 'src_addr=%s\n' "$4"
    printf 'router_ip=%s\n' "$5"
    printf 'lan_mac=%s\n' "$6"
    [ -n "${7:-}" ] && printf 'hostname=%s\n' "$7"
  } > "$1"
}
