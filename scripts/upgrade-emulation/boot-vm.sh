#!/bin/bash
# boot-vm.sh — boot the upgrade-test VM (daemonized, serial+monitor unix sockets)
set -e
RUN=~/upgrade-test/run
OV=~/upgrade-test/alpha.qcow2
sudo rm -f $RUN/serial.sock $RUN/monitor.sock $RUN/vm.pid
sudo qemu-system-x86_64 -enable-kvm -display none -daemonize \
  -name tg-upg-alpha \
  -m 512 -smp 2 \
  -serial unix:$RUN/serial.sock,server,nowait \
  -monitor unix:$RUN/monitor.sock,server,nowait \
  -drive file=$OV,if=virtio,format=qcow2 \
  -netdev tap,id=lan,ifname=tg-upg-tap,script=no,downscript=no \
  -device virtio-net-pci,netdev=lan,mac=52:54:00:99:99:01
sleep 1
sudo pgrep -f "qemu-system-x86_64.*tg-upg-alpha" | head -1 | sudo tee $RUN/vm.pid
echo "VM pid: $(cat $RUN/vm.pid)"
