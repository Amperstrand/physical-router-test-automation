#!/bin/bash
# upgrade-bench.sh — v0.5.0 -> v0.6.0 upgrade emulation bench on ai-legion-small
#
# Creates an isolated QEMU bench (bridge + tap + NAT + fresh OpenWrt overlay)
# for testing tollgate-wrt package upgrades without physical hardware.
# Host: ai-legion-small (16c/31GB, /dev/kvm, passwordless sudo).
# Base image: ~/tollgate-virtual-lab/images/openwrt-base.qcow2 (OpenWrt 24.10.1 x86_64,
# near-vanilla; LAN defaults -> provisioned over serial on first boot).
#
# Usage (from ai-legion-small):
#   bash scripts/upgrade-emulation/upgrade-bench.sh up       # bridge+tap+NAT+overlay+boot
#   bash scripts/upgrade-emulation/upgrade-bench.sh provision # serial: root pw + 10.99.99.1
#   bash scripts/upgrade-emulation/upgrade-bench.sh down      # graceful VM stop + bridge rm
#
# Conventions:
#   Bridge tg-upg-br 10.99.99.2/24, tap tg-upg-tap, VM 10.99.99.1, MAC 52:54:00:99:99:01
#   VM root password: set by `provision` to $VMPW (default Upgr4deTest-2026)
#   ~/upgrade-test/{run,logs,artifacts,etc-snapshots,mint2} on the host
#   vmssh helper: ~/upgrade-test/vmssh '<command>'
set -e

BR=tg-upg-br; TAP=tg-upg-tap; HOSTIP=10.99.99.2; VMIP=10.99.99.1
BASE=~/tollgate-virtual-lab/images/openwrt-base.qcow2
OV=~/upgrade-test/alpha.qcow2
RUN=~/upgrade-test/run
VMPW="${VMPW:-Upgr4deTest-2026}"

cmd_up() {
  mkdir -p "$RUN" ~/upgrade-test/{logs,artifacts,etc-snapshots}
  if ! ip link show $BR &>/dev/null; then
    sudo ip link add $BR type bridge
    sudo ip addr add $HOSTIP/24 dev $BR
    sudo ip link set $BR up
    echo "bridge $BR created ($HOSTIP/24)"
  fi
  sudo iptables -t nat -C POSTROUTING -s 10.99.99.0/24 ! -o $BR -j MASQUERADE 2>/dev/null || \
    sudo iptables -t nat -A POSTROUTING -s 10.99.99.0/24 ! -o $BR -j MASQUERADE
  if ! ip link show $TAP &>/dev/null; then
    sudo ip tuntap add dev $TAP mode tap
    sudo ip link set $TAP master $BR
    sudo ip link set $TAP up
    echo "tap $TAP created"
  fi
  [ -f "$OV" ] || qemu-img create -f qcow2 -b "$BASE" -F qcow2 "$OV"
  if [ ! -f $RUN/vm.pid ] || ! sudo kill -0 "$(cat $RUN/vm.pid)" 2>/dev/null; then
    bash "$(dirname "$0")/boot-vm.sh"
  else
    echo "VM already running (pid $(cat $RUN/vm.pid))"
  fi
}

cmd_provision() {
  # First-boot serial provisioning: root password + static LAN + NAT gateway.
  bash "$(dirname "$0")/serial.sh" "printf \"%s\n%s\n\" $VMPW $VMPW | passwd root" 4
  bash "$(dirname "$0")/serial.sh" "uci set network.lan.ipaddr=\"$VMIP\"" 2
  bash "$(dirname "$0")/serial.sh" "uci set network.lan.netmask=\"255.255.255.0\"" 2
  bash "$(dirname "$0")/serial.sh" "uci set network.lan.gateway=\"$HOSTIP\"" 2
  bash "$(dirname "$0")/serial.sh" "uci set network.lan.dns=\"1.1.1.1\"" 2
  bash "$(dirname "$0")/serial.sh" "uci commit network; /etc/init.d/network restart" 6
  cat > ~/upgrade-test/vmssh << EOF
#!/bin/bash
exec sshpass -p "$VMPW" ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$VMIP "\$@"
EOF
  chmod +x ~/upgrade-test/vmssh
  sleep 5
  ~/upgrade-test/vmssh "echo SSH_OK; ping -c 2 -W 3 8.8.8.8 | tail -1"
}

cmd_down() {
  if [ -f $RUN/vm.pid ]; then
    echo "system_powerdown" | timeout 5 sudo socat - UNIX-CONNECT:$RUN/monitor.sock 2>/dev/null || true
    sleep 10
    sudo kill "$(cat $RUN/vm.pid)" 2>/dev/null || true
    rm -f $RUN/vm.pid
  fi
  sudo ip link del $TAP 2>/dev/null || true
  sudo ip link del $BR 2>/dev/null || true
  sudo iptables -t nat -D POSTROUTING -s 10.99.99.0/24 ! -o $BR -j MASQUERADE 2>/dev/null || true
  echo "bench down"
}

case "${1:-}" in
  up) cmd_up ;;
  provision) cmd_provision ;;
  down) cmd_down ;;
  *) echo "usage: $0 {up|provision|down}"; exit 1 ;;
esac
