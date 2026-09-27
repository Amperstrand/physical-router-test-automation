#!/usr/bin/env python3
"""Manage the local TollGate virtual lab.

The first implementation target is the Ubuntu machine reachable as `218`.
This script intentionally starts with diagnostics/bootstrap commands so the VM
orchestration can be built on a verified host instead of assumptions.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import secrets
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from collections.abc import Callable

from lib.backend import BACKEND_CHOICES_CLI


REQUIRED_COMMANDS = [
    "qemu-system-x86_64",
    "qemu-img",
    "ip",
    "python3",
    "curl",
]

OPTIONAL_COMMANDS = [
    "podman",
    "dnsmasq",
    "brctl",
    "sshpass",
]

APT_PACKAGES = [
    "qemu-system-x86",
    "qemu-utils",
    "iproute2",
    "curl",
    "dnsmasq-base",
    "bridge-utils",
    "python3",
]

DEFAULT_OPENWRT_VERSION = "24.10.1"
DEFAULT_WORKDIR = "~/tollgate-virtual-lab"
POC_BRIDGE = "tg-poc-br"
POC_TAP = "tg-poc-tap"
DEBIAN_TAP = "tg-poc-tap2"
DEBIAN_RAM = 1024
DEBIAN_IMAGE = "debian-12-generic-amd64.qcow2"
DEBIAN_IMAGE_URL = f"https://cloud.debian.org/images/cloud/bookworm/latest/{DEBIAN_IMAGE}"
DEBIAN_MAC = "de:54:4e:91:49:da"
POC_OPENWRT_MAC = "52:54:00:12:34:56"
DEBIAN_CLIENT_IP = "10.99.99.100"
POC_GATEWAY = "10.99.99.1"
POC_HOST_BRIDGE_IP = "10.99.99.2/24"


def _generate_password():
    env_pw = os.environ.get("TOLLGATE_FIRMWARE_PASSWORD") or os.environ.get("TOLLGATE_VIRTUAL_LAB_PASSWORD")
    if env_pw:
        return env_pw
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("TOLLGATE_SSH_PASSWORD="):
                existing = line.split("=", 1)[1].strip()
                if existing:
                    return existing
    return ''.join(secrets.choice('abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(24))


def _ensure_ssh_key():
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(exist_ok=True, mode=0o700)

    key_path = ssh_dir / "id_ed25519"
    if key_path.exists():
        return

    gcloud_key = ssh_dir / "google_compute_engine"
    if gcloud_key.exists():
        os.symlink(gcloud_key, key_path)
        gcloud_pub = ssh_dir / "google_compute_engine.pub"
        if gcloud_pub.exists():
            os.symlink(gcloud_pub, ssh_dir / "id_ed25519.pub")
        print(f"Linked gcloud SSH key to {key_path}")
        return

    print(f"Generating new SSH keypair at {key_path}")
    subprocess.run([
        "ssh-keygen", "-t", "ed25519", "-f", str(key_path),
        "-N", "", "-C", "tollgate-virtual-lab",
    ], check=True)


def _save_credentials(password):
    import json
    creds_dir = Path(__file__).parent.parent / "credentials"
    creds_dir.mkdir(exist_ok=True)
    creds_path = creds_dir / "virtual-lab-credentials.json"
    creds = {"password": password, "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if creds_path.exists():
        try:
            existing = json.loads(creds_path.read_text())
            creds.update(existing)
            creds["password"] = password
        except Exception:
            pass
    creds_path.write_text(json.dumps(creds, indent=2))
    creds_path.chmod(0o600)


def _update_env_file(password):
    env_path = Path(__file__).parent.parent / ".env"
    lines = []
    found_ssh = False
    found_luci = False
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("TOLLGATE_SSH_PASSWORD="):
                lines.append(f"TOLLGATE_SSH_PASSWORD={password}")
                found_ssh = True
            elif line.startswith("TOLLGATE_LUCI_PASSWORD="):
                lines.append(f"TOLLGATE_LUCI_PASSWORD={password}")
                found_luci = True
            else:
                lines.append(line)
    if not found_ssh:
        lines.append(f"TOLLGATE_SSH_PASSWORD={password}")
    if not found_luci:
        lines.append(f"TOLLGATE_LUCI_PASSWORD={password}")
    env_path.write_text("\n".join(lines) + "\n")


POC_PASSWORD = _generate_password()
_save_credentials(POC_PASSWORD)
_update_env_file(POC_PASSWORD)
_ensure_ssh_key()

POC_SUBNET = "10.99.99.0/24"

# Template for the serial-console provisioning script.  Placeholders
# ``__WORKDIR__`` and ``__PASSWORD__`` are substituted at runtime by
# ``_generate_provision_script()``.  We use a plain string (not an
# f-string) so that Python braces inside the generated code do not need
# to be doubled.
_PROVISION_TEMPLATE = r"""
import socket, time, sys, os

SOCK_PATH = os.path.expanduser('__WORKDIR__') + '/run/serial.sock'
PASSWORD = '__PASSWORD__'
BOOT_TIMEOUT = 60

def recv_all(s, timeout=5):
    s.settimeout(timeout)
    chunks = []
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    except socket.timeout:
        pass
    return b''.join(chunks).decode('utf-8', errors='replace')

def send_and_wait(s, cmd, wait=3):
    s.sendall((cmd + '\n').encode())
    time.sleep(wait)
    return recv_all(s, timeout=2)

# Connect to serial console (retry until socket appears)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
deadline = time.time() + 30
connected = False
while time.time() < deadline:
    try:
        s.connect(SOCK_PATH)
        connected = True
        break
    except (ConnectionRefusedError, FileNotFoundError):
        time.sleep(2)
if not connected:
    print('TIMEOUT: could not connect to serial socket at ' + SOCK_PATH)
    sys.exit(1)

print('Connected to serial console, waiting for VM to boot...')

s.sendall(b'\n')
deadline = time.time() + BOOT_TIMEOUT
booted = False
while time.time() < deadline:
    data = recv_all(s, timeout=2)
    if data.strip():
        sys.stdout.write(data)
        sys.stdout.flush()
    if 'Please press Enter' in data:
        booted = True
        print('\nFresh boot detected (first-boot prompt)')
        break
    if 'root@OpenWrt' in data or data.strip().endswith('#'):
        booted = True
        print('\nAlready-provisioned boot detected (shell prompt)')
        # Already at shell, skip provisioning — just verify network
        send_and_wait(s, "ip addr show br-lan | grep 'inet '", wait=2)
        s.close()
        print('PROVISIONED OK (existing)')
        sys.exit(0)
        break
    time.sleep(2)
if not booted:
    print('TIMEOUT: VM did not reach boot prompt within ' + str(BOOT_TIMEOUT) + 's')
    s.close()
    sys.exit(1)

print('\nVM booted, activating console...')
time.sleep(1)
send_and_wait(s, '', wait=3)
recv_all(s, timeout=1)

# --- Set root password (BusyBox has no chpasswd) ---
print('Setting root password...')
resp = send_and_wait(s, "printf '%s\\n%s\\n' '" + PASSWORD + "' '" + PASSWORD + "' | passwd root", wait=5)
print(resp.strip())

# --- Enable SSH password authentication ---
print('Enabling SSH password auth...')
send_and_wait(s, "uci set dropbear.@dropbear[0].PasswordAuth='on'", wait=2)
send_and_wait(s, 'uci commit dropbear', wait=2)
send_and_wait(s, '/etc/init.d/dropbear restart', wait=3)

# --- Inject SSH public key ---
ssh_key_path = os.path.expanduser("~/.ssh/id_ed25519.pub")
if not os.path.exists(ssh_key_path):
    ssh_key_path = os.path.expanduser("~/.ssh/id_rsa.pub")
if os.path.exists(ssh_key_path):
    ssh_pubkey = open(ssh_key_path).read().strip()
    print('Injecting SSH public key...')
    send_and_wait(s, f"mkdir -p /etc/dropbear && echo '{ssh_pubkey}' > /etc/dropbear/authorized_keys && chmod 600 /etc/dropbear/authorized_keys", wait=2)

# --- Add WAN SSH firewall rule ---
print('Adding WAN SSH firewall rule...')
send_and_wait(s, 'uci add firewall rule', wait=2)
send_and_wait(s, "uci set firewall.@rule[-1].name='Allow-SSH-WAN'", wait=2)
send_and_wait(s, "uci set firewall.@rule[-1].src='wan'", wait=2)
send_and_wait(s, "uci set firewall.@rule[-1].dest_port='22'", wait=2)
send_and_wait(s, "uci set firewall.@rule[-1].proto='tcp'", wait=2)
send_and_wait(s, "uci set firewall.@rule[-1].target='ACCEPT'", wait=2)
send_and_wait(s, 'uci commit firewall', wait=2)
send_and_wait(s, 'fw4 restart', wait=5)

print('Configuring LAN IP and internet access via host bridge...')
send_and_wait(s, "uci set network.lan.ipaddr='10.99.99.1'", wait=2)
send_and_wait(s, "uci set network.lan.netmask='255.255.255.0'", wait=2)
send_and_wait(s, "uci set network.lan.gateway='10.99.99.2'", wait=2)
send_and_wait(s, "uci set network.lan.dns='8.8.8.8'", wait=2)
send_and_wait(s, 'uci commit network', wait=2)
send_and_wait(s, '/etc/init.d/network restart', wait=5)

print('PROVISIONED OK')
s.close()
"""

# Template for serial-console provisioning of the Debian nocloud VM.
# Placeholders ``__WORKDIR__`` and ``__PASSWORD__`` are substituted at runtime
# by ``_generate_debian_provision_script()``.
_DEBIAN_PROVISION_TEMPLATE = r"""
import socket, time, sys, os

SOCK_PATH = os.path.expanduser('__WORKDIR__') + '/run/serial-client.sock'
PASSWORD = '__PASSWORD__'
BOOT_TIMEOUT = 120

def recv_all(s, timeout=5):
    s.settimeout(timeout)
    chunks = []
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    except socket.timeout:
        pass
    return b''.join(chunks).decode('utf-8', errors='replace')

def send_and_wait(s, cmd, wait=3):
    s.sendall((cmd + '\n').encode())
    time.sleep(wait)
    return recv_all(s, timeout=2)

# Connect to serial console (retry until socket appears)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
deadline = time.time() + 30
connected = False
while time.time() < deadline:
    try:
        s.connect(SOCK_PATH)
        connected = True
        break
    except (ConnectionRefusedError, FileNotFoundError):
        time.sleep(2)
if not connected:
    print('TIMEOUT: could not connect to serial socket at ' + SOCK_PATH)
    sys.exit(1)

print('Connected to Debian serial console, waiting for VM to boot...')

# Wait for the login prompt (nocloud image boots to login:)
s.sendall(b'\n')
deadline = time.time() + BOOT_TIMEOUT
booted = False
while time.time() < deadline:
    data = recv_all(s, timeout=3)
    if data.strip():
        sys.stdout.write(data)
        sys.stdout.flush()
    if 'login:' in data:
        booted = True
        break
    time.sleep(2)
if not booted:
    print('TIMEOUT: Debian VM did not reach login prompt within ' + str(BOOT_TIMEOUT) + 's')
    s.close()
    sys.exit(1)

print('\nLogin prompt detected, logging in as root...')

time.sleep(2)
s.sendall(b'root\n')
time.sleep(3)
data = recv_all(s, timeout=2)
sys.stdout.write(data)
sys.stdout.flush()

if 'Password:' in data or 'password:' in data:
    s.sendall((PASSWORD + '\n').encode())
    time.sleep(3)
    data = recv_all(s, timeout=2)
    sys.stdout.write(data)
    sys.stdout.flush()

s.sendall(b'\n')
time.sleep(2)
data = recv_all(s, timeout=2)
sys.stdout.write(data)
sys.stdout.flush()

print('Waiting for cloud-init to finish...')
send_and_wait(s, 'cloud-init status --wait 2>/dev/null || true', wait=30)

# --- Configure networking (static IP) ---
print('Configuring networking...')
send_and_wait(s, 'ip link set ens3 up', wait=2)
send_and_wait(s, 'ip addr add 10.99.99.100/24 dev ens3', wait=2)
send_and_wait(s, 'ip route add default via 10.99.99.1', wait=2)
send_and_wait(s, 'echo "nameserver 10.99.99.2" > /etc/resolv.conf', wait=2)
# Persist as netplan: this image uses netplan+systemd-networkd and has no
# /etc/network/interfaces. Overwrite the DHCP match-all default — a separate
# higher-numbered file loses to it because netplan emits both with the same
# prefix and networkd picks the lexicographically-first match. Without
# persistence every reboot comes up network-less and serial provisioning
# becomes mandatory again on each start-poc.
send_and_wait(s, "printf 'network:\\n  version: 2\\n  ethernets:\\n    ens3:\\n      addresses:\\n        - 10.99.99.100/24\\n      routes:\\n        - to: default\\n          via: 10.99.99.1\\n      nameservers:\\n        addresses: [10.99.99.2]\\n' > /etc/netplan/90-default.yaml && chmod 600 /etc/netplan/90-default.yaml", wait=3)
send_and_wait(s, "printf '[Unit]\\nDescription=Regenerate networkd config from netplan at boot\\nDefaultDependencies=no\\nBefore=systemd-networkd.service\\n[Service]\\nType=oneshot\\nExecStart=/usr/sbin/netplan generate\\nRemainAfterExit=yes\\n[Install]\\nWantedBy=multi-user.target\\n' > /etc/systemd/system/netplan-generate-boot.service && systemctl enable netplan-generate-boot.service", wait=4)
send_and_wait(s, 'rm -f /etc/systemd/system/systemd-networkd.service.d/10-netplan.conf; rmdir --ignore-fail-on-non-empty /etc/systemd/system/systemd-networkd.service.d 2>/dev/null', wait=2)
send_and_wait(s, 'sleep 5 && ping -c 1 -W 5 10.99.99.1', wait=10)

print('Installing openssh-server...')
send_and_wait(s, 'apt-get update -qq', wait=60)
send_and_wait(s, 'apt-get install -y -qq openssh-server', wait=60)
send_and_wait(s, "sed -i 's/^#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config", wait=2)
send_and_wait(s, "sed -i 's/^PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config", wait=2)
send_and_wait(s, "sed -i 's/^#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config", wait=2)
send_and_wait(s, 'systemctl enable --now ssh', wait=5)

print('Resizing disk partition...')
send_and_wait(s, 'apt-get install -y -qq cloud-guest-utils', wait=60)
send_and_wait(s, 'growpart /dev/vda 1', wait=10)
send_and_wait(s, 'resize2fs /dev/vda1', wait=10)

print('DEBIAN PROVISIONED OK')
s.close()
"""


def host_is_remote(host: str) -> bool:
    return host.strip() not in {"", "local", "localhost", "127.0.0.1"}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def run_local(command: list[str], timeout: int = 30) -> CommandResult:
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())


def run_remote(host: str, script: str, timeout: int = 60) -> CommandResult:
    if host in {"", "local", "localhost", "127.0.0.1"}:
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as tf:
            tf.write(script)
            tf.flush()
            tmppath = tf.name
        try:
            proc = subprocess.run(
                ["bash", tmppath],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print(f"TIMEOUT running {tmppath}", file=sys.stderr, flush=True)
            os.unlink(tmppath)
            return CommandResult(1, "", "TIMEOUT")
        stderr_out = proc.stderr
        if proc.returncode != 0:
            saved = "/tmp/failed-script.sh"
            os.rename(tmppath, saved)
            print(f"Saved failed script to {saved}", file=sys.stderr, flush=True)
            print(f"rc={proc.returncode} stderr={stderr_out[:300]}", file=sys.stderr, flush=True)
            return CommandResult(proc.returncode, proc.stdout.strip(), stderr_out)
        os.unlink(tmppath)
        return CommandResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())
    if not script.startswith("bash -lc "):
        script = quote_script(script)
    return run_local(["ssh", host, script], timeout=timeout)


def run_python_on_host(host: str, python_code: str, timeout: int = 120) -> CommandResult:
    if host in {"", "local", "localhost", "127.0.0.1"}:
        try:
            proc = subprocess.run(
                ["python3"],
                input=python_code,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(1, "", "TIMEOUT")
    else:
        proc = subprocess.run(
            ["ssh", host, "python3"],
            input=python_code,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    return CommandResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())


def quote_script(script: str) -> str:
    return "bash -lc " + shlex.quote(script)


def remote_exists(host: str, command: str) -> bool:
    result = run_remote(host, quote_script(f"command -v {shlex.quote(command)} >/dev/null 2>&1"))
    return result.returncode == 0


def doctor(args: argparse.Namespace) -> int:
    host = cast(str, args.host)
    script = r'''
set +e
printf '== host ==\n'
hostname
uname -a
printf '\n== os ==\n'
if [ -r /etc/os-release ]; then . /etc/os-release; printf '%s %s\n' "$NAME" "$VERSION"; fi
printf '\n== cpu/memory ==\n'
nproc
free -h
printf '\n== kvm ==\n'
ls -l /dev/kvm 2>&1 || true
if [ -e /dev/kvm ] && [ -r /dev/kvm ] && [ -w /dev/kvm ]; then printf 'current user can access /dev/kvm\n'; else printf 'current user cannot access /dev/kvm\n'; fi
if [ -r /sys/module/kvm_amd/parameters/nested ]; then printf 'kvm_amd nested=%s\n' "$(cat /sys/module/kvm_amd/parameters/nested)"; fi
if [ -r /sys/module/kvm_intel/parameters/nested ]; then printf 'kvm_intel nested=%s\n' "$(cat /sys/module/kvm_intel/parameters/nested)"; fi
printf '\n== commands ==\n'
for c in qemu-system-x86_64 qemu-img ip python3 curl podman dnsmasq brctl sshpass; do
  printf '%s: ' "$c"
  command -v "$c" || true
done
printf '\n== user ==\n'
id
groups
'''
    result = run_remote(host, quote_script(script), timeout=60)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    missing_required = [cmd for cmd in REQUIRED_COMMANDS if not remote_exists(host, cmd)]
    missing_optional = [cmd for cmd in OPTIONAL_COMMANDS if not remote_exists(host, cmd)]

    print("\n== virtual lab readiness ==")
    if missing_required:
        print("missing required commands: " + ", ".join(missing_required))
        print("install on host:")
        print("  sudo apt update")
        print("  sudo apt install -y " + " ".join(APT_PACKAGES))
        return 1

    print("required commands: ok")
    if missing_optional:
        print("missing optional commands: " + ", ".join(missing_optional))
    else:
        print("optional commands: ok")

    kvm = run_remote(host, quote_script("test -e /dev/kvm"))
    if kvm.returncode != 0:
        print("/dev/kvm missing: QEMU can still run, but it will be too slow for the full lab")
        return 1

    kvm_access = run_remote(host, quote_script("test -r /dev/kvm -a -w /dev/kvm"))
    if kvm_access.returncode != 0:
        print("/dev/kvm exists, but the SSH user cannot access it")
        print("fix on host:")
        print("  sudo usermod -aG kvm $(whoami)")
        print("  # then open a fresh SSH session")
        return 1

    print("/dev/kvm: ok")
    return 0


def install_deps(args: argparse.Namespace) -> int:
    host = cast(str, args.host)
    packages = " ".join(shlex.quote(pkg) for pkg in APT_PACKAGES)
    script = f"sudo apt update && sudo apt install -y {packages}"
    result = run_remote(host, quote_script(script), timeout=900)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


def prepare_image(args: argparse.Namespace) -> int:
    host = cast(str, args.host)
    version = cast(str, args.openwrt_version)
    workdir = cast(str, args.workdir)
    image_name = f"openwrt-{version}-x86-64-generic-ext4-combined.img"
    image_gz = f"{image_name}.gz"
    image_url = (
        f"https://downloads.openwrt.org/releases/{version}/targets/x86/64/"
        f"{image_gz}"
    )
    script = f'''
set -eu
workdir={shlex.quote(workdir)}
workdir=$(eval printf '%s' "$workdir")
mkdir -p "$workdir/images" "$workdir/overlays"
cd "$workdir/images"
if [ ! -f {shlex.quote(image_gz)} ]; then
  curl -fL -o {shlex.quote(image_gz)} {shlex.quote(image_url)}
fi
if [ ! -f {shlex.quote(image_name)} ]; then
  gzip -dk {shlex.quote(image_gz)} || test -f {shlex.quote(image_name)}
fi
if [ ! -f openwrt-base.qcow2 ]; then
  qemu-img convert -f raw -O qcow2 {shlex.quote(image_name)} openwrt-base.qcow2
  qemu-img resize openwrt-base.qcow2 2G
fi
for router in seller reseller; do
  overlay="$workdir/overlays/${{router}}.qcow2"
  if [ ! -f "$overlay" ]; then
    qemu-img create -f qcow2 -F qcow2 -b "$workdir/images/openwrt-base.qcow2" "$overlay"
  fi
done
qemu-img info "$workdir/images/openwrt-base.qcow2"
printf '\\nPrepared overlays:\\n'
ls -lh "$workdir/overlays"
'''
    result = run_remote(host, quote_script(script), timeout=900)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


def _print_result(result: CommandResult) -> int:
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


def prepare_debian(args: argparse.Namespace) -> int:
    host = cast(str, args.host)
    workdir = cast(str, args.workdir)
    _, disk = _client_paths(workdir)
    script = f'''
set -eu
workdir={shlex.quote(workdir)}
workdir=$(eval printf '%s' "$workdir")
mkdir -p "$workdir/images" "$workdir/overlays"
cd "$workdir/images"

if [ ! -f {shlex.quote(DEBIAN_IMAGE)} ]; then
  curl -fL -o {shlex.quote(DEBIAN_IMAGE)} {shlex.quote(DEBIAN_IMAGE_URL)}
fi

overlay={disk}
if [ ! -f "$overlay" ]; then
  qemu-img create -f qcow2 -F qcow2 -b "$workdir/images/{DEBIAN_IMAGE}" "$overlay"
fi
qemu-img resize "$overlay" 10G

# NoCloud seed: lets cloud-init provision a fresh overlay on first boot
# (ssh server, root access, static IP), so serial-console provisioning is
# only a fallback. start-poc attaches images/seed.iso when it exists.
if command -v genisoimage >/dev/null 2>&1 || command -v mkisofs >/dev/null 2>&1; then
  pubkey=$(cat ~/.ssh/id_ed25519.pub 2>/dev/null || cat ~/.ssh/id_rsa.pub 2>/dev/null || true)
  cat > user-data <<UD
#cloud-config
hostname: debian-client
disable_root: false
ssh_pwauth: true
chpasswd:
  expire: false
  users:
    - name: root
      password: {POC_PASSWORD}
      type: text
packages:
  - openssh-server
  - curl
  - iputils-ping
  - iproute2
ssh_authorized_keys:
  - $pubkey
write_files:
  - path: /etc/ssh/sshd_config.d/99-tollgate-lab.conf
    permissions: '0644'
    content: |
      PermitRootLogin yes
      PasswordAuthentication yes
  - path: /etc/systemd/system/netplan-generate-boot.service
    permissions: '0644'
    content: |
      # Debian's netplan.io ships no boot hook and systemd-networkd.service
      # is sandboxed (ProtectSystem=strict, CapabilityBoundingSet without
      # DAC_OVERRIDE), so an ExecStartPre drop-in there fails and
      # crash-loops networkd. This unconfined oneshot regenerates
      # /run/systemd/network (tmpfs) before networkd starts instead.
      [Unit]
      Description=Regenerate networkd config from netplan at boot
      DefaultDependencies=no
      Before=systemd-networkd.service
      [Service]
      Type=oneshot
      ExecStart=/usr/sbin/netplan generate
      RemainAfterExit=yes
      [Install]
      WantedBy=multi-user.target
  - path: /etc/netplan/90-default.yaml
    permissions: '0600'
    content: |
      network:
        version: 2
        ethernets:
          ens3:
            addresses:
              - {DEBIAN_CLIENT_IP}/24
            routes:
              - to: default
                via: {POC_GATEWAY}
            nameservers:
              addresses: [{POC_HOST_BRIDGE_IP.split('/')[0]}]
runcmd:
  - systemctl enable netplan-generate-boot.service
  - systemctl restart ssh
UD
  cat > meta-data <<MD
instance-id: debian-client-local-01
local-hostname: debian-client
MD
  cat > network-config <<NC
version: 2
ethernets:
  ens3:
    addresses:
      - {DEBIAN_CLIENT_IP}/24
    routes:
      - to: default
        via: {POC_GATEWAY}
    nameservers:
      addresses: [{POC_HOST_BRIDGE_IP.split('/')[0]}]
NC
  mkisobin=$(command -v genisoimage || command -v mkisofs)
  "$mkisobin" -output seed.iso -volid cidata -rational-rock -joliet \\
    user-data meta-data network-config >/dev/null
  rm -f user-data meta-data network-config
  printf 'Built NoCloud seed: %s/seed.iso\\n' "$workdir/images"
else
  printf 'genisoimage/mkisofs not found — skipping seed; serial provisioning stays mandatory\\n' >&2
fi

printf 'Prepared Debian nocloud client image\\n'
qemu-img info "$overlay"
'''
    return _print_result(run_remote(host, quote_script(script), timeout=600))


def _poc_paths(workdir: str) -> tuple[str, str, str]:
    """Return (pidfile, serial_sock, disk) as shell-expanded path expressions."""
    expanded = "$(eval printf '%s' " + shlex.quote(workdir) + ")"
    pidfile = f"{expanded}/run/tollgate.pid"
    serial_sock = f"{expanded}/run/serial.sock"
    disk = f"{expanded}/overlays/tollgate-poc.qcow2"
    return pidfile, serial_sock, disk


def _client_paths(workdir: str) -> tuple[str, str]:
    """Return (pidfile, disk) for the Debian client VM."""
    expanded = "$(eval printf '%s' " + shlex.quote(workdir) + ")"
    pidfile = f"{expanded}/run/debian-client.pid"
    disk = f"{expanded}/overlays/debian-client.qcow2"
    return pidfile, disk


def _generate_provision_script(workdir: str) -> str:
    pwd = POC_PASSWORD.replace("'", "'\\''")
    wdir = workdir.replace("'", "'\\''")
    return _PROVISION_TEMPLATE.replace("__WORKDIR__", wdir).replace("__PASSWORD__", pwd)


def _generate_ssh_key_inject_script(workdir: str) -> str:
    import base64
    wdir = workdir.replace("'", "'\\''")
    pubkey_path = os.path.expanduser('~/.ssh/id_ed25519.pub')
    if not os.path.exists(pubkey_path):
        pubkey_path = os.path.expanduser('~/.ssh/id_rsa.pub')
    pubkey = ''
    if os.path.exists(pubkey_path):
        with open(pubkey_path) as f:
            pubkey = f.read().strip()
    setup_script = f'mkdir -p /root/.ssh && chmod 700 /root/.ssh && echo "{pubkey}" > /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys && echo KEY_OK'
    b64 = base64.b64encode(setup_script.encode()).decode()
    return f'''
import socket, time, sys, os
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock_path = os.path.expanduser('{wdir}') + '/run/serial-client.sock'
deadline = time.time() + 10
connected = False
while time.time() < deadline:
    try:
        s.connect(sock_path)
        connected = True
        break
    except (ConnectionRefusedError, FileNotFoundError):
        time.sleep(1)
if not connected:
    print('Cannot connect to serial socket')
    sys.exit(1)
s.settimeout(2)
try:
    while True:
        d = s.recv(4096)
        if not d: break
except: pass
s.sendall(('echo {b64} | base64 -d | bash' + chr(10)).encode())
time.sleep(3)
s.settimeout(2)
try:
    while True:
        d = s.recv(4096)
        if not d: break
        sys.stdout.write(d.decode('utf-8', errors='replace'))
except: pass
s.close()
'''


def _generate_debian_provision_script(workdir: str) -> str:
    """Return a self-contained Python script that provisions the Debian nocloud
    VM over the QEMU serial-console Unix socket."""
    pwd = POC_PASSWORD.replace("'", "'\\''")
    wdir = workdir.replace("'", "'\\''")
    return _DEBIAN_PROVISION_TEMPLATE.replace("__WORKDIR__", wdir).replace("__PASSWORD__", pwd)


def start_poc(args: argparse.Namespace) -> int:
    host = cast(str, args.host)
    ephemeral_client = bool(getattr(args, "ephemeral_client", False))
    workdir = cast(str, args.workdir)
    pidfile, _serial_sock, disk = _poc_paths(workdir)

    # Step 1: bridge, tap, host IP, overlay, QEMU
    infra_script = f'''
set -eu
workdir={shlex.quote(workdir)}
workdir=$(eval printf '%s' "$workdir")
mkdir -p "$workdir/run" "$workdir/overlays"
base="$workdir/images/openwrt-base.qcow2"
disk={disk}
pidfile={pidfile}

if [ ! -f "$base" ]; then
  printf 'OpenWrt base image missing. Run prepare-image first.\\n' >&2
  exit 1
fi

if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
  printf 'POC VM already running with pid %s\\n' "$(cat "$pidfile")"
  exit 0
fi

# Clean up old resources
sudo ip link del {DEBIAN_TAP} 2>/dev/null || true
sudo ip link del {POC_TAP} 2>/dev/null || true
sudo ip link del {POC_BRIDGE} 2>/dev/null || true

# Create bridge and tap
sudo ip link add name {POC_BRIDGE} type bridge
sudo ip link set {POC_BRIDGE} up
sudo ip addr add {POC_HOST_BRIDGE_IP} dev {POC_BRIDGE} 2>/dev/null || true

_subnet=$(ip -4 route show exact {POC_SUBNET} | grep -v "dev {POC_BRIDGE}" | head -1)
if [ -n "$_subnet" ]; then
  _dev=$(echo "$_subnet" | sed -n 's/.*dev \\([^ ]*\\).*/\\1/p')
  echo "Route conflict: moving {POC_SUBNET} from $_dev to {POC_BRIDGE}"
  sudo ip route del {POC_SUBNET} dev "$_dev" 2>/dev/null || true
fi

sudo ip tuntap add dev {POC_TAP} mode tap user "$USER"
sudo ip link set {POC_TAP} master {POC_BRIDGE}
sudo ip link set {POC_TAP} up

sudo iptables -C FORWARD -i {POC_BRIDGE} -j ACCEPT 2>/dev/null || sudo iptables -I FORWARD 1 -i {POC_BRIDGE} -j ACCEPT
sudo iptables -C FORWARD -o {POC_BRIDGE} -j ACCEPT 2>/dev/null || sudo iptables -I FORWARD 2 -o {POC_BRIDGE} -j ACCEPT
sudo iptables -t nat -C POSTROUTING -s {POC_SUBNET} ! -o {POC_BRIDGE} -j MASQUERADE 2>/dev/null || sudo iptables -t nat -A POSTROUTING -s {POC_SUBNET} ! -o {POC_BRIDGE} -j MASQUERADE

# Create overlay if needed
if [ ! -f "$disk" ]; then
  qemu-img create -f qcow2 -F qcow2 -b "$base" "$disk"
fi

# Start QEMU with serial/monitor Unix sockets
nohup qemu-system-x86_64 \
  -enable-kvm \
  -m 512 \
  -smp 1 \
  -nographic \
  -serial unix:"$workdir/run/serial.sock",server,nowait \
  -monitor unix:"$workdir/run/monitor.sock",server,nowait \
  -drive file="$disk",if=virtio,format=qcow2 \
  -netdev tap,id=lan,ifname={POC_TAP},script=no,downscript=no \
  -device virtio-net-pci,netdev=lan,mac={POC_OPENWRT_MAC} \
  >"$workdir/run/qemu.stdout" 2>"$workdir/run/qemu.stderr" &
printf '%s\\n' "$!" > "$pidfile"

printf 'Started POC OpenWrt VM pid=%s\\n' "$(cat "$pidfile")"
'''
    rc = _print_result(run_remote(host, quote_script(infra_script), timeout=120))
    if rc != 0:
        return rc

    # Step 2: provision VM via serial console
    print("Provisioning OpenWrt VM via serial console...")
    provision_script = _generate_provision_script(workdir)
    result = run_python_on_host(host, provision_script, timeout=120)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if result.returncode != 0:
        print("Serial console provisioning failed.", file=sys.stderr)
        return result.returncode

    # Step 3: verify SSH login
    print("Verifying SSH access to OpenWrt VM...")
    ssh_verify = (
        f"sshpass -p {POC_PASSWORD} ssh "
        f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-o ConnectTimeout=10 -o LogLevel=ERROR "
        f"root@{POC_GATEWAY} 'echo SSH_OK'"
    )
    rc = _print_result(run_remote(host, quote_script(ssh_verify), timeout=30))
    if rc != 0:
        print("SSH verification failed.", file=sys.stderr)
        return rc

    print("Starting Debian client VM...", flush=True)
    client_pidfile, client_disk = _client_paths(workdir)
    client_script = f'''
set -eu
workdir={shlex.quote(workdir)}
workdir=$(eval printf '%s' "$workdir")
client_pidfile={client_pidfile}
snapshot_opts={"" if not ephemeral_client else "-snapshot"}
client_disk={client_disk}

if [ -f "$client_pidfile" ] && kill -0 "$(cat "$client_pidfile")" 2>/dev/null; then
  printf 'Debian client VM already running with pid %s\\n' "$(cat "$client_pidfile")"
  exit 0
fi

if [ ! -f "$client_disk" ]; then
  printf 'Debian client image missing. Run prepare-debian first.\\n' >&2
  exit 1
fi

sudo ip tuntap add dev {DEBIAN_TAP} mode tap user "$USER"
sudo ip link set {DEBIAN_TAP} master {POC_BRIDGE}
sudo ip link set {DEBIAN_TAP} up

seed_iso="$workdir/images/seed.iso"
cdrom_opts=""
smbios_opts=""
snapshot_opts=""
if [ -f "$seed_iso" ]; then
  cdrom_opts="-cdrom $seed_iso"
  # ds=nocloud skips cloud-init's datasource probing (Ec2/Azure/...), which
  # otherwise delays sshd by ~2 minutes on every boot via cloud-init.service.
  smbios_opts="-smbios type=1,serial=ds=nocloud"
fi
nohup qemu-system-x86_64 \
  -enable-kvm \
  -m {DEBIAN_RAM} \
  -smp 2 \
  -nographic \
  $smbios_opts \
  -drive file="$client_disk",if=virtio \
  $cdrom_opts \
  $snapshot_opts \
  -netdev tap,id=client,ifname={DEBIAN_TAP},script=no,downscript=no \
  -device virtio-net-pci,netdev=client,mac={DEBIAN_MAC} \
  -serial unix:"$workdir/run/serial-client.sock",server,nowait \
  -monitor unix:"$workdir/run/monitor-client.sock",server,nowait \
  >"$workdir/run/qemu-client.stdout" 2>"$workdir/run/qemu-client.stderr" &
printf '%s\\n' "$!" > "$client_pidfile"

printf 'Started Debian client VM pid=%s\\n' "$(cat "$client_pidfile")"
'''
    try:
        rc = _print_result(run_remote(host, client_script, timeout=120))
    except Exception as e:
        print(f"Step 4 FAILED: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return 1
    if rc != 0:
        return rc

    # Step 4b: an already-provisioned client overlay boots straight to sshd
    # with its static IP — skip the serial-console provisioning entirely.
    # Serial (Step 5) is the fallback for a fresh overlay only.
    print("Step 4b: waiting for client SSH (provisioned overlay)...", flush=True)
    ssh_probe = (
        f"for i in $(seq 1 36); do "
        f"if sshpass -p {POC_PASSWORD} ssh -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR "
        f"root@{DEBIAN_CLIENT_IP} 'echo SSH_OK' 2>/dev/null | grep -q SSH_OK; then "
        f"echo CLIENT_SSH_READY; exit 0; fi; "
        f"sleep 5; done; exit 1"
    )
    probe_result = run_remote(host, quote_script(ssh_probe), timeout=300)
    client_provisioned = probe_result.returncode == 0
    if client_provisioned:
        print("Client SSH up — skipping serial provisioning.", flush=True)
    elif ephemeral_client:
        print("ERROR: --ephemeral-client requires an already-provisioned overlay "
              "(ephemeral writes would be discarded, so serial provisioning cannot "
              "run in this mode). Boot once without the flag, then retry.",
              file=sys.stderr)
        return 1

    if not client_provisioned:
        print("Step 5: provision Debian VM via serial console...", flush=True)
        debian_provision_script = _generate_debian_provision_script(workdir)
        result = run_python_on_host(host, debian_provision_script, timeout=600)
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if result.returncode != 0:
            print("Debian serial console provisioning failed.", file=sys.stderr)
            return result.returncode

        # Step 5b: inject SSH key and set root password.
        # chpasswd over the serial console gets corrupted by terminal escape
        # sequences, so we inject the host SSH key via base64, then set the
        # password through SSH (which avoids the serial console entirely).
        print("Injecting SSH key into Debian VM...", flush=True)
        ssh_key_script = _generate_ssh_key_inject_script(workdir)
        key_result = run_python_on_host(host, ssh_key_script, timeout=60)
        if key_result.returncode != 0:
            print(f"SSH key injection failed: {key_result.stderr}", file=sys.stderr)

        print("Setting root password via SSH...", flush=True)
        pw_script = (
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout=10 -o LogLevel=ERROR "
            f"root@{DEBIAN_CLIENT_IP} 'echo root:{POC_PASSWORD} | chpasswd'"
        )
        rc = _print_result(run_remote(host, quote_script(pw_script), timeout=30))
        if rc != 0:
            print("Warning: SSH password setup failed, continuing with key auth only.", file=sys.stderr)

    print("Step 6: verify SSH to Debian VM...", flush=True)
    ssh_verify_debian = (
        f"sshpass -p {POC_PASSWORD} ssh "
        f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-o ConnectTimeout=10 -o LogLevel=ERROR "
        f"root@{DEBIAN_CLIENT_IP} 'echo SSH_OK'"
    )
    rc = _print_result(run_remote(host, quote_script(ssh_verify_debian), timeout=30))
    if rc != 0:
        print("SSH verification to Debian VM failed.", file=sys.stderr)
        return rc

    # Step 7: add static DHCP lease for the VM so the TollGate backend
    # can resolve the client MAC via /tmp/dhcp.leases (its only lookup method).
    # The VM uses a static IP so no real DHCP lease exists otherwise.
    print("Adding static DHCP lease for client VM...")
    dhcp_lease_script = (
        f"sshpass -p {POC_PASSWORD} ssh "
        f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "
        f"root@{POC_GATEWAY} "
        f'"uci get dhcp.@host[0].mac 2>/dev/null | grep -q {DEBIAN_MAC} && exit 0; '
        f"uci add dhcp host; "
        f"uci set dhcp.@host[-1].mac={DEBIAN_MAC}; "
        f"uci set dhcp.@host[-1].ip={DEBIAN_CLIENT_IP}; "
        f"uci set dhcp.@host[-1].name=debian-vm; "
        f'uci commit dhcp; /etc/init.d/dnsmasq restart; echo DHCP_lease_added"'
    )
    rc = _print_result(run_remote(host, quote_script(dhcp_lease_script), timeout=30))
    if rc != 0:
        print("Warning: DHCP lease setup failed (non-fatal).", file=sys.stderr)

    print("\nPOC environment ready:")
    print(f"  OpenWrt VM: {POC_GATEWAY}")
    print(f"  Debian client VM: {DEBIAN_CLIENT_IP} (static, via {DEBIAN_TAP})")
    print(f"  Host bridge IP: {POC_HOST_BRIDGE_IP}")
    return 0


def provision_debian(args: argparse.Namespace) -> int:
    host = cast(str, args.host)
    client_ip = DEBIAN_CLIENT_IP

    print(f"Provisioning Debian client at {client_ip}...")
    ssh_opts = (
        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        "-o LogLevel=ERROR"
    )
    install_script = f'''
set +e
sshpass -p {POC_PASSWORD} ssh {ssh_opts} root@{client_ip} '
  apt update -qq && apt install -y -qq curl iputils-ping iproute2 chromium python3-pip
  pip3 install --break-system-packages playwright && playwright install chromium --with-deps
  chromium --version
  python3 -c "from playwright.sync_api import sync_playwright; print(\\"ok\\")"
'
'''
    return _print_result(run_remote(host, quote_script(install_script), timeout=600))


def stop_poc(args: argparse.Namespace) -> int:
    host = cast(str, args.host)
    workdir = cast(str, args.workdir)
    pidfile, _, _ = _poc_paths(workdir)
    client_pidfile, _ = _client_paths(workdir)
    script = f'''
set +e
workdir={shlex.quote(workdir)}
workdir=$(eval printf '%s' "$workdir")
pidfile={pidfile}
client_pidfile={client_pidfile}

for pf in "$client_pidfile" "$pidfile"; do
  if [ -f "$pf" ]; then
    p=$(cat "$pf")
    # ACPI shutdown first: a hard kill leaves a dirty qcow2, and the next
    # boot pays journal replay (minutes), which defeats the fast
    # SSH-first start path.
    mon=""
    case "$pf" in *debian-client*) mon="$workdir/run/monitor-client.sock";; *) mon="$workdir/run/monitor.sock";; esac
    if [ -S "$mon" ]; then
      python3 - "$mon" <<'PYMON' 2>/dev/null || true
import socket, sys
c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
c.settimeout(3)
c.connect(sys.argv[1])
c.sendall(b"system_powerdown\n")
c.close()
PYMON
      for _w in $(seq 1 20); do
        kill -0 "$p" 2>/dev/null || break
        sleep 1
      done
    fi
    kill "$p" 2>/dev/null || true
    sleep 1
    kill -9 "$p" 2>/dev/null || true
    rm -f "$pf"
  fi
done

ps aux | grep 'qemu-system.*drive file=' | grep -v grep | awk '{{print $2}}' | xargs kill 2>/dev/null || true
sleep 1

sudo ip link del {DEBIAN_TAP} 2>/dev/null || true
sudo ip link del {POC_TAP} 2>/dev/null || true
sudo ip link del {POC_BRIDGE} 2>/dev/null || true

printf 'Stopped POC virtual lab\\n'
'''
    return _print_result(run_remote(host, quote_script(script), timeout=60))


def status_poc(args: argparse.Namespace) -> int:
    host = cast(str, args.host)
    workdir = cast(str, args.workdir)
    pidfile, _serial_sock, disk = _poc_paths(workdir)
    client_pidfile, client_disk = _client_paths(workdir)
    script = f'''
set +e
workdir={shlex.quote(workdir)}
workdir=$(eval printf '%s' "$workdir")
pidfile={pidfile}
disk={disk}
client_pidfile={client_pidfile}
client_disk={client_disk}

printf '== OpenWrt QEMU ==\\n'
if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
  printf 'running pid=%s\\n' "$(cat "$pidfile")"
else
  printf 'not running\\n'
fi

printf '\\n== Debian client QEMU ==\\n'
if [ -f "$client_pidfile" ] && kill -0 "$(cat "$client_pidfile")" 2>/dev/null; then
  printf 'running pid=%s\\n' "$(cat "$client_pidfile")"
else
  printf 'not running\\n'
fi

printf '\\n== network ==\\n'
ip link show {POC_BRIDGE} 2>/dev/null || true
ip link show {POC_TAP} 2>/dev/null || true
ip link show {DEBIAN_TAP} 2>/dev/null || true
ip addr show {POC_BRIDGE} 2>/dev/null | grep -F 'inet ' || true

printf '\\n== DHCP leases (from OpenWrt) ==\\n'
sshpass -p {POC_PASSWORD} ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR root@{POC_GATEWAY} "cat /tmp/dhcp.leases" 2>/dev/null || printf 'unavailable\n'

printf '\\n== Debian client (static) ==\\n'
printf 'Static IP: {DEBIAN_CLIENT_IP}\\n'
if sshpass -p {POC_PASSWORD} ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=3 -o LogLevel=ERROR root@{DEBIAN_CLIENT_IP} "echo reachable" 2>/dev/null; then
  printf 'SSH: reachable\\n'
else
  printf 'SSH: unreachable\\n'
fi

printf '\\n== disk ==\\n'
if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
  ls -lh "$disk" 2>/dev/null || true
  printf 'qcow2 is in use by the running VM; skipping qemu-img info\\n'
elif [ -f "$disk" ]; then
  qemu-img info "$disk"
else
  printf 'missing %s\\n' "$disk"
fi

printf '\\n== serial console ==\\n'
if [ -S "$workdir/run/serial.sock" ]; then
  printf 'serial socket: $workdir/run/serial.sock (ready)\\n'
else
  printf 'serial socket not found\\n'
fi
'''
    return _print_result(run_remote(host, quote_script(script), timeout=60))


def debug_poc(args: argparse.Namespace) -> int:
    host = cast(str, args.host)
    workdir = cast(str, args.workdir)
    pidfile, _serial_sock, disk = _poc_paths(workdir)
    script = f'''
set +e
workdir={shlex.quote(workdir)}
workdir=$(eval printf '%s' "$workdir")

printf '===== VIRTUAL LAB DEBUG =====\\n\\n'

printf '== 1. QEMU processes ==\\n'
ps aux | grep qemu | grep -v grep || printf 'No QEMU processes\\n'

printf '\\n== 2. Network (bridge/tap) ==\\n'
ip addr show {POC_BRIDGE} 2>/dev/null || printf '{POC_BRIDGE} missing\\n'
ip link show {POC_TAP} 2>/dev/null | head -2 || printf '{POC_TAP} missing\\n'
ip link show {DEBIAN_TAP} 2>/dev/null | head -2 || printf '{DEBIAN_TAP} missing\\n'
bridge link show | grep -E '{POC_TAP}|{DEBIAN_TAP}' || printf 'No taps bridged\\n'

printf '\\n== 3. IP connectivity ==\\n'
printf 'Pinging {POC_GATEWAY}... '
ping -c 1 -W 2 {POC_GATEWAY} 2>/dev/null && printf 'OK\\n' || printf 'FAIL\\n'
printf 'Pinging {DEBIAN_CLIENT_IP}... '
ping -c 1 -W 2 {DEBIAN_CLIENT_IP} 2>/dev/null && printf 'OK\\n' || printf 'FAIL\\n'

printf '\\n== 4. OpenWrt VM (SSH) ==\\n'
VM_OK=false
if sshpass -p {POC_PASSWORD} ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR root@{POC_GATEWAY} '
  printf "  hostname: "; hostname
  printf "  uptime:   "; uptime
  printf "  load:     "; cat /proc/loadavg
  printf "  memory:\\n"; free -m | head -2
  printf "  br-lan IP:\\n"; ip addr show br-lan | grep "inet "
  printf "  routes:\\n"; ip route show
  printf "  DNS:\\n"; cat /etc/resolv.conf
' 2>/dev/null; then
  VM_OK=true
else
  printf '  SSH to {POC_GATEWAY} FAILED\\n'
fi

if [ "$VM_OK" = true ]; then
  printf '\\n== 5. OpenWrt services ==\\n'
  sshpass -p {POC_PASSWORD} ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR root@{POC_GATEWAY} '
    printf "  TollGate process:\\n"
    ps | grep tollgate | grep -v grep || printf "    NOT RUNNING\\n"
    printf "  TollGate /health:\\n"
    curl -s --connect-timeout 3 http://127.0.0.1:2121/health 2>/dev/null || printf "    NO RESPONSE\\n"
    printf "\\n  ndsctl status:\\n"
    ndsctl status 2>/dev/null | head -10 || printf "    nodogsplash not running\\n"
    printf "\\n  DHCP leases:\\n"
    cat /tmp/dhcp.leases 2>/dev/null || printf "    none\\n"
    printf "\\n  Firewall (tollgate rules):\\n"
    iptables -L -n 2>/dev/null | grep -i "tol\\|nds\\|2121" || printf "    no tollgate rules found\\n"
    printf "\\n  Recent TollGate logs (last 20):\\n"
    tail -20 /tmp/tollgate-debug.log 2>/dev/null || logread -e tollgate 2>/dev/null | tail -10 || printf "    no logs\\n"
  ' 2>/dev/null
fi

printf '\\n== 6. Debian client (SSH) ==\\n'
if sshpass -p {POC_PASSWORD} ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR root@{DEBIAN_CLIENT_IP} '
  printf "  hostname: "; hostname
  printf "  IP:\\n"; ip addr show | grep "inet " | grep -v 127
  printf "  gateway:\\n"; ip route show default
  printf "  DNS:\\n"; cat /etc/resolv.conf
  printf "  ping gateway: "; ping -c 1 -W 2 {POC_GATEWAY} 2>/dev/null && printf "OK\\n" || printf "FAIL\\n"
  printf "  ping internet: "; ping -c 1 -W 2 8.8.8.8 2>/dev/null && printf "OK\\n" || printf "FAIL\\n"
  printf "  captive portal: "; curl -s -o /dev/null -w "%{{http_code}}" --connect-timeout 5 http://captiveportal.example.com/ 2>/dev/null; printf "\\n"
' 2>/dev/null; then
  :
else
  printf '  SSH to {DEBIAN_CLIENT_IP} FAILED\\n'
fi

printf '\\n== 7. NAT/forwarding rules ==\\n'
sudo iptables -t nat -L POSTROUTING -n 2>/dev/null | grep -E "10.99.99|MASQ" || printf '  no NAT rules\\n'
sudo iptables -L FORWARD -n 2>/dev/null | head -5

printf '\\n===== END DEBUG =====\\n'
'''
    return _print_result(run_remote(host, quote_script(script), timeout=60))


def smoke_poc(args: argparse.Namespace) -> int:
    host = cast(str, args.host)
    timeout = cast(int, args.timeout)
    script = f'''
set +e
deadline=$((SECONDS + {timeout}))
while [ "$SECONDS" -lt "$deadline" ]; do
  if sshpass -p {POC_PASSWORD} ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR root@{DEBIAN_CLIENT_IP} "ping -c 1 -W 2 {POC_GATEWAY}" >/dev/null 2>&1; then
    printf 'PASS: Debian client {DEBIAN_CLIENT_IP} reached OpenWrt gateway {POC_GATEWAY}\\n'
    exit 0
  fi
  sleep 2
done
printf 'FAIL: Debian client {DEBIAN_CLIENT_IP} could not reach OpenWrt gateway {POC_GATEWAY}\\n' >&2
exit 1
'''
    return _print_result(run_remote(host, quote_script(script), timeout=timeout + 10))


def run_poc(args: argparse.Namespace) -> int:
    rc = prepare_image(args)
    if rc != 0:
        return rc
    rc = start_poc(args)
    if rc != 0:
        return rc
    return smoke_poc(args)


def run_reseller_scenarios(args: argparse.Namespace) -> int:
    host = cast(str, args.host)
    results_dir = cast(str, args.results_dir)
    backend = cast(str, args.backend)
    secondary_host = cast(str | None, args.secondary_router_host)
    secondary_port = cast(str | None, args.secondary_router_port)
    if not secondary_host:
        print(
            "ERROR: run-reseller-scenarios requires --secondary-router-host. "
            "The single-router POC can smoke-test the harness, but real reseller coverage needs a seller router.",
            file=sys.stderr,
        )
        return 2

    env_parts = [
        f"TOLLGATE_LUCI_PASSWORD={shlex.quote(POC_PASSWORD)}",
        f"TOLLGATE_SSH_PASSWORD={shlex.quote(POC_PASSWORD)}",
        f"TOLLGATE_SSH_HOST={shlex.quote(POC_GATEWAY)}",
        f"TOLLGATE_SSH_JUMP_HOST={shlex.quote(host)}",
        "TOLLGATE_CLIENT_TYPE=container",
        "TOLLGATE_VIRTUAL_LAB=1",
        "TOLLGATE_ENABLE_RESELLER_SCENARIOS=1",
        f"TOLLGATE_CLIENT_IP={shlex.quote(DEBIAN_CLIENT_IP)}",
        f"TOLLGATE_CLIENT_MAC={shlex.quote(DEBIAN_MAC)}",
        f"TOLLGATE_BACKEND={shlex.quote(backend)}",
    ]
    env_parts.append(f"TOLLGATE_SECONDARY_ROUTER_HOST={shlex.quote(secondary_host)}")
    if secondary_port:
        env_parts.append(f"TOLLGATE_SECONDARY_ROUTER_PORT={shlex.quote(secondary_port)}")

    command = " ".join(env_parts) + (
        " python3 -m pytest tests/scenarios/test_reseller_mode.py "
        f"--backend={shlex.quote(backend)} --client=container --results {shlex.quote(results_dir)} -v"
    )
    return _print_result(run_local(["bash", "-lc", command], timeout=600))


def _poc_disk_path(workdir: str) -> str:
    return os.path.join(workdir, "overlays", "tollgate-poc.qcow2")


def _vm_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", "tollgate-poc.qcow2"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def snapshot_create(args: argparse.Namespace) -> int:
    disk = _poc_disk_path(args.workdir)
    if not os.path.isfile(disk):
        print(f"ERROR: POC disk not found at {disk}")
        return 1
    if _vm_running():
        print("ERROR: POC VM is running. Stop it first: python3 scripts/virtual-lab.py stop-poc")
        return 1
    r = subprocess.run(["qemu-img", "snapshot", "-c", args.name, disk], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"ERROR: {r.stderr.strip()}")
        return 1
    print(f"Snapshot '{args.name}' created.")
    return 0


def snapshot_list(args: argparse.Namespace) -> int:
    disk = _poc_disk_path(args.workdir)
    if not os.path.isfile(disk):
        print(f"ERROR: POC disk not found at {disk}")
        return 1
    r = subprocess.run(["qemu-img", "snapshot", "-l", disk], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"ERROR: {r.stderr.strip()}")
        return 1
    print(r.stdout.strip() if r.stdout.strip() else "No snapshots.")
    return 0


def snapshot_restore(args: argparse.Namespace) -> int:
    disk = _poc_disk_path(args.workdir)
    if not os.path.isfile(disk):
        print(f"ERROR: POC disk not found at {disk}")
        return 1
    if _vm_running():
        print("ERROR: POC VM is running. Stop it first.")
        return 1
    r = subprocess.run(["qemu-img", "snapshot", "-a", args.name, disk], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"ERROR: {r.stderr.strip()}")
        return 1
    print(f"Restored to snapshot '{args.name}'. Start VM with: start-poc")
    return 0


def snapshot_delete(args: argparse.Namespace) -> int:
    disk = _poc_disk_path(args.workdir)
    if not os.path.isfile(disk):
        print(f"ERROR: POC disk not found at {disk}")
        return 1
    r = subprocess.run(["qemu-img", "snapshot", "-d", args.name, disk], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"ERROR: {r.stderr.strip()}")
        return 1
    print(f"Snapshot '{args.name}' deleted.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the TollGate virtual lab")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="Check host readiness")
    _ = doctor_parser.add_argument("--host", default="218", help="SSH host for the Ubuntu lab machine")
    doctor_parser.set_defaults(func=doctor)

    deps_parser = subparsers.add_parser("install-deps", help="Install host dependencies via apt")
    _ = deps_parser.add_argument("--host", default="218", help="SSH host for the Ubuntu lab machine")
    deps_parser.set_defaults(func=install_deps)

    image_parser = subparsers.add_parser("prepare-image", help="Download and prepare OpenWrt x86 images")
    _ = image_parser.add_argument("--host", default="218", help="SSH host for the Ubuntu lab machine")
    _ = image_parser.add_argument("--openwrt-version", default=DEFAULT_OPENWRT_VERSION)
    _ = image_parser.add_argument("--workdir", default=DEFAULT_WORKDIR)
    image_parser.set_defaults(func=prepare_image)

    start_parser = subparsers.add_parser("start-poc", help="Start OpenWrt VM and Debian client VM")
    _ = start_parser.add_argument("--host", default="218", help="SSH host for the Ubuntu lab machine")
    _ = start_parser.add_argument("--workdir", default=DEFAULT_WORKDIR)
    _ = start_parser.add_argument("--ephemeral-client", action="store_true",
                                  help="Run the Debian client with QEMU -snapshot: writes are discarded "
                                       "on stop-poc, guaranteeing a pristine client per cycle. Requires an "
                                       "already-provisioned overlay (provision first without this flag); "
                                       "provision_debian changes (playwright/chromium) will not persist.")
    start_parser.set_defaults(func=start_poc)

    stop_parser = subparsers.add_parser("stop-poc", help="Stop the POC VMs and clean up")
    _ = stop_parser.add_argument("--host", default="218", help="SSH host for the Ubuntu lab machine")
    _ = stop_parser.add_argument("--workdir", default=DEFAULT_WORKDIR)
    stop_parser.set_defaults(func=stop_poc)

    status_parser = subparsers.add_parser("status-poc", help="Show POC VM status")
    _ = status_parser.add_argument("--host", default="218", help="SSH host for the Ubuntu lab machine")
    _ = status_parser.add_argument("--workdir", default=DEFAULT_WORKDIR)
    status_parser.set_defaults(func=status_poc)

    smoke_parser = subparsers.add_parser("smoke-poc", help="Verify Debian client VM reaches OpenWrt gateway")
    _ = smoke_parser.add_argument("--host", default="218", help="SSH host for the Ubuntu lab machine")
    _ = smoke_parser.add_argument("--timeout", type=int, default=120)
    smoke_parser.set_defaults(func=smoke_poc)

    debug_parser = subparsers.add_parser("debug-poc", help="Comprehensive mid-flight debug of the virtual lab")
    _ = debug_parser.add_argument("--host", default="218", help="SSH host for the Ubuntu lab machine")
    _ = debug_parser.add_argument("--workdir", default=DEFAULT_WORKDIR)
    debug_parser.set_defaults(func=debug_poc)

    reseller_parser = subparsers.add_parser(
        "run-reseller-scenarios",
        help="Run virtualizable reseller-mode scenario tests against the virtual lab",
    )
    _ = reseller_parser.add_argument("--host", default="218", help="SSH host for the Ubuntu lab machine")
    _ = reseller_parser.add_argument("--results-dir", default="results/virtual-reseller-scenarios")
    _ = reseller_parser.add_argument("--backend", default="go", choices=list(BACKEND_CHOICES_CLI))
    _ = reseller_parser.add_argument("--secondary-router-host", default=None)
    _ = reseller_parser.add_argument("--secondary-router-port", default=None)
    reseller_parser.set_defaults(func=run_reseller_scenarios)

    prepare_debian_parser = subparsers.add_parser("prepare-debian", help="Download Debian nocloud image and create overlay")
    _ = prepare_debian_parser.add_argument("--host", default="218", help="SSH host for the Ubuntu lab machine")
    _ = prepare_debian_parser.add_argument("--workdir", default=DEFAULT_WORKDIR)
    prepare_debian_parser.set_defaults(func=prepare_debian)

    provision_debian_parser = subparsers.add_parser("provision-debian", help="Install Chromium + Playwright in Debian client VM")
    _ = provision_debian_parser.add_argument("--host", default="218", help="SSH host for the Ubuntu lab machine")
    _ = provision_debian_parser.add_argument("--workdir", default=DEFAULT_WORKDIR)
    provision_debian_parser.set_defaults(func=provision_debian)

    poc_parser = subparsers.add_parser("poc", help="Prepare image, start VMs, and run smoke proof")
    _ = poc_parser.add_argument("--host", default="218", help="SSH host for the Ubuntu lab machine")
    _ = poc_parser.add_argument("--openwrt-version", default=DEFAULT_OPENWRT_VERSION)
    _ = poc_parser.add_argument("--workdir", default=DEFAULT_WORKDIR)
    _ = poc_parser.add_argument("--timeout", type=int, default=120)
    poc_parser.set_defaults(func=run_poc)

    snap_parser = subparsers.add_parser("snapshot", help="QEMU snapshot management for the POC VM")
    snap_sub = snap_parser.add_subparsers(dest="snapshot_command", required=True)
    snap_create = snap_sub.add_parser("create", help="Create a snapshot (VM must be stopped)")
    _ = snap_create.add_argument("name", help="Snapshot name")
    _ = snap_create.add_argument("--host", default="218")
    _ = snap_create.add_argument("--workdir", default=DEFAULT_WORKDIR)
    snap_create.set_defaults(func=snapshot_create)
    snap_list = snap_sub.add_parser("list", help="List snapshots")
    _ = snap_list.add_argument("--host", default="218")
    _ = snap_list.add_argument("--workdir", default=DEFAULT_WORKDIR)
    snap_list.set_defaults(func=snapshot_list)
    snap_restore = snap_sub.add_parser("restore", help="Restore VM to snapshot (VM must be stopped)")
    _ = snap_restore.add_argument("name", help="Snapshot name")
    _ = snap_restore.add_argument("--host", default="218")
    _ = snap_restore.add_argument("--workdir", default=DEFAULT_WORKDIR)
    snap_restore.set_defaults(func=snapshot_restore)
    snap_delete = snap_sub.add_parser("delete", help="Delete a snapshot")
    _ = snap_delete.add_argument("name", help="Snapshot name")
    _ = snap_delete.add_argument("--host", default="218")
    _ = snap_delete.add_argument("--workdir", default=DEFAULT_WORKDIR)
    snap_delete.set_defaults(func=snapshot_delete)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if hasattr(args, "workdir") and args.workdir:
        if host_is_remote(getattr(args, "host", "")):
            pass
        else:
            args.workdir = os.path.expanduser(args.workdir)
    func = cast(Callable[[argparse.Namespace], int], args.func)
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
