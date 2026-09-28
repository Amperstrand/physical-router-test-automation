#!/usr/bin/env bash
# TEST DOUBLE — a stand-in DESTRUCTIVE STEP (flash / install / sysupgrade) for
# tests/bench-device-identity/run-tests.sh.
#
# Two shapes, and the difference between them IS the control:
#
#   fake-flash.sh …                            (no preflight)
#       reads whichever box answers on the wired NIC and flashes it. This is the
#       2026-09-28 accident verbatim: the Cudy was flashed because nothing asked
#       which box was really there. It prints `FLASHED … lan_mac=<what it hit>`.
#
#   fake-flash.sh --guarded …                  (the documented one-liner)
#       runs the REAL guard first:
#           scripts/bench/device-identity.sh verify --name/--file … || exit $?
#       and only reaches its destructive step if that returned 0.
#
# Running both against the SAME fake rig (same record, same box on the wire) is
# the non-vacuity proof: the unguarded shape must really proceed on the wrong
# box, otherwise the guarded shape proves nothing.
#
# usage: fake-flash.sh [--guarded] [--name BOX | --file RECORD]
#                      [--router-ip IP] [--iface IFACE] [--src ADDR]
set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARD="$(cd "$HARNESS_DIR/../../.." && pwd)/scripts/bench/device-identity.sh"
# test seam: the suite points this at a MUTATED copy of the guard to prove the
# wrong-device comparison is load-bearing (a control that cannot fail is decoration)
[ -n "${DI_GUARD_OVERRIDE:-}" ] && GUARD="${DI_GUARD_OVERRIDE}"

GUARDED=0
NAME=""; FILE=""
ROUTER_IP=192.168.1.1
IFACE=enp0s31f6
SRC=192.168.1.200

while [ $# -gt 0 ]; do
  case "$1" in
    --guarded) GUARDED=1; shift ;;
    --name) NAME="$2"; shift 2 ;;
    --file) FILE="$2"; shift 2 ;;
    --router-ip) ROUTER_IP="$2"; shift 2 ;;
    --iface) IFACE="$2"; shift 2 ;;
    --src) SRC="$2"; shift 2 ;;
    *) echo "fake-flash: unknown option '$1'" >&2; exit 2 ;;
  esac
done

if [ "$GUARDED" = 1 ]; then
  if [ -n "$FILE" ]; then
    "$GUARD" verify --file "$FILE"
  elif [ -n "$NAME" ]; then
    "$GUARD" verify --name "$NAME"
  else
    echo "fake-flash: --guarded needs --name or --file" >&2
    exit 2
  fi
  rc=$?
  if [ "$rc" != 0 ]; then
    echo "fake-flash: PREFLIGHT REFUSED (rc=$rc) — NOT flashing $ROUTER_IP" >&2
    exit "$rc"
  fi
fi

# the destructive step reads the box it is about to write to (over the wired NIC)
curl -s -o /dev/null -m 4 --interface "$SRC" --connect-timeout 4 "http://$ROUTER_IP:2050/" >/dev/null 2>&1 || true
mac="$(ip -4 neigh show dev "$IFACE" 2>/dev/null | sed -n "s/^$ROUTER_IP .* lladdr \([0-9a-fA-F:]\{17\}\).*/\1/p" | head -1)"
if [ -z "$mac" ]; then
  echo "fake-flash: no box answered at $ROUTER_IP on $IFACE — nothing flashed"
  exit 4
fi
echo "FLASHED router_ip=$ROUTER_IP iface=$IFACE src=$SRC lan_mac=$mac"
