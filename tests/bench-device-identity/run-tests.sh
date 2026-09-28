#!/usr/bin/env bash
# =============================================================================
# tests/bench-device-identity/run-tests.sh — the NO-ROUTER suite for the
# bench device-identity guard (scripts/bench/device-identity.sh).
#
# What it proves, without a router and without a network:
#
#   * claim  writes a record ONLY when the box's LAN MAC could really be read
#            over the interface that was NAMED — and refuses (naming both
#            interfaces) when the address answers out of a different one, when
#            the source address is not on the claimed interface, when nothing
#            answers, and when an existing record for the same box disagrees;
#   * verify PASSES when the box that answers over the claimed source address IS
#            the claimed box, and that the read was forced out of that source
#            address (`curl --interface <src>`, `ssh -o BindAddress=<src>`) —
#            the doubles refuse anything unbound, so an unbound check cannot pass;
#   * verify FAILS CLOSED (exit 3, loud, naming expected + observed MAC and the
#            interface) when a DIFFERENT box answers on that address — the
#            2026-09-28 accident, where the bench host had a GL-MT3000 on
#            enp0s31f6 and a Cudy WR3000 on a USB dongle, both at 192.168.1.1;
#   * verify fails closed on an unreachable address (exit 4), a missing (exit 6),
#            ambiguous (exit 6) or incomplete (exit 6) record, an unreadable MAC
#            (exit 7) and a source/interface that cannot be bound (exit 5);
#   * a STALE neighbour entry is never trusted in either direction (the guard
#            re-reads over the bound path);
#   * THE NON-VACUITY CONTROL: against the SAME fake rig, the unguarded flow
#            really does flash the wrong box (that is the accident, reproduced),
#            while the guarded flow refuses and flashes nothing — plus the
#            positive control that the guarded flow still flashes the RIGHT box.
#            A guard that cannot fail its own control is decoration.
#
# The "rig" is a throw-away directory; `ip`, `curl` and `ssh` are PATH doubles in
# harness/bin/ (see harness/lib.sh for the model). The production guard text runs
# UNMODIFIED, and it is the only thing that decides pass/fail in the cases below.
#
# usage: tests/bench-device-identity/run-tests.sh
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=harness/lib.sh
. "$HERE/harness/lib.sh"

WORK="${DI_TEST_WORKDIR:-$(mktemp -d "${TMPDIR:-/tmp}/bench-device-identity.XXXXXX")}"
mkdir -p "$WORK"
export DI_FAKE_ROOT="$WORK/rig"
export DI_BOXES_DIR="$WORK/boxes"
export PATH="$HERE/harness/bin:$PATH"
# every command is bounded; a hang is a FAIL with a verdict, never a stuck suite
export DI_TEST_CMD_TIMEOUT="${DI_TEST_CMD_TIMEOUT:-60}"
# the guard's production default probe list is "2050 80 22"; the doubles make one
# port enough, and this host can be heavily loaded by other lanes — fewer forks,
# same behaviour under test (the record/refusal assertions name the ports tried).
export DI_PROBE_PORTS="${DI_PROBE_PORTS:-2050}"

# Never, ever let this suite touch a real host interface or a real record: the
# doubles must be the ones on PATH, and the record dir must be inside $WORK.
printf 'bench-device-identity tests: workdir=%s\n' "$WORK"
printf 'guard:   %s\n' "$GUARD"
printf 'doubles: %s\n' "$(command -v ip) | $(command -v curl) | $(command -v ssh)"
printf 'records: %s\n' "$DI_BOXES_DIR"

# =============================================== 0. the suite is hermetic
t_begin "the suite is HERMETIC: PATH doubles only, records inside the workdir"
case "$(command -v ip)" in
  "$HERE/harness/bin/ip") pass "ip resolves to the harness double (no real interface can be touched)" ;;
  *) fail "ip resolves to $(command -v ip) — this suite must never speak to a real host" ;;
esac
case "$(command -v curl)" in
  "$HERE/harness/bin/curl") pass "curl resolves to the harness double" ;;
  *) fail "curl resolves to $(command -v curl)" ;;
esac
case "$(command -v ssh)" in
  "$HERE/harness/bin/ssh") pass "ssh resolves to the harness double (no real router can be reached)" ;;
  *) fail "ssh resolves to $(command -v ssh)" ;;
esac
run_cmd bash -n "$GUARD"
check_rc "the guard is syntactically valid bash" 0 "$RC"

t_begin "the guard's help names the one-line preflight"
run_cmd "$GUARD" --help
check_rc "help exits 2 (usage)" 2 "$RC"
check_contains "help documents the preflight one-liner" 'scripts/bench/device-identity.sh verify --name bench-mt3000 || exit $?' "$OUT"
check_contains "help documents the record path convention" 'scripts/bench/boxes' "$OUT"

# =============================================== 1. claim
t_begin "claim reads the box's LAN MAC over the NAMED interface and writes the record"
fake_reset
run_cmd "$GUARD" claim --name bench-mt3000 --iface enp0s31f6 --src 192.168.1.200
check_rc "claim succeeded" 0 "$RC"
REC="$DI_BOXES_DIR/bench-mt3000.identity"
check_contains "claim names the MAC it read" "lan_mac=$FAKE_MT3000" "$OUT"
check_contains "claim names the interface and source it used" "iface=enp0s31f6 src=192.168.1.200" "$OUT"
check_contains "the record carries the box name" "box=bench-mt3000" "$(cat "$REC" 2>/dev/null)"
check_contains "the record carries the interface" "iface=enp0s31f6" "$(cat "$REC" 2>/dev/null)"
check_contains "the record carries the host source address" "src_addr=192.168.1.200" "$(cat "$REC" 2>/dev/null)"
check_contains "the record carries the LAN MAC" "lan_mac=$FAKE_MT3000" "$(cat "$REC" 2>/dev/null)"
check_contains "the record carries the router address" "router_ip=192.168.1.1" "$(cat "$REC" 2>/dev/null)"
check_contains "the record embeds the preflight one-liner" "verify --name bench-mt3000 || exit" "$(cat "$REC" 2>/dev/null)"
check_contains "the claim traffic was FORCED out of the named source address" "curl --interface 192.168.1.200 http://192.168.1.1:2050/" "$(fake_curl_log)"

t_begin "claim refuses when the address answers on a DIFFERENT interface"
fake_reset
# the kernel would reach 192.168.1.1 from 192.168.1.200 out of the dongle
printf '192.168.1.1 192.168.1.200 enx00e04c680001\n' > "$DI_FAKE_ROOT/routes"
rm -f "$DI_BOXES_DIR"/*.identity
run_cmd "$GUARD" claim --name bench-mt3000 --iface enp0s31f6 --src 192.168.1.200
check_rc "claim refused (exit 5)" 5 "$RC"
check_contains "the refusal names the accident" "ADDRESS-ON-A-DIFFERENT-INTERFACE" "$OUT"
check_contains "the refusal names the interface that would be hit" "enx00e04c680001" "$OUT"
check_contains "the refusal names the claimed interface" "enp0s31f6" "$OUT"
check_contains "the refusal says do not flash" "DO NOT FLASH" "$OUT"
check_eq "and no record was written" "0" "$(ls "$DI_BOXES_DIR"/*.identity 2>/dev/null | wc -l | tr -d ' ')"

t_begin "claim refuses a source address that is not on the claimed interface"
fake_reset
run_cmd "$GUARD" claim --name bench-mt3000 --iface enp0s31f6 --src 192.168.7.10
check_rc "claim refused (exit 5)" 5 "$RC"
check_contains "the refusal names the source/interface mismatch" "SOURCE-NOT-ON-IFACE" "$OUT"
check_contains "the refusal lists the addresses the iface really has" "192.168.1.200" "$OUT"
check_eq "and no record was written" "0" "$(ls "$DI_BOXES_DIR"/*.identity 2>/dev/null | wc -l | tr -d ' ')"

t_begin "claim refuses when nothing answers over the claimed interface/source"
fake_reset
fake_box enp0s31f6 ""           # no box on the wired NIC (the Cable is out / wrong port)
run_cmd "$GUARD" claim --name bench-mt3000 --iface enp0s31f6 --src 192.168.1.200
check_rc "claim refused (exit 4)" 4 "$RC"
check_contains "the refusal says UNREACHABLE" "UNREACHABLE" "$OUT"
check_contains "the refusal names the ports it tried" "ports tried: 2050" "$OUT"
check_eq "and no record was written" "0" "$(ls "$DI_BOXES_DIR"/*.identity 2>/dev/null | wc -l | tr -d ' ')"

t_begin "claim refuses when the box answers but no LAN MAC can be read (exit 7)"
fake_reset
run_cmd env DI_FAKE_NO_NEIGH=1 "$GUARD" claim --name bench-mt3000 --iface enp0s31f6 --src 192.168.1.200
check_rc "claim refused (exit 7)" 7 "$RC"
check_contains "the refusal says the MAC could not be read" "NO-LAN-MAC-READABLE" "$OUT"
check_contains "the refusal says a record without a MAC pins nothing" "pins nothing" "$OUT"
check_eq "and no record was written" "0" "$(ls "$DI_BOXES_DIR"/*.identity 2>/dev/null | wc -l | tr -d ' ')"

t_begin "claim refuses to overwrite a record that names a DIFFERENT box (exit 8), then --force"
fake_reset
mkdir -p "$DI_BOXES_DIR"
record_write "$DI_BOXES_DIR/bench-mt3000.identity" bench-mt3000 enp0s31f6 192.168.1.200 192.168.1.1 "$FAKE_CUDY"
run_cmd "$GUARD" claim --name bench-mt3000 --iface enp0s31f6 --src 192.168.1.200
check_rc "claim refused the conflicting record (exit 8)" 8 "$RC"
check_contains "the refusal names the recorded MAC" "$FAKE_CUDY" "$OUT"
check_contains "the refusal names the observed MAC" "$FAKE_MT3000" "$OUT"
check_contains "the record was left alone" "$FAKE_CUDY" "$(cat "$DI_BOXES_DIR/bench-mt3000.identity")"
run_cmd "$GUARD" claim --name bench-mt3000 --iface enp0s31f6 --src 192.168.1.200 --force
check_rc "--force re-claims (exit 0)" 0 "$RC"
check_contains "the record now names the box that is really there" "$FAKE_MT3000" "$(cat "$DI_BOXES_DIR/bench-mt3000.identity")"

t_begin "claim --method ssh reads the box over the BOUND path and records how"
fake_reset
run_cmd "$GUARD" claim --name bench-mt3000 --iface enp0s31f6 --src 192.168.1.200 --method ssh --hostname GL-MT3000
check_rc "ssh-bound claim succeeded" 0 "$RC"
check_contains "the ssh read used -o BindAddress=<src>" "bind=192.168.1.200 target=root@192.168.1.1" "$(fake_ssh_log)"
check_contains "the record says how the MAC was read" "claimed_via=ssh(bind:192.168.1.200)" "$(cat "$DI_BOXES_DIR/bench-mt3000.identity")"
check_contains "the record keeps the expected hostname" "hostname=GL-MT3000" "$(cat "$DI_BOXES_DIR/bench-mt3000.identity")"

t_begin "the ssh double REFUSES an unbound call (so no case can pass by accident)"
fake_reset
run_cmd ssh -o BatchMode=yes root@192.168.1.1 'cat /sys/class/net/br-lan/address'
check_rc "an ssh without BindAddress is refused (exit 255)" 255 "$RC"
check_contains "the double says why" "either router could answer" "$OUT"

# =============================================== 2. verify — the happy path
t_begin "verify PASSES when the box that answers IS the claimed box"
fake_reset
mkdir -p "$DI_BOXES_DIR"
record_write "$DI_BOXES_DIR/bench-mt3000.identity" bench-mt3000 enp0s31f6 192.168.1.200 192.168.1.1 "$FAKE_MT3000" GL-MT3000
run_cmd "$GUARD" verify --name bench-mt3000
check_rc "verify passed" 0 "$RC"
check_contains "verify reports the identity it proved" "OK box=bench-mt3000 iface=enp0s31f6 src=192.168.1.200 ip=192.168.1.1 lan_mac=$FAKE_MT3000" "$OUT"
check_contains "the read was forced out of the claimed source address" "curl --interface 192.168.1.200 http://192.168.1.1:2050/" "$(fake_curl_log)"

t_begin "verify never trusts a STALE neighbour entry (it re-reads over the bound path)"
fake_reset
mkdir -p "$DI_BOXES_DIR"
record_write "$DI_BOXES_DIR/bench-mt3000.identity" bench-mt3000 enp0s31f6 192.168.1.200 192.168.1.1 "$FAKE_MT3000"
fake_neigh_seed enp0s31f6 192.168.1.1 "$FAKE_CUDY"      # stale: says the Cudy is there
run_cmd "$GUARD" verify --name bench-mt3000
check_rc "verify passes on the real box despite the stale entry" 0 "$RC"
check_eq "the neighbour entry was replaced by the bound read" "$FAKE_MT3000" "$(fake_neigh_mac enp0s31f6 192.168.1.1)"
fake_neigh_seed enp0s31f6 192.168.1.1 "$FAKE_MT3000"     # stale the OTHER way: says the MT3000
fake_box enp0s31f6 "$FAKE_CUDY" "" "Cudy-WR3000"        # the Cudy really is on the wire now
run_cmd "$GUARD" verify --name bench-mt3000
check_rc "verify still fails closed (exit 3): a stale entry is not a pass" 3 "$RC"
check_eq "and the entry now shows what is really there" "$FAKE_CUDY" "$(fake_neigh_mac enp0s31f6 192.168.1.1)"

# =============================================== 3. verify — FAIL CLOSED
t_begin "verify FAILS CLOSED when the WRONG box answers (the 2026-09-28 accident)"
fake_reset
mkdir -p "$DI_BOXES_DIR"
# the record pins the MT3000 … but the Cudy WR3000 is what is on enp0s31f6 now
record_write "$DI_BOXES_DIR/bench-mt3000.identity" bench-mt3000 enp0s31f6 192.168.1.200 192.168.1.1 "$FAKE_MT3000" GL-MT3000
fake_box enp0s31f6 "$FAKE_CUDY" "" "Cudy-WR3000"
run_cmd "$GUARD" verify --name bench-mt3000
WRONG_RC="$RC"; WRONG_OUT="$OUT"
check_rc "verify refused (exit 3)" 3 "$RC"
check_contains "the refusal is a WRONG-DEVICE refusal" "WRONG-DEVICE" "$OUT"
check_contains "the refusal names the OBSERVED MAC" "$FAKE_CUDY" "$OUT"
check_contains "the refusal names the EXPECTED MAC" "$FAKE_MT3000" "$OUT"
check_contains "the refusal names the interface" "enp0s31f6" "$OUT"
check_contains "the refusal names the host source address" "192.168.1.200" "$OUT"
check_contains "the refusal repeats the accident it came from" "2026-09-28" "$OUT"
check_contains "the refusal forbids the destructive step" "DO NOT FLASH, INSTALL, REBOOT OR sysupgrade" "$OUT"
check_not_contains "no pass line was printed" "OK box=" "$OUT"
printf '\n   ---- RAW wrong-device refusal (exit %s) ----\n%s\n   -------------------------------------------\n' "$WRONG_RC" "$WRONG_OUT"

t_begin "verify's --json verdict is machine-readable on the wrong-device case"
fake_reset
mkdir -p "$DI_BOXES_DIR"
record_write "$DI_BOXES_DIR/bench-mt3000.identity" bench-mt3000 enp0s31f6 192.168.1.200 192.168.1.1 "$FAKE_MT3000"
fake_box enp0s31f6 "$FAKE_CUDY" "" "Cudy-WR3000"
run_cmd "$GUARD" verify --name bench-mt3000 --json
check_rc "verify refused (exit 3)" 3 "$RC"
JSON_LINE="$(printf '%s\n' "$OUT" | grep '^{' | head -1)"
check_contains "the verdict names the status" '"status":"WRONG-DEVICE"' "$JSON_LINE"
check_contains "the verdict carries the exit code" '"exit":3' "$JSON_LINE"
check_contains "the verdict carries the expected MAC" "\"expected_mac\":\"$FAKE_MT3000\"" "$JSON_LINE"
check_contains "the verdict carries the observed MAC" "\"observed_mac\":\"$FAKE_CUDY\"" "$JSON_LINE"
check_contains "the verdict carries the interface" '"iface":"enp0s31f6"' "$JSON_LINE"
printf '   raw verdict json: %s\n' "$JSON_LINE"

t_begin "verify fails closed when the address is UNREACHABLE over the claimed path"
fake_reset
mkdir -p "$DI_BOXES_DIR"
record_write "$DI_BOXES_DIR/bench-mt3000.identity" bench-mt3000 enp0s31f6 192.168.1.200 192.168.1.1 "$FAKE_MT3000"
fake_box enp0s31f6 ""          # cable unplugged / box powered off
run_cmd "$GUARD" verify --name bench-mt3000
check_rc "verify refused (exit 4)" 4 "$RC"
check_contains "the refusal says UNREACHABLE" "UNREACHABLE" "$OUT"
check_contains "the refusal forbids 'flash it anyway'" "never 'just flash it anyway'" "$OUT"

t_begin "verify fails closed when the address answers on a DIFFERENT interface"
fake_reset
mkdir -p "$DI_BOXES_DIR"
record_write "$DI_BOXES_DIR/bench-mt3000.identity" bench-mt3000 enp0s31f6 192.168.1.200 192.168.1.1 "$FAKE_MT3000"
printf '192.168.1.1 192.168.1.200 enx00e04c680001\n' > "$DI_FAKE_ROOT/routes"
run_cmd "$GUARD" verify --name bench-mt3000
check_rc "verify refused (exit 5)" 5 "$RC"
check_contains "the refusal names the interface actually used" "enx00e04c680001" "$OUT"
check_contains "the refusal names the claimed interface" "enp0s31f6" "$OUT"
check_contains "the refusal is the wrong-interface refusal" "ADDRESS-ON-A-DIFFERENT-INTERFACE" "$OUT"
check_not_contains "and it read no MAC over the wrong interface" "observed LAN MAC : 04:ab" "$OUT"

t_begin "verify fails closed when the claimed source address is not on the interface"
fake_reset
mkdir -p "$DI_BOXES_DIR"
record_write "$DI_BOXES_DIR/bench-mt3000.identity" bench-mt3000 enp0s31f6 192.168.7.10 192.168.1.1 "$FAKE_MT3000"
run_cmd "$GUARD" verify --name bench-mt3000
check_rc "verify refused (exit 5)" 5 "$RC"
check_contains "the refusal says the pin cannot be honoured" "SOURCE-NOT-ON-IFACE" "$OUT"
check_contains "the refusal says why that matters" "would be meaningless" "$OUT"

t_begin "verify fails closed on a MISSING record, and names the claim command"
fake_reset
run_cmd "$GUARD" verify --name bench-mt3000
check_rc "verify refused (exit 6)" 6 "$RC"
check_contains "the refusal says there is no record" "IDENTITY-MISSING" "$OUT"
check_contains "the refusal gives the claim command" "claim --name bench-mt3000 --iface" "$OUT"
check_contains "the refusal says never flash with an unknown identity" "while the target identity is unknown" "$OUT"
run_cmd "$GUARD" verify --file "$WORK/nope.identity"
check_rc "verify refused for an absent --file (exit 6)" 6 "$RC"
check_contains "the refusal names the path" "IDENTITY-FILE-MISSING" "$OUT"

t_begin "verify fails closed on an AMBIGUOUS record (two records claim the same box)"
fake_reset
mkdir -p "$DI_BOXES_DIR"
record_write "$DI_BOXES_DIR/bench-mt3000.identity" bench-mt3000 enp0s31f6 192.168.1.200 192.168.1.1 "$FAKE_MT3000"
record_write "$DI_BOXES_DIR/stale-copy.identity" bench-mt3000 "enx00e04c680001" 192.168.7.10 192.168.1.1 "$FAKE_CUDY"
run_cmd "$GUARD" verify --name bench-mt3000
check_rc "verify refused (exit 6)" 6 "$RC"
check_contains "the refusal says the record is AMBIGUOUS" "IDENTITY-AMBIGUOUS" "$OUT"
check_contains "the refusal names the first record" "bench-mt3000.identity" "$OUT"
check_contains "the refusal names the second record" "stale-copy.identity" "$OUT"

t_begin "verify fails closed on an INCOMPLETE record (no LAN MAC)"
fake_reset
mkdir -p "$DI_BOXES_DIR"
{
  printf '# half a record\nbox=bench-mt3000\niface=enp0s31f6\nsrc_addr=192.168.1.200\nrouter_ip=192.168.1.1\n'
} > "$DI_BOXES_DIR/bench-mt3000.identity"
run_cmd "$GUARD" verify --name bench-mt3000
check_rc "verify refused (exit 6)" 6 "$RC"
check_contains "the refusal names the missing key" "has no 'lan_mac='" "$OUT"
check_contains "the refusal says a MAC-less record is not an identity" "not an identity" "$OUT"

t_begin "verify fails closed when a bound ssh read yields no usable MAC (exit 7)"
fake_reset
mkdir -p "$DI_BOXES_DIR"
record_write "$DI_BOXES_DIR/bench-mt3000.identity" bench-mt3000 enp0s31f6 192.168.1.200 192.168.1.1 "$FAKE_MT3000"
fake_box enp0s31f6 "" "" ""      # nothing on the wire: the bound ssh cannot read a box
run_cmd "$GUARD" verify --name bench-mt3000 --method ssh
check_rc "verify refused (exit 7)" 7 "$RC"
check_contains "the refusal says a refused credential proves nothing" "proves nothing" "$OUT"

t_begin "verify --method ssh binds the source and compares over it"
fake_reset
mkdir -p "$DI_BOXES_DIR"
record_write "$DI_BOXES_DIR/bench-mt3000.identity" bench-mt3000 enp0s31f6 192.168.1.200 192.168.1.1 "$FAKE_MT3000" GL-MT3000
run_cmd "$GUARD" verify --name bench-mt3000 --method ssh --check-hostname
check_rc "the ssh-bound verify passed" 0 "$RC"
check_contains "the ssh read carried BindAddress" "bind=192.168.1.200 target=root@192.168.1.1" "$(fake_ssh_log)"
check_contains "the pass line says which method proved it" "method=ssh(bind:192.168.1.200)" "$OUT"
fake_box enp0s31f6 "$FAKE_CUDY" "" "Cudy-WR3000"
run_cmd "$GUARD" verify --name bench-mt3000 --method ssh
check_rc "the ssh-bound verify refuses the wrong box (exit 3)" 3 "$RC"
check_contains "the refusal still names both MACs" "$FAKE_CUDY" "$OUT"

t_begin "verify --check-hostname catches a matching MAC with a different hostname (exit 9)"
fake_reset
mkdir -p "$DI_BOXES_DIR"
record_write "$DI_BOXES_DIR/bench-mt3000.identity" bench-mt3000 enp0s31f6 192.168.1.200 192.168.1.1 "$FAKE_MT3000" GL-MT3000
printf 'Cudy-WR3000\n' > "$DI_FAKE_ROOT/hostname-enp0s31f6"    # same MAC view, wrong name
run_cmd "$GUARD" verify --name bench-mt3000 --method ssh --check-hostname
check_rc "verify refused (exit 9)" 9 "$RC"
check_contains "the refusal is an ATTRIBUTE-MISMATCH" "ATTRIBUTE-MISMATCH" "$OUT"
check_contains "the refusal names the expected hostname" "expected 'GL-MT3000'" "$OUT"
check_contains "the refusal names the observed hostname" "observed 'Cudy-WR3000'" "$OUT"

# =============================================== 4. THE NON-VACUITY CONTROL
t_begin "NON-VACUITY: unguarded vs guarded against the SAME rig (the accident, reproduced)"
fake_reset
mkdir -p "$DI_BOXES_DIR"
# the record pins the GL-MT3000; the Cudy WR3000 is what enp0s31f6 really reaches
record_write "$DI_BOXES_DIR/bench-mt3000.identity" bench-mt3000 enp0s31f6 192.168.1.200 192.168.1.1 "$FAKE_MT3000" GL-MT3000
fake_box enp0s31f6 "$FAKE_CUDY" "" "Cudy-WR3000"

# --- (A) the UNGUARDED flow: same fake, no preflight. This MUST proceed, or the
#         control proves nothing.
run_cmd "$FAKE_FLASH" --name bench-mt3000
UNG_RC="$RC"; UNG_OUT="$OUT"
check_rc "the unguarded flow ran and exited 0 (it does NOT check)" 0 "$RC"
check_contains "…and it FLASHED a box" "FLASHED router_ip=192.168.1.1" "$UNG_OUT"
check_contains "…and the box it flashed is the WRONG one (the Cudy)" "lan_mac=$FAKE_CUDY" "$UNG_OUT"
check_not_contains "…and nothing stopped it" "REFUSED" "$UNG_OUT"

# --- (B) the GUARDED flow: same record, same fake, same box on the wire.
run_cmd "$FAKE_FLASH" --guarded --name bench-mt3000
GRD_RC="$RC"; GRD_OUT="$OUT"
check_rc "the guarded flow refused (exit 3, the guard's code passes through)" 3 "$RC"
check_contains "it says the preflight refused" "PREFLIGHT REFUSED (rc=3)" "$GRD_OUT"
check_contains "the guard's WRONG-DEVICE refusal is in the output" "WRONG-DEVICE" "$GRD_OUT"
check_not_contains "NOTHING was flashed" "FLASHED" "$GRD_OUT"

check_eq "A and B ran against byte-identical rig state (same box on the wire)" "$FAKE_CUDY" "$(fake_neigh_mac enp0s31f6 192.168.1.1)"
printf '\n   ---- RAW non-vacuity control ----\n   [A] unguarded (no preflight) rc=%s:\n%s\n   [B] guarded (one-line preflight) rc=%s:\n%s\n   ---------------------------------\n' \
  "$UNG_RC" "$UNG_OUT" "$GRD_RC" "$GRD_OUT"

t_begin "MUTATION CONTROL: disable the MAC comparison and the wrong box IS flashed"
# The non-vacuity pair above shows the guarded flow refusing. This case shows the
# refusal is the COMPARISON doing the work: mutate exactly that line out of a copy
# of the guard and the same rig flashes the wrong box. If the mutant still refused,
# the suite's wrong-device expectations would be passing for some other reason.
fake_reset
mkdir -p "$DI_BOXES_DIR"
record_write "$DI_BOXES_DIR/bench-mt3000.identity" bench-mt3000 enp0s31f6 192.168.1.200 192.168.1.1 "$FAKE_MT3000" GL-MT3000
fake_box enp0s31f6 "$FAKE_CUDY" "" "Cudy-WR3000"
MUTANT="$WORK/mutant-device-identity.sh"
sed 's/^  if \[ "\$DI_OBSERVED" != "\$DI_EXPECTED" \]; then$/  if false; then/' "$GUARD" > "$MUTANT"
chmod +x "$MUTANT"
check_contains "the mutation landed (the comparison is gone)" 'if false; then' "$(cat "$MUTANT")"
run_cmd bash -n "$MUTANT"
check_rc "the mutant is still valid bash" 0 "$RC"
run_cmd env DI_GUARD_OVERRIDE="$MUTANT" "$FAKE_FLASH" --guarded --name bench-mt3000
check_rc "the MUTANT flow proceeds (exit 0)" 0 "$RC"
check_contains "…and it flashes the wrong box — so the comparison is what stops it" "lan_mac=$FAKE_CUDY" "$OUT"
printf '   raw mutant flow: %s\n' "$(printf '%s' "$OUT" | tr '\n' '|')"

t_begin "positive control: the guarded flow still flashes the RIGHT box (not always-refuse)"
fake_reset
mkdir -p "$DI_BOXES_DIR"
record_write "$DI_BOXES_DIR/bench-mt3000.identity" bench-mt3000 enp0s31f6 192.168.1.200 192.168.1.1 "$FAKE_MT3000" GL-MT3000
run_cmd "$FAKE_FLASH" --guarded --name bench-mt3000
check_rc "the guarded flow exited 0" 0 "$RC"
check_contains "and it flashed the pinned box" "FLASHED router_ip=192.168.1.1 iface=enp0s31f6 src=192.168.1.200 lan_mac=$FAKE_MT3000" "$OUT"

t_begin "the same claim/verify pair works after the rig MOVES to the other interface"
fake_reset
mkdir -p "$DI_BOXES_DIR"
# the operator re-cables: the Cudy is now the box on the dongle, the MT3000 stays wired
run_cmd "$GUARD" claim --name cudy-dongle --iface enx00e04c680001 --src 192.168.7.10
check_rc "claim on the dongle succeeded" 0 "$RC"
check_contains "the dongle record carries the dongle's box" "lan_mac=$FAKE_CUDY" "$OUT"
run_cmd "$GUARD" verify --name cudy-dongle
check_rc "verify on the dongle passed" 0 "$RC"
check_contains "the dongle verify bound the dongle's source" "curl --interface 192.168.7.10 http://192.168.1.1:2050/" "$(fake_curl_log)"
run_cmd "$GUARD" verify --name cudy-dongle --method neigh
check_contains "both records co-exist and neither needs the other" "OK box=cudy-dongle" "$OUT"

t_begin "usage: unknown mode and unknown option are refusals (exit 2)"
run_cmd "$GUARD" frobnicate
check_rc "an unknown mode refuses (exit 2)" 2 "$RC"
run_cmd "$GUARD" verify --name bench-mt3000 --wat
check_rc "an unknown option refuses (exit 2)" 2 "$RC"
check_contains "the refusal points at --help" "see --help" "$OUT"

summary
