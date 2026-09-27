# ─── Labgrid topology manager ────────────────────────────────────────────
"""Manages the virtual topology that interconnects labgrid targets.

Labgrid manages devices; this module manages the virtual networks that
connect them. It creates Linux bridges, attaches QEMU VMs to them, and
sets up mac80211_hwsim virtual radios for wireless protocol testing.

Usage from conftest.py:
    topology = TopologyManager()
    topology.ensure_bridge("tg-poc-br", "10.99.99.0/24")
    topology.ensure_hwsim_radios(count=2)
    topology.start_openwrt_vm("alpha")
    topology.start_debian_vm("client")
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger("tollgate.labgrid.topology")

IMAGES_DIR = Path(os.environ.get("TOLLGATE_VM_IMAGES", 
                                 os.path.expanduser("~/tollgate-virtual-lab/images")))
BRIDGE_PREFIX = "tg-"


def sh(cmd: str, timeout: int = 30, check: bool = True) -> str:
    """Run a shell command, return stdout."""
    r = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd failed ({r.returncode}): {cmd}\n{r.stderr}")
    return r.stdout.strip()


class TopologyManager:
    """Manages virtual network topology for labgrid targets."""

    def __init__(self):
        self.bridges: dict[str, str] = {}
        self.vms: dict[str, subprocess.Popen] = {}
        self.hwsim_radios = 0

    # ─── Bridges ─────────────────────────────────────────

    def ensure_bridge(self, name: str, subnet: str,
                      host_ip: str | None = None) -> str:
        """Create a Linux bridge if not present. Returns the host IP."""
        if not host_ip:
            # Derive host IP from subnet (e.g. 10.99.99.0/24 → 10.99.99.2)
            net_base = subnet.rsplit(".", 1)[0]
            host_ip = f"{net_base}.2"

        exists = sh(f"ip link show {name} 2>/dev/null", check=False)
        if not exists:
            sh(f"ip link add {name} type bridge")
            sh(f"ip addr add {host_ip}/24 dev {name}")
            sh(f"ip link set {name} up")
            log.info("created bridge %s (%s, host=%s)", name, subnet, host_ip)

        self.bridges[name] = host_ip
        return host_ip

    def destroy_bridge(self, name: str) -> None:
        sh(f"ip link set {name} down 2>/dev/null; ip link del {name} 2>/dev/null",
           check=False)
        self.bridges.pop(name, None)

    # ─── Virtual wireless (mac80211_hwsim) ──────────────

    def ensure_hwsim_radios(self, count: int = 2) -> int:
        """Load mac80211_hwsim kernel module with N radios."""
        try:
            current = int(sh("ls /sys/kernel/debug/ieee80211/phy*/ -d 2>/dev/null | wc -l"))
        except (ValueError, RuntimeError):
            current = 0

        if current < count:
            sh(f"modprobe mac80211_hwsim radios={count}")
            time.sleep(1)
            new_count = int(sh("ls /sys/kernel/debug/ieee80211/phy*/ -d | wc -l"))
            log.info("loaded mac80211_hwsim: %d radios", new_count)
            self.hwsim_radios = new_count
        else:
            self.hwsim_radios = current
            log.info("mac80211_hwsim already has %d radios", current)

        return self.hwsim_radios

    # ─── QEMU VMs ─────────────────────────────────────────

    def start_openwrt_vm(self, name: str, bridge: str = "tg-poc-br",
                          mem: str = "256M") -> str:
        """Start an OpenWrt QEMU VM attached to the given bridge."""
        image = IMAGES_DIR / "openwrt-base.qcow2"
        overlay = Path("/tmp") / f"openwrt-{name}.qcow2"

        if not image.exists():
            raise FileNotFoundError(f"OpenWrt image not found: {image}")

        # Create overlay if needed
        if not overlay.exists():
            sh(f"qemu-img create -f qcow2 -b {image} {overlay}")

        # Determine bridge interface
        bridge_iface = sh(f"ip -o link show {bridge} | awk '{{print $2}}' | tr -d ':'")

        # Start QEMU
        cmd = (
            f"qemu-system-x86_64 "
            f"-m {mem} "
            f"-drive file={overlay},format=qcow2,if=virtio "
            f"-netdev bridge,id=net0,br={bridge} "
            f"-device virtio-net-pci,netdev=net0 "
            f"-display none -daemonize "
            f"-pidfile /tmp/openwrt-{name}.pid "
            f"-serial unix:/tmp/openwrt-{name}.sock,server,nowait"
        )
        sh(cmd, timeout=15)
        log.info("started OpenWrt VM '%s' on bridge '%s'", name, bridge)

        # Wait for SSH
        self._wait_ssh("10.99.99.1" if name == "alpha" else f"10.99.99.{20 + hash(name) % 200}")
        return name

    def start_debian_vm(self, name: str, bridge: str = "tg-poc-br",
                         mem: str = "512M") -> str:
        """Start a Debian client QEMU VM attached to the given bridge."""
        image = IMAGES_DIR / "debian-12-generic-amd64.qcow2"
        overlay = Path("/tmp") / f"debian-{name}.qcow2"
        seed = IMAGES_DIR / f"seed-{name}.iso"

        if not image.exists():
            raise FileNotFoundError(f"Debian image not found: {image}")
        if not overlay.exists():
            sh(f"qemu-img create -f qcow2 -b {image} {overlay}")

        cmd = (
            f"qemu-system-x86_64 "
            f"-m {mem} "
            f"-drive file={overlay},format=qcow2,if=virtio "
            f"-drive file={seed},media=cdrom "
            f"-netdev bridge,id=net0,br={bridge} "
            f"-device virtio-net-pci,netdev=net0 "
            f"-display none -daemonize "
            f"-pidfile /tmp/debian-{name}.pid"
        )
        sh(cmd, timeout=15)
        log.info("started Debian VM '%s' on bridge '%s'", name, bridge)
        return name

    def stop_vm(self, name: str, prefix: str = "openwrt") -> None:
        pidfile = Path("/tmp") / f"{prefix}-{name}.pid"
        if pidfile.exists():
            pid = pidfile.read_text().strip()
            sh(f"kill {pid} 2>/dev/null", check=False)
            pidfile.unlink(missing_ok=True)
            log.info("stopped VM '%s'", name)

    def _wait_ssh(self, ip: str, timeout: int = 60) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                sh(f"ssh -o ConnectTimeout=2 -o StrictHostKeyChecking=no "
                   f"root@{ip} 'echo ok' 2>/dev/null", timeout=5, check=False)
                return True
            except (subprocess.TimeoutExpired, RuntimeError):
                pass
            time.sleep(2)
        log.warning("SSH to %s not ready after %ds", ip, timeout)
        return False

    # ─── Lifecycle ─────────────────────────────────────────

    def setup_full_topology(self) -> dict:
        """Set up the complete test topology. Returns a status dict."""
        status = {}
        status["bridge"] = self.ensure_bridge("tg-poc-br", "10.99.99.0/24")
        status["hwsim"] = self.ensure_hwsim_radios(2)
        try:
            status["openwrt"] = self.start_openwrt_vm("alpha")
        except Exception as e:
            status["openwrt_error"] = str(e)
        try:
            status["debian"] = self.start_debian_vm("client")
        except Exception as e:
            status["debian_error"] = str(e)
        return status

    def teardown(self) -> None:
        """Stop all VMs and remove bridges."""
        for name in list(self.vms):
            self.stop_vm(name)
        for bridge in list(self.bridges):
            self.destroy_bridge(bridge)
