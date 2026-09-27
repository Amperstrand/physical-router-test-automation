#!/usr/bin/env bash
# rig-flash.sh — AP3915i rig flash bring-up (PRTA dual-router rig).
#
# Lanes:
#   in-place     <unit> [version]  sysupgrade -n over SSH (default lane)
#   verify       <unit> [version]  post-flash verification only
#   from-scratch <unit> [version]  RECOVERY ONLY: PoE cycle + U-Boot TFTP boot
#                                 + sysupgrade (needs explicit --confirm)
#   download     [version]         fetch + sha256-verify images into IMAGE_DIR
#
# Units: alpha (router-alpha, stock GS1900 #2 port 5, 192.168.105.51)
#        beta  (router-beta,  stock GS1900 #2 port 2, 192.168.103.51)
#
# Version policy: tollgate-os publish-os.yml pins OpenWrt 24.10.4 for its
# device matrix, but the bench golden per user directive is 24.10.8
# (latest 24.10.x, verified available for ipq40xx/ap3915i). Rollback image:
# 24.10.2 (the pre-flash version), kept in IMAGE_DIR.
#
# Safety canon (~/conwrt-bench/docs/BENCH-CANON.md):
#   - DHCP-disabled overlay is MANDATORY: the APs sit flat on the 192.168.13.0/24
#     lab L2 via the stock switch; a default-config OpenWrt AP would be a rogue
#     DHCP server (see BENCH-CANON "Never firstboot / reflash onto a dirty or
#     shared L2").
#   - Verify after write: image sha256 is re-checked ON DEVICE before sysupgrade;
#     post-flash state is re-read (version, keys, dhcp.ignore, hostname).
#   - Doors: each unit has SSH (key) + U-Boot TFTP fallback
#     (bootcmd=run boot_openwrt; run boot_net, verified in CFG1 on both units);
#     beta additionally has the serial splice (ap-lan2 listener, :4003).
#
# See docs/rig-flash.md for the full runbook, rollback and 25.12 notes.

set -euo pipefail
trap 'rm -rf "${overlay_dir:-}" 2>/dev/null || true' EXIT

readonly IMAGE_DIR="${RIG_IMAGE_DIR:-$HOME/rig-flash-images}"
readonly SOPS_KEY="${SOPS_AGE_KEY_FILE:-$HOME/.config/age/keys.txt}"
readonly SECRETS="${RIG_SECRETS:-$HOME/conwrt-bench/secrets/secrets.json}"
readonly VENVS_PY="$HOME/venvs/rig-labgrid/bin/python"

# Pinned sha256 (downloads.openwrt.org targets/ipq40xx/generic sha256sums).
declare -A PINNED_SYSUPGRADE=(
  [24.10.8]="30bf9601523f2c850c35901dd8dd1974c5654c86dabea09f60f4bfcd0f4dacca"
  [24.10.2]="38ca385660e46aa084017b80e620ab07fb30716a07bcaab8f3d5435bc88bf848"
)
declare -A PINNED_INITRAMFS=(
  [24.10.8]="29e7ee7d33ab83a1af98f89c17c0a896acb570714de548877d1733cbde2f93f2"
)

# GS1900-8HP #1 (OpenWrt lab switch) image pins — realtek/rtl838x.
readonly SW_BOARD="zyxel,gs1900-8hp-a1"
declare -A PINNED_SW_SYSUPGRADE=(
  [25.12.5]="$(grep 'gs1900-8hp-a1-squashfs-sysupgrade' "$HOME/rig-flash-images/sha256sums-25.12.5-rtl838x" 2>/dev/null | cut -d' ' -f1)"
  [25.12.1]="$(grep 'gs1900-8hp-a1-squashfs-sysupgrade' "$HOME/rig-flash-images/sha256sums-25.12.1-rtl838x" 2>/dev/null | cut -d' ' -f1)"
)
sw_image_name() { echo "openwrt-$1-realtek-rtl838x-zyxel_gs1900-8hp-a1-squashfs-sysupgrade.bin"; }

unit_ip()   { case "$1" in alpha) echo 192.168.105.51;; beta) echo 192.168.103.51;; *) return 1;; esac; }
unit_port() { case "$1" in alpha) echo 5;; beta) echo 2;; *) return 1;; esac; }
# U-Boot env ipaddr= (TFTP address, from CFG1; differs per unit).
unit_bootip() { case "$1" in alpha) echo 192.168.1.1;; beta) echo 192.168.1.11;; *) return 1;; esac; }
unit_hostname() { case "$1" in alpha) echo router-alpha;; beta) echo router-beta;; *) return 1;; esac; }

image_name() { echo "openwrt-$1-ipq40xx-generic-extreme-networks_ws-ap3915i-squashfs-sysupgrade.bin"; }
initramfs_name() { echo "openwrt-$1-ipq40xx-generic-extreme-networks_ws-ap3915i-initramfs-uImage.itb"; }

fleet_password() {
  [ -r "$SECRETS" ] || { echo "secrets file not readable: $SECRETS" >&2; exit 1; }
  SOPS_AGE_KEY_FILE="$SOPS_KEY" sops -d "$SECRETS" | python3 -c 'import json,sys; print(json.load(sys.stdin)["fleet"]["bench_root_password"])'
}

ssh_unit() {
  local ip; ip="$(unit_ip "$1")"; shift
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "root@$ip" "$@"
}

verify_local_image() {
  local version="$1" img sum
  img="$IMAGE_DIR/$(image_name "$version")"
  sum="${PINNED_SYSUPGRADE[$version]:-}"
  [ -n "$sum" ] || { echo "no pinned sha256 for $version" >&2; exit 1; }
  [ -f "$img" ] || { echo "missing image: $img (run: $0 download $version)" >&2; exit 1; }
  echo "$sum  $img" | sha256sum -c - >/dev/null 2>&1 || { echo "sha256 MISMATCH for $img" >&2; exit 1; }
  echo "local image verified: $(basename "$img") ($sum)"
}

cmd_download() {
  local version="${1:-24.10.8}"
  mkdir -p "$IMAGE_DIR"
  local base="https://downloads.openwrt.org/releases/$version/targets/ipq40xx/generic"
  local -a files=("$(image_name "$version")")
  # initramfs pinned only for the golden + recovery version set
  [ -n "${PINNED_INITRAMFS[$version]:-}" ] && files+=("$(initramfs_name "$version")")
  local f
  for f in "${files[@]}"; do
    [ -s "$IMAGE_DIR/$f" ] || curl -sfL -o "$IMAGE_DIR/$f" "$base/$f"
  done
  # authoritative sums from upstream (informational; pin above is the gate)
  curl -sfL -o "$IMAGE_DIR/sha256sums-$version" "$base/sha256sums" || true
  verify_local_image "$version"
  [ -n "${PINNED_INITRAMFS[$version]:-}" ] && {
    echo "${PINNED_INITRAMFS[$version]}  $IMAGE_DIR/$(initramfs_name "$version")" | sha256sum -c -
  }
}

build_overlay() {
  # build_overlay <unit> <tmpdir> — writes overlay tarball adopting the unit:
  # static mgmt IP, DHCP disabled, bench keys preserved (device keys + host
  # id_ed25519), root password set on first boot via uci-defaults.
  local unit="$1" out="$2" ip hostname pw
  ip="$(unit_ip "$unit")"; hostname="$(unit_hostname "$unit")"
  pw="$(fleet_password)"

  mkdir -p "$out/etc/config" "$out/etc/dropbear" "$out/etc/uci-defaults"
  chmod 755 "$out/etc" "$out/etc/config" "$out/etc/dropbear" "$out/etc/uci-defaults"

  cat > "$out/etc/config/network" <<EOF
config interface 'loopback'
	option device 'lo'
	option proto 'static'
	option ipaddr '127.0.0.1'
	option netmask '255.0.0.0'

config globals 'globals'

config device
	option name 'br-lan'
	option type 'bridge'
	list ports 'lan'

config interface 'lan'
	option device 'br-lan'
	option proto 'static'
	list ipaddr '$ip/24'
	option ip6assign '60'
EOF

  # DHCP server OFF everywhere (rogue-DHCP guard while flat on the lab L2).
  cat > "$out/etc/config/dhcp" <<'EOF'
config dnsmasq
	option domainneeded '1'
	option boguspriv '1'
	option localise_queries '1'
	option rebind_protection '1'
	option rebind_localhost '1'
	option local '/lan/'
	option domain 'lan'
	option expandhosts '1'
	option authoritative '1'
	option readethers '1'
	option leasefile '/tmp/dhcp.leases'
	option resolvfile '/tmp/resolv.conf.d/resolv.conf.auto'
	option nonwildcard '1'
	option ednspacket_max '1232'

config dhcp 'lan'
	option interface 'lan'
	option ignore '1'

config dhcp 'wan'
	option interface 'wan'
	option ignore '1'
EOF

  # Keys: whatever the unit already trusts (pre-flash backup) + host key.
  {
    ssh_unit "$unit" 'cat /etc/dropbear/authorized_keys 2>/dev/null' || true
    cat "$HOME/.ssh/id_ed25519.pub"
  } | sort -u > "$out/etc/dropbear/authorized_keys"
  chmod 600 "$out/etc/dropbear/authorized_keys"
  [ -s "$out/etc/dropbear/authorized_keys" ] || { echo "refusing: empty authorized_keys union" >&2; exit 1; }

  # First-boot adoption: hostname + root password (fleet). BusyBox has no
  # chpasswd; printf-to-passwd is the documented pattern. Script must always
  # reach exit 0 (uci-defaults contract).
  cat > "$out/etc/uci-defaults/99-rig-adopt" <<EOF
#!/bin/sh
uci set system.@system[0].hostname='$hostname'
uci commit system
printf '%s\n%s\n' '$pw' '$pw' | passwd root >/dev/null 2>&1
echo "rig-adopt $hostname \$(date -u +%FT%TZ)" > /etc/rig-adopted
exit 0
EOF
  chmod 755 "$out/etc/uci-defaults/99-rig-adopt"

  tar --owner=0 --group=0 --numeric-owner -czf "$out/../overlay-$unit.tar.gz" -C "$out" .
  echo "$out/../overlay-$unit.tar.gz"
}

backup_unit_config() {
  local unit="$1" ts dest
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  dest="$IMAGE_DIR/backup-$unit-$ts.tar.gz"
  ssh_unit "$unit" 'tar -C / -czf - etc/config etc/dropbear 2>/dev/null' > "$dest"
  [ -s "$dest" ] || { echo "config backup failed for $unit" >&2; exit 1; }
  echo "$dest"
}

wait_ssh_and_verify() {
  local unit="$1" version="$2" ip deadline board
  ip="$(unit_ip "$unit")"
  echo "waiting for $unit ($ip) to come back (up to 420s)..."
  deadline=$((SECONDS + 420))
  while [ $SECONDS -lt $deadline ]; do
    if board="$(ssh -o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new "root@$ip" 'ubus call system board' 2>/dev/null)"; then
      break
    fi
    sleep 5
  done
  [ -n "${board:-}" ] || { echo "FAIL: SSH never returned for $unit" >&2; exit 1; }
  verify_unit "$unit" "$version"
}

verify_unit() {
  local unit="$1" version="$2" ip fail=0 out
  ip="$(unit_ip "$unit")"
  echo "--- verify $unit @ $ip (expect $version) ---"
  # shellcheck disable=SC2016 — $(...) must expand on the device, not locally
  out="$(ssh_unit "$unit" 'ubus call system board; echo KEYCOUNT=$(wc -l < /etc/dropbear/authorized_keys); echo DHCP_LAN_IGNORE=$(uci get dhcp.lan.ignore 2>/dev/null || echo unset); echo HOSTNAME=$(uci get system.@system[0].hostname); cat /etc/rig-adopted 2>/dev/null || true')"
  echo "$out" | grep -q "\"version\": \"$version\"" || { echo "FAIL: version != $version"; echo "$out" | grep -E 'version|release' ; fail=1; }
  echo "$out" | grep -q "HOSTNAME=$(unit_hostname "$unit")" || { echo "FAIL: hostname"; fail=1; }
  echo "$out" | grep -q "DHCP_LAN_IGNORE=1" || { echo "FAIL: dhcp.lan.ignore != 1 (rogue-DHCP guard)"; fail=1; }
  local keys; keys="$(echo "$out" | sed -n 's/^KEYCOUNT=//p')"
  [ "${keys:-0}" -ge 2 ] || { echo "FAIL: authorized_keys count=$keys (<2)"; fail=1; }
  echo "$out" | grep -q "rig-adopt $(unit_hostname "$unit")" || { echo "FAIL: rig-adopt marker missing"; fail=1; }
  if [ $fail -eq 0 ]; then
    echo "PASS: $unit golden $version adopted (hostname/keys/dhcp-off verified)"
    ssh_unit "$unit" 'ubus call system board' | python3 -c 'import json,sys; b=json.load(sys.stdin); print(b["release"]["description"], "|", b["model"])'
  else
    exit 1
  fi
}

cmd_in_place() {
  local unit="$1" version="${2:-24.10.8}" ip img overlay_dir backup
  ip="$(unit_ip "$unit")"; img="$IMAGE_DIR/$(image_name "$version")"
  verify_local_image "$version"

  echo "=== in-place flash: $unit @ $ip -> $version ==="
  echo "--- preflight (doors + resources) ---"
  ssh_unit "$unit" 'ubus call system board | grep -E "\"version\"|board_name"; \
    dd if=/dev/mtd0 2>/dev/null | strings | grep -E "^bootcmd=" || echo "WARN: bootcmd not found (no TFTP fallback door)"; \
    df -h /tmp | tail -1; free | head -2'

  backup="$(backup_unit_config "$unit")"
  echo "config backup: $backup"

  overlay_dir="$(mktemp -d)"
  trap 'rm -rf "$overlay_dir"' EXIT
  local overlay; overlay="$(build_overlay "$unit" "$overlay_dir/ovl")"
  echo "overlay: $overlay"

  echo "--- transfer ---"
  scp -O -o BatchMode=yes "$img" "$overlay" "root@$ip:/tmp/"
  local base; base="$(basename "$img")"

  echo "--- on-device verify (sha256) ---"
  ssh_unit "$unit" "sha256sum /tmp/$base" | grep -q "${PINNED_SYSUPGRADE[$version]}" \
    || { echo "FAIL: on-device sha256 mismatch"; exit 1; }

  echo "--- sysupgrade -n (clean config; ~2-4 min) ---"
  # Connection drop mid-upgrade exits 255 — expected. sysupgrade -n also
  # regenerates dropbear host keys, so the stale known_hosts entry must go
  # before the wait loop can reconnect.
  ssh_unit "$unit" "sysupgrade -n -f /tmp/overlay-$unit.tar.gz /tmp/$base" || true
  ssh-keygen -f "$HOME/.ssh/known_hosts" -R "$ip" >/dev/null 2>&1 || true

  wait_ssh_and_verify "$unit" "$version"
}

cmd_from_scratch() {
  # RECOVERY lane — only when a unit will not boot OpenWrt from flash.
  # Path: (optionally fix bootcmd on stock WiNG via legacy-kex SSH) ->
  # TFTP-serve initramfs from this host (192.168.1.2 = U-Boot serverip env) ->
  # StockWeb PoE power cycle on the stock switch -> U-Boot TFTP boot ->
  # sysupgrade -n with the same overlay. Requires --confirm.
  local unit="${1:-}" version="${2:-24.10.8}"
  [ "${3:-}" = "--confirm" ] || { echo "from-scratch is recovery-only: pass --confirm (see docs/rig-flash.md)"; exit 2; }
  local port ip bootip img initramfs
  port="$(unit_port "$unit")"; ip="$(unit_ip "$unit")"; bootip="$(unit_bootip "$unit")"
  img="$IMAGE_DIR/$(image_name "$version")"; initramfs="$IMAGE_DIR/$(initramfs_name "$version")"
  verify_local_image "$version"
  [ -s "$initramfs" ] || { echo "missing initramfs: $initramfs"; exit 1; }
  command -v dnsmasq >/dev/null || { echo "dnsmasq required for the TFTP server (sudo apt install dnsmasq)"; exit 1; }

  echo "=== from-scratch recovery: $unit stock-port $port boot-ip $bootip ==="
  echo "1) temporary 192.168.1.2/24 on the L2 (U-Boot serverip) + TFTP server"
  local tftp_root; tftp_root="$(mktemp -d)"
  cp "$initramfs" "$tftp_root/vmlinux.gz.uImage.3912"
  ip addr add 192.168.1.2/24 dev wlp4s0 2>/dev/null || true
  dnsmasq --port=0 --enable-tftp --tftp-root="$tftp_root" --user=root --pid-file=/tmp/rig-tftp.pid
  trap 'kill "$(cat /tmp/rig-tftp.pid 2>/dev/null)" 2>/dev/null; rm -rf "$tftp_root"; ip addr del 192.168.1.2/24 dev wlp4s0 2>/dev/null || true' EXIT
  echo "   TFTP serving $(basename "$initramfs") as vmlinux.gz.uImage.3912 (sha ${PINNED_INITRAMFS[$version]:0:12}...)"

  echo "2) PoE power cycle stock port $port via StockWeb (off 8s -> on)"
  "$VENVS_PY" - "$port" <<'PY'
import sys
from tollgate_lab.hardware.zyxel_stock import StockWeb
import subprocess, os
pw = subprocess.run(["sops", "-d", os.path.expanduser("~/conwrt-bench/secrets/secrets.json")],
                    capture_output=True, text=True,
                    env={**os.environ, "SOPS_AGE_KEY_FILE": os.path.expanduser("~/.config/age/keys.txt")}).stdout
import json
pw = json.loads(pw)["fleet"]["bench_root_password"]
sw = StockWeb("192.168.13.3", pw)
port = int(sys.argv[1])
sw.set_poe_state(port, False)
import time; time.sleep(8)
sw.set_poe_state(port, True)
print(f"poe cycle done for port {port}; status:", {k: v.get("status") for k, v in sw.poe_status().items() if int(k) == port})
PY

  echo "3) waiting for initramfs SSH at $bootip (U-Boot TFTP window; up to 300s)"
  local deadline=$((SECONDS + 300))
  until ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "root@$bootip" 'echo up' 2>/dev/null; do
    [ $SECONDS -lt $deadline ] || { echo "FAIL: initramfs never came up at $bootip (check TFTP RRQ / bootcmd boot_net)"; exit 1; }
    sleep 5
  done

  echo "4) sysupgrade -n from initramfs"
  local overlay_dir; overlay_dir="$(mktemp -d)"
  local overlay; overlay="$(build_overlay "$unit" "$overlay_dir/ovl")"
  scp -O -o BatchMode=yes "$img" "$overlay" "root@$bootip:/tmp/"
  ssh -o BatchMode=yes "root@$bootip" "sysupgrade -n -f /tmp/overlay-$unit.tar.gz /tmp/$(basename "$img")" || true

  wait_ssh_and_verify "$unit" "$version"
}

cmd_switch_upgrade() {
  # GS1900-8HP #1 (OpenWrt, root@192.168.13.2) upgrade lane.
  # GATED: requires --confirm AND the phone-E2E agent must not be working
  # (a switch reboot cold-cycles ap-lan2 = the E2E's serial feed).
  # Serial recovery (door #2) is ON-DEMAND per owner 2026-09-25: no
  # pre-wiring; if the upgrade strands the box, wire the UART header
  # (recipes/zyxel/gs1900-8hp — VCC/TX/RX/GND, 3.3V, 115200 8N1).
  # Artifact decision: STOCK downloads.openwrt.org image; keep-settings
  # sysupgrade (NO -n) preserves /etc AND the overlay — including the
  # Amperstrand realtek-poe fork package (userspace apk pkg) — verified
  # post-upgrade. Kernel-pinned kmods would NOT survive; gate below.
  local version="25.12.5" confirm=false a
  for a in "$@"; do
    case "$a" in
      --confirm) confirm=true ;;
      2[0-9].[0-9]*.[0-9]*) version="$a" ;;
      *) echo "unknown flag: $a" >&2; exit 2 ;;
    esac
  done
  $confirm || { echo "REFUSED: this reboots the lab switch (cold-cycles every PD). Pass --confirm after the checklist in docs/rig-flash.md"; exit 2; }
  command -v herdr >/dev/null && herdr agent get phone 2>/dev/null | grep -q '"agent_status":"working"' \
    && { echo "REFUSED: herdr agent 'phone' is working (E2E in progress — a switch reboot kills its serial feed)"; exit 2; }

  local img sum snapdir
  img="$IMAGE_DIR/$(sw_image_name "$version")"
  sum="${PINNED_SW_SYSUPGRADE[$version]:-}"
  [ -n "$sum" ] || { echo "no pinned sha256 for switch $version (populate from sha256sums-<v>-rtl838x)"; exit 1; }
  [ -f "$img" ] || { echo "missing image: $img"; exit 1; }
  echo "$sum  $img" | sha256sum -c - >/dev/null || { echo "sha256 MISMATCH: $img"; exit 1; }
  echo "switch image verified: $(basename "$img")"

  echo "--- preflight (board, poe package deps, E2E-critical residents) ---"
  ssh -o BatchMode=yes -o ConnectTimeout=10 root@192.168.13.2 '
    ubus call system board | grep -E "board_name|\"version\"";
    apk info --depends realtek-poe 2>/dev/null || echo "WARN: realtek-poe not queryable";
    ping -c1 -W2 192.168.102.51 >/dev/null 2>&1 && echo "ap-lan2 reference: reachable (will cold-cycle!)" || echo "ap-lan2 reference: UNREACHABLE pre-upgrade (investigate first)"'

  snapdir="$IMAGE_DIR/switch-pre-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$snapdir"
  ssh -o BatchMode=yes root@192.168.13.2 'tar -C / -czf - etc/config etc/dropbear' > "$snapdir/etc-backup.tar.gz"
  ssh -o BatchMode=yes root@192.168.13.2 'uci show network; echo ===; ip -4 addr; echo ===; bridge vlan 2>/dev/null; echo ===; apk info 2>/dev/null | sort' > "$snapdir/state.txt"
  echo "pre-upgrade snapshot: $snapdir"

  echo "--- CHECKLIST (verify each before continuing) ---"
  echo "  [ ] labgrid places on the switch RELEASED (coordinator: 192.168.13.208:20408)"
  echo "  [ ] /tmp/amperstrand-bench flock free (no other bench mutation in flight)"
  echo "  [ ] announced to herdr agents main + tollgate + stock-poe"
  echo "  [ ] bench power stable (September self-reboots were bench power cuts)"
  echo "  [ ] serial recovery kit located: USB-TTL 3.3V + pinout (recipes/zyxel/gs1900-8hp: VCC/TX/RX/GND header) — on-demand per owner, no pre-wiring"
  read -r -p "Type GO to sysupgrade $version onto the lab switch: " ack
  [ "$ack" = "GO" ] || { echo "aborted"; exit 1; }

  scp -O -o BatchMode=yes "$img" root@192.168.13.2:/tmp/
  ssh -o BatchMode=yes root@192.168.13.2 "sha256sum /tmp/$(basename "$img")" | grep -q "$sum" \
    || { echo "FAIL: on-device sha256 mismatch"; exit 1; }

  echo "--- sysupgrade (KEEP settings; ~2-3 min; every PD cold-cycles) ---"
  ssh -o BatchMode=yes root@192.168.13.2 "sysupgrade -v /tmp/$(basename "$img")" || true
  ssh-keygen -f "$HOME/.ssh/known_hosts" -R 192.168.13.2 >/dev/null 2>&1 || true

  echo "waiting for switch SSH (up to 600s)..."
  local deadline=$((SECONDS + 600)) board=""
  while [ $SECONDS -lt $deadline ]; do
    board="$(ssh -o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new root@192.168.13.2 'ubus call system board' 2>/dev/null)" && break
    sleep 8
  done
  [ -n "$board" ] || { echo "FAIL: switch SSH never returned — SERIAL CONSOLE TIME (rollback: 25.12.1 image in $IMAGE_DIR)"; exit 1; }

  echo "--- post-verify ---"
  local fail=0 post
  post="$(ssh -o BatchMode=yes root@192.168.13.2 'ubus call system board | grep "\"version\""; uci show network | md5sum; ip -4 addr | grep "192.168.10"; apk info realtek-poe 2>/dev/null | head -1; ubus call poe info | md5sum; sleep 3; ubus call poe info | md5sum')"
  echo "$post" | grep -q "\"version\": \"$version\"" || { echo "FAIL: version"; fail=1; }
  echo "$post" | grep -q "realtek-poe" || { echo "FAIL: fork realtek-poe package lost (reinstall from Amperstrand/realtek-poe ai-experiments dist)"; fail=1; }
  local digest1 digest2
  digest1="$(ssh -o BatchMode=yes root@192.168.13.2 'ubus call poe info | md5sum')"
  sleep 4
  digest2="$(ssh -o BatchMode=yes root@192.168.13.2 'ubus call poe info | md5sum')"
  [ "$digest1" = "$digest2" ] && echo "WARN: poe info digest frozen across 4s (consumption should jitter) — verify daemon liveness manually"
  echo "$post" | grep -q "192.168.10[2-8]\." || { echo "FAIL: per-DUT SVIs missing (VLAN config lost — restore from $snapdir)"; fail=1; }
  if [ $fail -eq 0 ]; then
    echo "PASS: switch on $version, config + poe package intact"
    echo "POST-STEPS (runtime-only state was wiped by the reboot):"
    echo "  1. re-run scripts/gs1900-bench-arm.sh (owner/Mac) — /32 pin route + TFTP lifelines"
    echo "  2. verify ap-lan2 SSH at 192.168.102.51 + serial bridge health (splice target!)"
    echo "  3. one manage/verify PoE cycle on the EMPTY lan7 test port to prove the daemon"
  else
    exit 1
  fi
}

main() {
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    in-place)     cmd_in_place "$@" ;;
    verify)       verify_unit "$@" ;;
    download)     cmd_download "$@" ;;
    from-scratch) cmd_from_scratch "$@" ;;
    switch-upgrade) cmd_switch_upgrade "$@" ;;
    *) sed -n '2,20p' "$0"; echo "usage: $0 {in-place|verify|download|from-scratch} <alpha|beta> [version] [--confirm]"; echo "       $0 switch-upgrade [25.12.5] --confirm"; exit 2 ;;
  esac
}

main "$@"
