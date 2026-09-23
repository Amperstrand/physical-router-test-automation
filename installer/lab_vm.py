"""Fresh OpenWrt VM lifecycle for tollgate-installer E2E tests.

Boots a dedicated, isolated QEMU OpenWrt VM (baked ``openwrt-base.qcow2``
overlay) on its own bridge (``tg-inst-br``, 10.99.95.0/24) with host NAT,
re-IPs the VM from the baked 10.99.99.1 to 10.99.95.1 over the serial
console, and verifies SSH access. Nothing on 10.99.99.x (poc lab),
10.99.88.x, 10.99.87.x or 192.168.13.x is touched.

Proven in the 2026-09-19 spike: see installer/README.md.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

BRIDGE = "tg-inst-br"
TAP = "tg-inst-tap"
SUBNET = "10.99.95.0/24"
HOST_IP = "10.99.95.2/24"
VM_IP = "10.99.95.1"
VM_MAC = "52:54:00:95:00:01"

LAB_DIR = Path.home() / "tollgate-virtual-lab"
BASE_IMAGE = LAB_DIR / "images" / "openwrt-base.qcow2"
WORKDIR = LAB_DIR / "run" / "installer"
DISK = LAB_DIR / "overlays" / "installer-test.qcow2"


def _run(cmd: list[str], timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed rc={r.returncode}: {r.stderr.strip()[:300]}")
    return r


class InstallerLab:
    """Owns the isolated bridge + fresh OpenWrt VM used by installer tests."""

    def __init__(self, password: str) -> None:
        self.password = password
        self.qemu_pid: int | None = None
        self.serial_log: Path | None = None
        WORKDIR.mkdir(parents=True, exist_ok=True)

    # ── host network ────────────────────────────────────────────────────
    def cleanup_prior(self) -> None:
        """Tear down any previous run's artifacts (idempotent, best effort)."""
        if Path(WORKDIR / "router.pid").exists():
            try:
                pid = int((WORKDIR / "router.pid").read_text().strip())
                os.kill(pid, 15)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        # Kill any QEMU still attached to our tap (e.g. a crashed run).
        r = _run(["pgrep", "-f", f"ifname={TAP}"], check=False)
        for pid in filter(None, r.stdout.split()):
            try:
                os.kill(int(pid), 15)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(1)
        _run(["sudo", "ip", "link", "del", TAP], check=False)
        _run(["sudo", "ip", "link", "del", BRIDGE], check=False)
        _run(["sudo", "iptables", "-D", "FORWARD", "-i", BRIDGE, "-j", "ACCEPT"], check=False)
        _run(["sudo", "iptables", "-D", "FORWARD", "-o", BRIDGE, "-j", "ACCEPT"], check=False)
        _run(["sudo", "iptables", "-t", "nat", "-D", "POSTROUTING", "-s", SUBNET,
              "!", "-o", BRIDGE, "-j", "MASQUERADE"], check=False)
        # Try both ufw variants: this suite adds the bare rule; the 2026-09-19
        # spike added a comment-tagged one. ufw delete needs an exact match.
        _run(["sudo", "ufw", "route", "delete", "allow", "from", SUBNET], check=False)
        _run(["sudo", "ufw", "route", "delete", "allow", "from", SUBNET,
              "comment", "tg-installer-lab"], check=False)

    def ensure_network(self) -> None:
        _run(["sudo", "ip", "link", "add", "name", BRIDGE, "type", "bridge"], check=False)
        _run(["sudo", "ip", "link", "set", BRIDGE, "up"])
        _run(["sudo", "ip", "addr", "add", HOST_IP, "dev", BRIDGE], check=False)
        if _run(["sudo", "iptables", "-C", "FORWARD", "-i", BRIDGE, "-j", "ACCEPT"], check=False).returncode != 0:
            _run(["sudo", "iptables", "-I", "FORWARD", "1", "-i", BRIDGE, "-j", "ACCEPT"])
        if _run(["sudo", "iptables", "-C", "FORWARD", "-o", BRIDGE, "-j", "ACCEPT"], check=False).returncode != 0:
            _run(["sudo", "iptables", "-I", "FORWARD", "2", "-o", BRIDGE, "-j", "ACCEPT"])
        if _run(["sudo", "iptables", "-t", "nat", "-C", "POSTROUTING", "-s", SUBNET,
                 "!", "-o", BRIDGE, "-j", "MASQUERADE"], check=False).returncode != 0:
            _run(["sudo", "iptables", "-t", "nat", "-A", "POSTROUTING", "-s", SUBNET,
                  "!", "-o", BRIDGE, "-j", "MASQUERADE"])
        _run(["sudo", "ufw", "route", "allow", "from", SUBNET])
        _run(["sudo", "ip", "tuntap", "add", "dev", TAP, "mode", "tap", "user", os.environ.get("USER", "ubuntu")], check=False)
        _run(["sudo", "ip", "link", "set", TAP, "master", BRIDGE])
        _run(["sudo", "ip", "link", "set", TAP, "up"])

    def teardown_network(self) -> None:
        self.cleanup_prior()

    # ── VM lifecycle ────────────────────────────────────────────────────
    def boot_fresh_vm(self, boot_timeout: int = 180) -> None:
        """Boot a fresh overlay and re-IP it to VM_IP over the serial console."""
        if DISK.exists():
            DISK.unlink()
        _run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(BASE_IMAGE), str(DISK)])
        serial = WORKDIR / "serial.sock"
        if serial.exists():
            serial.unlink()
        log = open(WORKDIR / "qemu.stdout", "w")
        err = open(WORKDIR / "qemu.stderr", "w")
        proc = subprocess.Popen(
            ["qemu-system-x86_64", "-enable-kvm", "-m", "512", "-smp", "1", "-nographic",
             f"-serial", f"unix:{serial},server,nowait",
             f"-monitor", f"unix:{WORKDIR / 'monitor.sock'},server,nowait",
             "-drive", f"file={DISK},if=virtio,format=qcow2",
             "-netdev", f"tap,id=lan,ifname={TAP},script=no,downscript=no",
             "-device", f"virtio-net-pci,netdev=lan,mac={VM_MAC}"],
            stdout=log, stderr=err, stdin=subprocess.DEVNULL,
        )
        self.qemu_pid = proc.pid
        (WORKDIR / "router.pid").write_text(f"{proc.pid}\n")
        self._reip_over_serial(boot_timeout)
        self._wait_ssh(60)
        installed = self.ssh("opkg list-installed 2>/dev/null | grep -ci tollgate || true").strip()
        if installed not in ("0", ""):
            raise RuntimeError(f"fresh VM already has tollgate packages: {installed}")

    def _reip_over_serial(self, timeout: int) -> None:
        """Get a root shell on the askconsole serial and re-IP the LAN."""
        sock_path = str(WORKDIR / "serial.sock")
        deadline = time.time() + timeout
        while not Path(sock_path).exists() and time.time() < deadline:
            time.sleep(0.5)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(sock_path)
        buf = ""

        def drain(t: float = 0.3) -> None:
            nonlocal buf
            end = time.time() + t
            while time.time() < end:
                try:
                    d = s.recv(4096)
                    if not d:
                        return
                    buf += d.decode(errors="replace")
                    if self.serial_log is not None:
                        with open(self.serial_log, "a") as lf:
                            lf.write(d.decode(errors="replace"))
                except socket.timeout:
                    pass

        prompt_at = None
        while time.time() < deadline:
            s.sendall(b"\n")
            drain(2.0)
            lines = [l for l in buf.splitlines() if l.strip()]
            if lines and (lines[-1].endswith("#") or "root@" in lines[-1]):
                prompt_at = time.time()
                break
            if "login:" in buf:
                s.sendall(b"root\n")
                drain(2.0)
        if prompt_at is None:
            s.close()
            raise RuntimeError(f"no serial shell prompt within {timeout}s; console tail: {buf[-400:]}")

        def run(cmd: str, t: float = 15) -> bool:
            nonlocal buf
            buf = ""
            s.sendall((cmd + "\n").encode())
            end = time.time() + t
            while time.time() < end:
                drain(0.4)
                tail = [l for l in buf.splitlines() if l.strip()]
                if tail and (tail[-1].endswith("#") or "root@" in tail[-1]):
                    return True
            return False

        # Prompt-echo detection on the BusyBox console is unreliable under
        # load; the commands are sent unconditionally and _wait_ssh() — run
        # by boot_fresh_vm() right after — is the authoritative outcome check.
        for c in ["uci set network.lan.ipaddr='10.99.95.1'",
                  "uci set network.lan.netmask='255.255.255.0'",
                  "uci set network.lan.gateway='10.99.95.2'",
                  "uci set network.lan.dns='8.8.8.8'",
                  "uci commit network"]:
            run(c)
        run("/etc/init.d/network restart", 20)
        s.close()

    def _wait_ssh(self, timeout: int) -> None:
        deadline = time.time() + timeout
        last_err = ""
        while time.time() < deadline:
            try:
                out = self.ssh("echo SSH_OK", timeout=10)
                if "SSH_OK" in out:
                    return
            except subprocess.TimeoutExpired as e:
                last_err = str(e)
            except RuntimeError as e:
                last_err = str(e)
            time.sleep(3)
        raise RuntimeError(f"SSH to fresh VM {VM_IP} failed within {timeout}s: {last_err[:200]}")

    def ssh(self, cmd: str, timeout: int = 30) -> str:
        r = subprocess.run(
            ["sshpass", "-p", self.password,
             "ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
             "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR",
             f"root@{VM_IP}", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            raise RuntimeError(f"ssh rc={r.returncode}: {r.stderr.strip()[:200]}")
        return r.stdout

    def stop_vm(self) -> None:
        if self.qemu_pid:
            try:
                os.kill(self.qemu_pid, 15)
                time.sleep(2)
            except ProcessLookupError:
                pass
        if DISK.exists():
            DISK.unlink()


def load_base_password(repo_root: Path) -> str:
    creds = repo_root / "credentials" / "virtual-lab-credentials.json"
    data = json.loads(creds.read_text())
    return data["password"]
