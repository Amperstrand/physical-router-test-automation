#!/usr/bin/env python3
"""Bake a new GCP snapshot with all cloud lab dependencies pre-installed.

Creates a temporary VM from the current snapshot, installs gh CLI, Python venv,
cashu venv, and pre-provisions the OpenWrt base image. Then snapshots the disk
and cleans up.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.cloud_lab.constants import (
    DEFAULT_DISK_SIZE_GB,
    DEFAULT_MACHINE_TYPE,
    DEFAULT_ZONE,
    OPENWRT_IP,
    SNAPSHOT_NAME,
    SUITE_REPO_URL,
    VIRT_LAB_PASSWORD,
    VIRT_LAB_WORKDIR,
)

_OPENWRT_VERSION = "24.10.1"
_DEBIAN_IMAGE = "debian-12-nocloud-amd64.qcow2"
_DEBIAN_IMAGE_URL = f"https://cloud.debian.org/images/cloud/bookworm/latest/{_DEBIAN_IMAGE}"
from lib.cloud_lab.gcp import (
    ensure_firewall_rules,
    get_project,
)


def _run_gcloud(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    markers = (
        "NameResolutionError", "Failed to resolve", "ConnectionError",
        "Max retries exceeded", "Network is unreachable", "timed out",
    )
    last = subprocess.CompletedProcess(args=["gcloud"], returncode=1, stdout="", stderr="")
    for attempt in range(1, 4):
        last = subprocess.run(
            ["gcloud", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        combined = f"{last.stderr}\n{last.stdout}"
        if last.returncode == 0 or not any(m in combined for m in markers):
            return last
        if attempt < 3:
            print(f"WARNING: transient gcloud failure, retrying ({attempt}/3): {last.stderr[:200]}", file=sys.stderr)
            time.sleep(5 * attempt)
    return last


def _gcloud_ssh(vm_name: str, remote_cmd: str, zone: str, project: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    wrapped = f"sudo HOME=/root bash -c {shlex.quote(remote_cmd)}"
    cmd = [
        "gcloud", "compute", "ssh", vm_name,
        f"--project={project}", f"--zone={zone}",
        "--command", wrapped,
        "--ssh-flag=-o StrictHostKeyChecking=no",
        "--ssh-flag=-o UserKnownHostsFile=/dev/null",
        "--ssh-flag=-o ConnectTimeout=10",
        "--quiet",
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _wait_vm_ssh(vm_name: str, zone: str, project: str, timeout: int = 180) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _gcloud_ssh(vm_name, "echo SSH_OK", zone, project, timeout=60)
        if r.returncode == 0 and "SSH_OK" in r.stdout:
            return True
        elapsed = int(time.time() - deadline + timeout)
        print(f"  Waiting for SSH... ({elapsed}s elapsed)")
        time.sleep(10)
    return False


def _step(step_num: int, total: int, name: str) -> None:
    print(f"\n[{step_num}/{total}] {name}")
    print("-" * 60)


def _auto_snapshot_name() -> str:
    base = SNAPSHOT_NAME
    if base.startswith("tollgate-runner-baked-v"):
        version_str = base[len("tollgate-runner-baked-v"):]
        try:
            version = int(version_str)
            return f"tollgate-runner-baked-v{version + 1}"
        except ValueError:
            pass
    return f"{base}-new"


def cmd_bake(args: argparse.Namespace) -> int:
    zone = cast(str, args.zone)
    machine_type = cast(str, args.machine_type)
    base_snapshot = cast(str, args.base_snapshot)
    snapshot_name = cast(str, args.snapshot_name) or _auto_snapshot_name()
    disk_size_gb = cast(int, args.disk_size)
    project = get_project()

    total_steps = 13
    vm_name = f"tollgate-bake-{int(time.time())}"

    print("Bake configuration:")
    print(f"  Base snapshot:  {base_snapshot}")
    print(f"  New snapshot:   {snapshot_name}")
    print(f"  Project:        {project}")
    print(f"  Zone:           {zone}")
    print(f"  Machine type:   {machine_type}")
    print(f"  Temp VM name:   {vm_name}")

    # Step 1: Create temp VM (from base snapshot, or from a stock Debian image
    # with --from-scratch when no base snapshot exists — the v17-loss case).
    from_scratch = bool(getattr(args, "from_scratch", False))
    debian_overlay = str(getattr(args, "debian_overlay", "") or "")
    if from_scratch:
        _step(1, total_steps, "Creating temporary VM from stock Debian 12 image")
    else:
        _step(1, total_steps, "Creating temporary VM from base snapshot")
    t0 = time.monotonic()
    ensure_firewall_rules(project)
    create_args = [
        "compute", "instances", "create", vm_name,
        f"--project={project}", f"--zone={zone}",
        f"--machine-type={machine_type}",
        f"--boot-disk-size={disk_size_gb}GB",
        "--enable-nested-virtualization",
        "--min-cpu-platform=Intel Cascade Lake",
        "--tags=tollgate-runner",
    ]
    if from_scratch:
        create_args += [
            "--image-family=debian-12",
            "--image-project=debian-cloud",
        ]
    else:
        create_args.append(f"--source-snapshot={base_snapshot}")
    r = _run_gcloud(create_args, timeout=300)
    if r.returncode != 0:
        print(f"ERROR: Failed to create VM: {r.stderr}", file=sys.stderr)
        return 1
    print(f"  VM created in {time.monotonic() - t0:.1f}s")

    try:
        # Step 2: Wait for SSH
        _step(2, total_steps, "Waiting for SSH access")
        t0 = time.monotonic()
        if not _wait_vm_ssh(vm_name, zone, project):
            print("ERROR: SSH not available after 180s", file=sys.stderr)
            return 1
        print(f"  SSH ready in {time.monotonic() - t0:.1f}s")

        if from_scratch and debian_overlay:
            # A stock image has no pre-provisioned Debian client overlay; upload a
            # known-good one (static 10.99.99.100 — matches the cloud topology) so
            # the Playwright pre-bake step reuses it like a base snapshot would.
            import os as _os
            if not _os.path.isfile(debian_overlay):
                print(f"ERROR: --debian-overlay not found: {debian_overlay}", file=sys.stderr)
                return 1
            _step(2, total_steps, f"Uploading Debian client overlay ({debian_overlay})")
            # gcloud scp writes as the login user; the lab tree lives under /root
            # (the worker exports HOME=/root). Land in /tmp, then sudo-move.
            overlay_tmp = "/tmp/tollgate-debian-overlay.qcow2"
            r = _run_gcloud([
                "compute", "scp", debian_overlay,
                f"{vm_name}:{overlay_tmp}",
                f"--project={project}", f"--zone={zone}",
                "--quiet", "--compress",
            ], timeout=1800)
            if r.returncode != 0:
                print(f"ERROR: overlay upload failed: {r.stderr}", file=sys.stderr)
                return 1
            mv = _gcloud_ssh(
                vm_name,
                f"mkdir -p /root/tollgate-virtual-lab/overlays && "
                f"mv {overlay_tmp} /root/tollgate-virtual-lab/overlays/debian-client.qcow2 && "
                "echo OVERLAY_MOVED",
                zone, project,
            )
            if "OVERLAY_MOVED" not in (mv.stdout or ""):
                print(f"ERROR: overlay move failed: {mv.stderr or mv.stdout}", file=sys.stderr)
                return 1

        openwrt_archive = str(getattr(args, "openwrt_archive", "") or "")
        if from_scratch and openwrt_archive:
            owrt_gz = f"openwrt-{_OPENWRT_VERSION}-x86-64-generic-ext4-combined.img.gz"
            r = _run_gcloud([
                "compute", "scp", openwrt_archive,
                f"{vm_name}:/tmp/{owrt_gz}",
                f"--project={project}", f"--zone={zone}",
                "--quiet", "--compress",
            ], timeout=600)
            if r.returncode != 0:
                print(f"ERROR: openwrt archive upload failed: {r.stderr}", file=sys.stderr)
                return 1
            mv2 = _gcloud_ssh(
                vm_name,
                f"mkdir -p {VIRT_LAB_WORKDIR}/images && "
                f"mv /tmp/{owrt_gz} {VIRT_LAB_WORKDIR}/images/{owrt_gz} && "
                "echo OWRT_MOVED",
                zone, project,
            )
            if "OWRT_MOVED" not in (mv2.stdout or ""):
                print(f"ERROR: openwrt archive move failed: {mv2.stderr or mv2.stdout}", file=sys.stderr)
                return 1

        if from_scratch:
            # The old base snapshot carried these; a stock Debian image does not.
            _step(2, total_steps, "Installing qemu/genisoimage (stock-image bootstrap)")
            r = _gcloud_ssh(
                vm_name,
                "apt-get update -qq && "
                "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
                "qemu-utils qemu-system-x86 genisoimage >/dev/null && "
                "echo QEMU_BOOTSTRAP_OK",
                zone, project, timeout=600,
            )
            if "QEMU_BOOTSTRAP_OK" not in (r.stdout or ""):
                print(f"ERROR: qemu bootstrap failed: {r.stderr or r.stdout}", file=sys.stderr)
                return 1

        # Step 3: Download base images (OpenWrt + Debian)
        _step(3, total_steps, "Downloading OpenWrt and Debian base images")
        t0 = time.monotonic()
        workdir = VIRT_LAB_WORKDIR
        owrt_img = f"openwrt-{_OPENWRT_VERSION}-x86-64-generic-ext4-combined.img"
        owrt_gz = f"{owrt_img}.gz"
        owrt_url = f"https://downloads.openwrt.org/releases/{_OPENWRT_VERSION}/targets/x86/64/{owrt_gz}"
        images_cmd = (
            f"mkdir -p {workdir}/images && cd {workdir}/images && "
            f"rm -f openwrt-base.qcow2 && "
            f"[ -f {owrt_gz} ] || curl -fL --retry 4 --retry-delay 3 -o {owrt_gz} {owrt_url} && "
            f"[ -f {owrt_img} ] || (gzip -d < {owrt_gz} > {owrt_img} || [ -f {owrt_img} ]) && "
            f"qemu-img convert -f raw -O qcow2 {owrt_img} openwrt-base.qcow2 && "
            f"qemu-img resize openwrt-base.qcow2 2G && "
            f"if [ ! -f {_DEBIAN_IMAGE} ]; then "
            f"  curl -fL --retry 4 --retry-delay 3 -o {_DEBIAN_IMAGE} {_DEBIAN_IMAGE_URL} || "
            "  echo 'NOTE: debian base download failed (ok when a client overlay was uploaded)'; "
            f"fi && "
            "echo IMAGES_OK"
        )
        r = _gcloud_ssh(vm_name, images_cmd, zone, project, timeout=600)
        if r.returncode != 0 or "IMAGES_OK" not in (r.stdout or ""):
            print(f"ERROR: Image step failed rc={r.returncode} stderr: {(r.stderr or '')[:500]} stdout: {(r.stdout or '')[-800:]}", file=sys.stderr)
            return 1
        print(f"  Images ready in {time.monotonic() - t0:.1f}s")

        # Step 4: Install GitHub and Google Cloud CLIs
        _step(4, total_steps, "Installing GitHub and Google Cloud CLIs")
        t0 = time.monotonic()
        gh_install_cmd = (
            "apt-get update -qq && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq wget curl apt-transport-https ca-certificates gnupg >/dev/null && "
            "mkdir -p -m 755 /etc/apt/keyrings && "
            "if ! command -v gh >/dev/null 2>&1; then "
            "  wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg > /etc/apt/keyrings/githubcli-archive-keyring.gpg && "
            "  chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && "
            '  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] '
            'https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list; '
            "fi && "
            "if ! command -v gcloud >/dev/null 2>&1; then "
            "  curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor --yes -o /usr/share/keyrings/cloud.google.gpg && "
            '  echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" '
            "> /etc/apt/sources.list.d/google-cloud-sdk.list; "
            "fi && "
            "apt-get update -qq && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gh google-cloud-cli socat >/dev/null && "
            "command -v gh >/dev/null && command -v gcloud >/dev/null && echo CLI_INSTALLED_OK"
        )
        r = _gcloud_ssh(vm_name, gh_install_cmd, zone, project, timeout=180)
        if r.returncode != 0 or "CLI_INSTALLED_OK" not in (r.stdout or ""):
            print(f"WARNING: CLI install may have failed: {r.stderr[:300]}", file=sys.stderr)
        print(f"  Done in {time.monotonic() - t0:.1f}s")

        # Step 5: Clone repo and create Python venv
        _step(5, total_steps, "Creating Python venv with test dependencies")
        t0 = time.monotonic()
        venv_cmd = (
            "apt-get update -qq && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv git >/dev/null 2>&1 || true; "
            f"if [ -d /opt/tollgate-venv ] && /opt/tollgate-venv/bin/python3 -c 'import pytest; print(\"VENV_OK\")' 2>/dev/null; then "
            "echo 'Python venv already present'; exit 0; fi; "
            f"rm -rf /opt/tollgate-test && "
            f"git clone --depth 50 {SUITE_REPO_URL} /opt/tollgate-test && "
            "rm -rf /opt/tollgate-venv && "
            "python3 -m venv /opt/tollgate-venv && "
            "/opt/tollgate-venv/bin/pip install -q -r /opt/tollgate-test/requirements.txt && "
            "/opt/tollgate-venv/bin/pip install -q git+https://github.com/Amperstrand/nostr-publish-file-metadata-action.git && "
            "/opt/tollgate-venv/bin/python3 -c 'import pytest; print(\"VENV_OK\")'"
        )
        r = _gcloud_ssh(vm_name, venv_cmd, zone, project, timeout=300)
        if r.returncode != 0 or "VENV_OK" not in (r.stdout or ""):
            print(f"WARNING: Python venv creation may have failed: {r.stderr[:300]}", file=sys.stderr)
        print(f"  Done in {time.monotonic() - t0:.1f}s")

        # Step 6: Create cashu venv
        _step(6, total_steps, "Creating cashu CLI venv")
        t0 = time.monotonic()
        cashu_cmd = (
            "if [ -x /opt/cashu-venv/bin/cashu ] && /opt/cashu-venv/bin/cashu --version >/dev/null 2>&1; then "
            "echo 'Cashu already present'; exit 0; fi; "
            "rm -rf /opt/cashu-venv && "
            "python3 -m venv /opt/cashu-venv && "
            "/opt/cashu-venv/bin/pip install -q --upgrade pip && "
            "/opt/cashu-venv/bin/pip install -q cashu 'marshmallow<4' && "
            # Patch ALL known missing-field issues in nutshell models:
            # 1. MintKeyset.active (NUT-02 field missing on some public mints)
            # 2. KeysResponse validation (same root cause)
            "MODELS=$(/opt/cashu-venv/bin/python3 -c 'import cashu.core.models; print(cashu.core.models.__file__)') && "
            "sed -i 's/    active: bool$/    active: bool = True/' \"$MODELS\" && "
            "sed -i 's/    active: Optional\\[bool\\] = None/    active: Optional[bool] = True/' \"$MODELS\" && "
            # Also patch the KeysResponse keyset type if it uses a strict validator
            "NUTS02=$(/opt/cashu-venv/bin/python3 -c 'import cashumint; print(cashumint.__path__[0])' 2>/dev/null)/nuts/nut02.py && "
            "[ -f \"$NUTS02\" ] && sed -i 's/active: bool$/active: bool = True/' \"$NUTS02\" 2>/dev/null || true && "
            "test -x /opt/cashu-venv/bin/cashu && echo CASHU_OK"
        )
        r = _gcloud_ssh(vm_name, cashu_cmd, zone, project, timeout=300)
        if r.returncode != 0 or "CASHU_OK" not in (r.stdout or ""):
            print(f"WARNING: Cashu CLI install may have failed: {r.stderr[:300]}", file=sys.stderr)
        print(f"  Done in {time.monotonic() - t0:.1f}s")

        # Step 6b: Download CDK mintd and cdk-cli binaries
        _step(7, total_steps, "Downloading CDK mint binaries")
        t0 = time.monotonic()
        cdk_ver = os.environ.get("CDK_VER", "0.18.0")
        cdk_cmd = (
            "mkdir -p /opt/cdk-mintd && "
            f"CDK_VER={cdk_ver} && "
            "if [ -x /opt/cdk-mintd/cdk-mintd ]; then echo 'cdk-mintd cached'; else "
            "  wget -q -O /opt/cdk-mintd/cdk-mintd "
            "    https://github.com/cashubtc/cdk/releases/download/v${CDK_VER}/cdk-mintd-${CDK_VER}-x86_64 && "
            "  chmod +x /opt/cdk-mintd/cdk-mintd; fi && "
            "if [ -x /opt/cdk-mintd/cdk-cli ]; then echo 'cdk-cli cached'; else "
            "  wget -q -O /opt/cdk-mintd/cdk-cli "
            "    https://github.com/cashubtc/cdk/releases/download/v${CDK_VER}/cdk-cli-${CDK_VER}-x86_64 && "
            "  chmod +x /opt/cdk-mintd/cdk-cli && "
            "  ln -sf /opt/cdk-mintd/cdk-cli /usr/local/bin/cdk-cli; fi && "
            "/opt/cdk-mintd/cdk-mintd --version 2>&1 | head -1 && "
            "/opt/cdk-mintd/cdk-cli --version 2>&1 | head -1 && "
            "echo CDK_OK"
        )
        r = _gcloud_ssh(vm_name, cdk_cmd, zone, project, timeout=180)
        if r.returncode != 0 or "CDK_OK" not in (r.stdout or ""):
            print(f"WARNING: CDK install may have failed: {r.stderr[:300]}", file=sys.stderr)
        print(f"  Done in {time.monotonic() - t0:.1f}s")

        # Step 7: Setup bridge and prepare OpenWrt overlay
        _step(8, total_steps, "Setting up network bridge and OpenWrt overlay")
        t0 = time.monotonic()
        workdir = VIRT_LAB_WORKDIR
        bridge_cmd = (
            "sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1; "
            "ip link add name tg-poc-br type bridge 2>/dev/null || true; "
            "ip addr add 10.99.99.2/24 dev tg-poc-br 2>/dev/null || true; "
            "ip link set tg-poc-br up; "
            "ip tuntap add dev tg-poc-tap mode tap user root 2>/dev/null || true; "
            "ip link set tg-poc-tap master tg-poc-br 2>/dev/null || true; "
            "ip link set tg-poc-tap up; "
            "iptables -t nat -C POSTROUTING -s 10.99.99.0/24 ! -o tg-poc-br -j MASQUERADE 2>/dev/null || "
            "iptables -t nat -A POSTROUTING -s 10.99.99.0/24 ! -o tg-poc-br -j MASQUERADE; "
            f"mkdir -p {workdir}/run {workdir}/overlays {workdir}/images && "
            f"cd {workdir} && "
            "OWRT_BASE=images/openwrt-base.qcow2; "
            "[ -f \"$OWRT_BASE\" ] || OWRT_BASE=../images/openwrt-base.qcow2; "
            "OWRT_BASE=$(readlink -f \"$OWRT_BASE\"); "
            "rm -f overlays/tollgate-poc.qcow2 && "
            "qemu-img create -f qcow2 -F qcow2 -b \"$OWRT_BASE\" overlays/tollgate-poc.qcow2 >/dev/null && "
            "echo BRIDGE_OK"
        )
        r = _gcloud_ssh(vm_name, bridge_cmd, zone, project, timeout=60)
        if r.returncode != 0 or "BRIDGE_OK" not in (r.stdout or ""):
            print(f"ERROR: Bridge setup failed: {r.stderr[:300]}", file=sys.stderr)
            return 1
        print(f"  Done in {time.monotonic() - t0:.1f}s")

        # Step 8: Boot QEMU with OpenWrt overlay and provision via serial
        _step(9, total_steps, "Booting OpenWrt QEMU and provisioning via serial")
        t0 = time.monotonic()
        qemu_boot_cmd = (
            f"cd {workdir} && "
            "rm -f run/openwrt.serial.sock run/openwrt.monitor.sock run/openwrt.pid 2>/dev/null; "
            "nohup qemu-system-x86_64 "
            "-enable-kvm -m 128 -smp 1 -display none "
            "-serial unix:run/openwrt.serial.sock,server=on,wait=off "
            "-monitor unix:run/openwrt.monitor.sock,server=on,wait=off "
            "-drive file=overlays/tollgate-poc.qcow2,format=qcow2,if=virtio "
            "-netdev tap,id=net0,ifname=tg-poc-tap,script=no,downscript=no "
            "-device virtio-net-pci,netdev=net0,mac=52:54:00:12:34:56 "
            "-pidfile run/openwrt.pid "
            ">/tmp/openwrt-qemu.log 2>&1 &"
        )
        r = _gcloud_ssh(vm_name, qemu_boot_cmd, zone, project, timeout=30)
        # nohup returns immediately, but gcloud ssh may block
        # Wait a moment for QEMU to start
        time.sleep(3)

        # Now run the serial provisioning script inline
        password = shlex.quote(VIRT_LAB_PASSWORD)
        serial_provision_cmd = (
            f"cd {workdir} && "
            "/opt/tollgate-venv/bin/python3 - <<'PYEOF'\n"
            "import socket, time, sys, os\n"
            f"SOCK_PATH = os.path.expandvars('{workdir}') + '/run/openwrt.serial.sock'\n"
            f"PASSWORD = '{VIRT_LAB_PASSWORD}'\n"
            "OPENWRT_IP = '10.99.99.1'\n"
            "TIMEOUT = 90\n"
            "\n"
            "def recv_all(s, timeout=2):\n"
            "    s.settimeout(timeout)\n"
            "    chunks = []\n"
            "    try:\n"
            "        while True:\n"
            "            chunk = s.recv(4096)\n"
            "            if not chunk:\n"
            "                break\n"
            "            chunks.append(chunk)\n"
            "    except (TimeoutError, socket.timeout):\n"
            "        pass\n"
            "    return b''.join(chunks).decode('utf-8', errors='replace')\n"
            "\n"
            "def send_and_wait(s, cmd, wait=2):\n"
            "    s.sendall((cmd + '\\n').encode())\n"
            "    time.sleep(wait)\n"
            "    return recv_all(s, timeout=2)\n"
            "\n"
            "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "deadline = time.time() + 30\n"
            "connected = False\n"
            "while time.time() < deadline:\n"
            "    try:\n"
            "        s.connect(SOCK_PATH)\n"
            "        connected = True\n"
            "        break\n"
            "    except (ConnectionRefusedError, FileNotFoundError):\n"
            "        time.sleep(1)\n"
            "if not connected:\n"
            "    print('TIMEOUT: serial socket not ready')\n"
            "    sys.exit(1)\n"
            "\n"
            "print('Connected to serial, waiting for boot...')\n"
            "s.sendall(b'\\n')\n"
            "deadline = time.time() + TIMEOUT\n"
            "booted = False\n"
            "while time.time() < deadline:\n"
            "    s.sendall(b'\\n')\n"
            "    data = recv_all(s, timeout=2)\n"
            "    if 'Please press Enter' in data or 'root@OpenWrt' in data or ':/#' in data or 'OpenWrt' in data:\n"
            "        booted = True\n"
            "        break\n"
            "    time.sleep(1)\n"
            "if not booted:\n"
            "    print('TIMEOUT: OpenWrt did not boot')\n"
            "    s.close()\n"
            "    sys.exit(1)\n"
            "\n"
            "send_and_wait(s, '', wait=2)\n"
            "commands = [\n"
            f"    \"printf '%s\\\\\\n%s\\\\\\n' {password} {password} | passwd root\",\n"
            "    \"uci set dropbear.@dropbear[0].PasswordAuth='on'\",\n"
            "    'uci commit dropbear',\n"
            "    '/etc/init.d/dropbear restart',\n"
            "    'uci add firewall rule',\n"
            "    \"uci set firewall.@rule[-1].name='Allow-SSH-WAN'\",\n"
            "    \"uci set firewall.@rule[-1].src='wan'\",\n"
            "    \"uci set firewall.@rule[-1].dest_port='22'\",\n"
            "    \"uci set firewall.@rule[-1].proto='tcp'\",\n"
            "    \"uci set firewall.@rule[-1].target='ACCEPT'\",\n"
            "    'uci commit firewall',\n"
            "    'fw4 restart',\n"
            "    \"uci set network.lan.ipaddr='\" + OPENWRT_IP + \"'\",\n"
            "    \"uci set network.lan.netmask='255.255.255.0'\",\n"
            "    \"uci set network.lan.gateway='10.99.99.2'\",\n"
            "    \"uci set network.lan.dns='8.8.8.8'\",\n"
            "    'uci set network.mgmt=interface',\n"
            "    \"uci set network.mgmt.proto='static'\",\n"
            "    \"uci set network.mgmt.device='eth1'\",\n"
            "    \"uci set network.mgmt.ipaddr='10.99.97.1'\",\n"
            "    \"uci set network.mgmt.netmask='255.255.255.0'\",\n"
            "    'uci commit network',\n"
            "    '/etc/init.d/network restart',\n"
            # Remove any pre-installed Go TollGate binary and stale uci-defaults
            # so our Rust .ipk deploys cleanly without conflicts
            "    '/etc/init.d/tollgate-wrt stop 2>/dev/null; killall tollgate-wrt 2>/dev/null; killall tollgate 2>/dev/null; true',\n"
            "    'opkg remove tollgate-wrt 2>/dev/null; rm -f /usr/bin/tollgate /usr/bin/tollgate-wrt; true',\n"
            "    'rm -f /etc/uci-defaults/99-tollgate-setup /etc/uci-defaults/90-tollgate-captive-portal-symlink 2>/dev/null; true',\n"
            # Install jq (needed by our Rust init script for config.json parsing)
            "    'opkg update >/dev/null 2>&1; opkg install jq >/dev/null 2>&1; true',\n"
            "    'sync',\n"
            "]\n"
            "for cmd in commands:\n"
            "    send_and_wait(s, cmd, wait=2)\n"
            "\n"
            "s.close()\n"
            "print('SERIAL_PROVISION_OK')\n"
            "PYEOF\n"
        )
        r = _gcloud_ssh(vm_name, serial_provision_cmd, zone, project, timeout=180)
        if r.returncode != 0 or "SERIAL_PROVISION_OK" not in (r.stdout or ""):
            print(f"ERROR: Serial provisioning failed: {r.stderr[:500]}", file=sys.stderr)
            print(f"  stdout: {(r.stdout or '')[:500]}", file=sys.stderr)
            return 1

        # Wait for SSH to the OpenWrt VM
        print("  Waiting for OpenWrt SSH...")
        ssh_wait_cmd = (
            "for i in $(seq 1 30); do "
            f"sshpass -p {shlex.quote(VIRT_LAB_PASSWORD)} ssh "
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout=3 root@{OPENWRT_IP} 'echo SSH_OK' 2>/dev/null && break; "
            "sleep 2; "
            "done"
        )
        r = _gcloud_ssh(vm_name, ssh_wait_cmd, zone, project, timeout=120)
        if r.returncode != 0:
            print(f"WARNING: OpenWrt SSH wait returned {r.returncode}", file=sys.stderr)
        print(f"  Serial provisioning done in {time.monotonic() - t0:.1f}s")

        # Step 8b: Install WiFi packages for hwsim-based virtual WiFi testing
        _step(10, total_steps, "Installing WiFi packages for hwsim testing")
        wifi_pkgs = "kmod-mac80211-hwsim wpad-basic iw-full iwinfo socat"
        wifi_install_cmd = (
            f"sshpass -p {shlex.quote(VIRT_LAB_PASSWORD)} ssh "
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout=3 root@{OPENWRT_IP} "
            f"'opkg update >/dev/null 2>&1 && opkg install {wifi_pkgs} 2>&1 && echo WIFI_PKGS_OK || echo WIFI_PKGS_SKIP' "
            "2>/dev/null || true"
        )
        r = _gcloud_ssh(vm_name, wifi_install_cmd, zone, project, timeout=120)
        if "WIFI_PKGS_OK" in (r.stdout or ""):
            print("  WiFi packages installed: kmod-mac80211-hwsim wpad-basic iw-full iwinfo")
        else:
            print(f"  WARNING: WiFi package install skipped (non-fatal): {(r.stdout or '')[:200]}", file=sys.stderr)

        shutdown_cmd = (
            f"sshpass -p {shlex.quote(VIRT_LAB_PASSWORD)} ssh "
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout=3 root@{OPENWRT_IP} 'sync; poweroff' 2>/dev/null || true; "
            "sleep 8"
        )
        _gcloud_ssh(vm_name, shutdown_cmd, zone, project, timeout=60)

        # Step 8c: Install vwifi binaries (from Blossom cache or compile)
        _step(10, total_steps, "Installing vwifi binaries (cache or compile)")
        t0_vwifi = time.monotonic()
        vwifi_build_cmd = (
            'mkdir -p /opt/vwifi/bin/host /opt/vwifi/bin/debian /opt/vwifi/bin/openwrt && '
            # Try downloading each vwifi binary from Blossom cache
            'fetch_cached() { '
            '  local key=$1 dest=$2; '
            '  local url=$(nak req -k 1063 -l 1 -t "filename=$key" '
            '    wss://relay.cashu.email 2>/dev/null | python3 -c "'
            "import sys,json;[print(next(t[1] for t in json.loads(l)['tags'] if t[0]=='url')) for l in [sys.stdin.readline().strip()] if l]\" 2>/dev/null); "
            '  if [ -n "$url" ]; then curl -sfL -o "$dest" "$url" && chmod +x "$dest" && return 0; fi; '
            '  return 1; '
            '} && '
            'if fetch_cached vwifi-host-server-072cdb8 /opt/vwifi/bin/host/vwifi-server '
            '   && fetch_cached vwifi-host-ctrl-072cdb8 /opt/vwifi/bin/host/vwifi-ctrl '
            '   && fetch_cached vwifi-guest-client-072cdb8 /opt/vwifi/bin/debian/vwifi-client; then '
            '  cp /opt/vwifi/bin/debian/vwifi-client /opt/vwifi/bin/openwrt/vwifi-client && '
            '  echo "vwifi downloaded from Blossom cache"; '
            'else '
            '  echo "vwifi not fully cached, compiling from source..."; '
            '  apt-get update -qq && '
            '  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq '
            '  cmake make g++ pkg-config libnl-3-dev libnl-genl-3-dev git >/dev/null 2>&1 || true && '
            '  rm -rf /tmp/vwifi-build && '
            '  git clone --depth 1 https://github.com/Raizo62/vwifi.git /tmp/vwifi-build && '
            '  cd /tmp/vwifi-build && '
            '  mkdir -p build-host && cd build-host && '
            '  cmake .. -DCMAKE_BUILD_TYPE=Release && make -j$(nproc) && '
            '  cp vwifi-server vwifi-ctrl /opt/vwifi/bin/host/ && '
            '  cd /tmp/vwifi-build && '
            '  mkdir -p build-guest && cd build-guest && '
            '  cmake .. -DCMAKE_BUILD_TYPE=Release -DCMAKE_EXE_LINKER_FLAGS="-static" && '
            '  make -j$(nproc) && '
            '  cp vwifi-client vwifi-add-interfaces /opt/vwifi/bin/debian/ && '
            '  cp vwifi-client vwifi-add-interfaces /opt/vwifi/bin/openwrt/; '
            'fi && '
            'ls -la /opt/vwifi/bin/host/ /opt/vwifi/bin/debian/ /opt/vwifi/bin/openwrt/ && '
            'echo VWIFI_BUILD_OK'
        )
        r = _gcloud_ssh(vm_name, vwifi_build_cmd, zone, project, timeout=600)
        if "VWIFI_BUILD_OK" in (r.stdout or ""):
            print("  vwifi binaries built and installed to /opt/vwifi/bin/")
            print(f"  Done in {time.monotonic() - t0_vwifi:.1f}s")
        else:
            print(f"  WARNING: vwifi build failed (non-fatal): {(r.stdout or '')[:200]}", file=sys.stderr)
            print(f"  stderr: {(r.stderr or '')[:300]}", file=sys.stderr)

        # Step 8d: Pre-bake Playwright into Debian QEMU overlay
        _step(10, total_steps, "Pre-baking Playwright + Chromium into Debian overlay")
        t0_deb = time.monotonic()
        debian_pw_cmd = (
            f"cd {workdir} && "
            # Reuse existing Debian overlay from base snapshot (already has SSH + password configured)
            # Only create fresh overlay if the cached one doesn't exist
            "if [ -f overlays/debian-client.qcow2 ]; then "
            "echo 'Reusing cached Debian overlay'; "
            "else "
            "DEB_BASE=images/debian-12-nocloud-amd64.qcow2; "
            "[ -f \"$DEB_BASE\" ] || DEB_BASE=../images/debian-12-nocloud-amd64.qcow2; "
            "DEB_BASE=$(readlink -f \"$DEB_BASE\"); "
            "qemu-img create -f qcow2 -F qcow2 -b \"$DEB_BASE\" overlays/debian-client.qcow2 >/dev/null && "
            "qemu-img resize --shrink overlays/debian-client.qcow2 10G >/dev/null 2>&1 || true; "
            "fi && "
            "ip tuntap add dev tg-poc-tap2 mode tap user root 2>/dev/null || true && "
            "ip link set tg-poc-tap2 master tg-poc-br 2>/dev/null || true && "
            "ip link set tg-poc-tap2 up 2>/dev/null || true && "
            "ip link add name mgmt-br type bridge 2>/dev/null || true; "
            "ip addr add 10.99.97.2/24 dev mgmt-br 2>/dev/null || true; "
            "ip link set mgmt-br up 2>/dev/null || true; "
            "ip tuntap add dev mgmt-tap2 mode tap user root 2>/dev/null || true; "
            "ip link set mgmt-tap2 master mgmt-br 2>/dev/null || true; "
            "ip link set mgmt-tap2 up 2>/dev/null || true; "
            "nohup qemu-system-x86_64 "
            "-enable-kvm -m 1536 -smp 2 -display none "
            "-smbios type=1,serial=ds=nocloud "
            f"-drive file=overlays/debian-client.qcow2,format=qcow2,if=virtio "
            "-netdev tap,id=net0,ifname=tg-poc-tap2,script=no,downscript=no "
            f"-device virtio-net-pci,netdev=net0,mac=de:54:4e:91:49:da "
            "-netdev tap,id=mgmt0,ifname=mgmt-tap2,script=no,downscript=no "
            f"-device virtio-net-pci,netdev=mgmt0,mac=52:54:00:c0:02:64 "
            ">/tmp/debian-qemu.log 2>&1 &"
        )
        r = _gcloud_ssh(vm_name, debian_pw_cmd, zone, project, timeout=60)

        print("  Waiting for Debian VM SSH...")
        deb_ssh_wait = (
            f"for i in $(seq 1 90); do "
            f"sshpass -p {shlex.quote(VIRT_LAB_PASSWORD)} ssh "
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout=3 root@10.99.99.100 'echo DEB_SSH_OK' 2>/dev/null && break; "
            "sleep 2; done"
        )
        r = _gcloud_ssh(vm_name, deb_ssh_wait, zone, project, timeout=300)

        if "DEB_SSH_OK" in (r.stdout or ""):
            print("  Debian VM SSH ready, installing Playwright...")
            pw_install = (
                f"sshpass -p {shlex.quote(VIRT_LAB_PASSWORD)} ssh "
                "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                "root@10.99.99.100 "
                "'apt-get -o Acquire::ForceIPv4=true update -qq && "
                "DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true install -y -qq --no-install-recommends "
                "python3-pip libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcairo2 libcups2 libdbus-1-3 "
                "libdrm2 libgbm1 libglib2.0-0 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 "
                "libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 xvfb fonts-liberation fonts-freefont-ttf >/dev/null && "
                "python3 -m pip install -q --break-system-packages playwright && "
                "python3 -m playwright install chromium >/dev/null && "
                "python3 -c \"import playwright; print('PLAYWRIGHT_OK')\"' 2>&1"
            )
            r = _gcloud_ssh(vm_name, pw_install, zone, project, timeout=600)
            if "PLAYWRIGHT_OK" in (r.stdout or ""):
                print("  Playwright + Chromium installed in Debian overlay")
            else:
                print(f"  WARNING: Playwright install may have failed: {(r.stdout or '')[:300]}", file=sys.stderr)

            print("  Shutting down Debian VM...")
            deb_shutdown = (
                f"sshpass -p {shlex.quote(VIRT_LAB_PASSWORD)} ssh "
                "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                "root@10.99.99.100 'sync; poweroff' 2>/dev/null || true; "
                "sleep 5; killall -9 qemu-system-x86_64 2>/dev/null; sleep 2; "
                f"cd {workdir} && "
                "echo 'Flattening Debian overlay onto base...'; "
                "DEB_BASE=images/debian-12-nocloud-amd64.qcow2; "
                "[ -f \"$DEB_BASE\" ] || DEB_BASE=../images/debian-12-nocloud-amd64.qcow2; "
                "DEB_BASE=$(readlink -f \"$DEB_BASE\"); "
                "qemu-img convert -f qcow2 -O qcow2 overlays/debian-client.qcow2 /tmp/debian-base-flat.qcow2 && "
                "mv /tmp/debian-base-flat.qcow2 \"$DEB_BASE\" && "
                "rm -f overlays/debian-client.qcow2 && "
                "echo DEBIAN_FLATTEN_OK"
            )
            r = _gcloud_ssh(vm_name, deb_shutdown, zone, project, timeout=120)
            if "DEBIAN_FLATTEN_OK" in (r.stdout or ""):
                print(f"  Debian base image updated with Playwright ({time.monotonic() - t0_deb:.1f}s)")
            else:
                print(f"  WARNING: Debian flatten may have failed: {(r.stdout or '')[:300]}", file=sys.stderr)
        else:
            print(f"  WARNING: Debian VM SSH not ready, skipping Playwright pre-bake ({time.monotonic() - t0_deb:.1f}s)", file=sys.stderr)

        # Step 9: Install GitHub Actions runner binary
        _step(11, total_steps, "Installing GitHub Actions runner binary")
        t0_runner = time.monotonic()
        runner_version = "2.334.0"
        runner_install_cmd = (
            "id runner >/dev/null 2>&1 || useradd -m -s /bin/bash runner && "
            f"RUNNER_VERSION={runner_version} && "
            'RUNNER_DIR=/home/runner/actions-runner && '
            f'[ -f "$RUNNER_DIR/run.sh" ] && grep -q "{runner_version}" "$RUNNER_DIR/run.sh" && echo "Runner v{runner_version} already present" && echo RUNNER_INSTALL_OK && exit 0; '
            'echo "Removing old runner..." && rm -rf "$RUNNER_DIR" && '
            'rm -rf "$RUNNER_DIR" && mkdir -p "$RUNNER_DIR" && '
            'cd "$RUNNER_DIR" && '
            "curl -fL -o actions-runner-linux-x64.tar.gz "
            '"https://github.com/actions/runner/releases/download/v$RUNNER_VERSION/actions-runner-linux-x64-$RUNNER_VERSION.tar.gz" && '
            "tar xzf actions-runner-linux-x64.tar.gz && "
            "rm -f actions-runner-linux-x64.tar.gz && "
            "chown -R runner:runner /home/runner/actions-runner && "
            'ls "$RUNNER_DIR/run.sh" && '
            "echo RUNNER_INSTALL_OK"
        )
        r = _gcloud_ssh(vm_name, runner_install_cmd, zone, project, timeout=300)
        if "RUNNER_INSTALL_OK" in (r.stdout or ""):
            print(f"  GitHub Actions runner v{runner_version} installed to /home/runner/actions-runner/")
            print(f"  Done in {time.monotonic() - t0_runner:.1f}s")
        else:
            print(f"  WARNING: Runner install failed (non-fatal): {(r.stdout or '')[:200]}", file=sys.stderr)
            print(f"  stderr: {(r.stderr or '')[:300]}", file=sys.stderr)

        # Step 10: Stop QEMU and copy overlay as new base
        _step(12, total_steps, "Stopping QEMU and replacing base image with provisioned overlay")
        t0 = time.monotonic()
        replace_cmd = (
            "killall -TERM qemu-system-x86_64 2>/dev/null || true; sleep 5; "
            "killall -9 qemu-system-x86_64 2>/dev/null || true; sleep 2; "
            f"cd {workdir} && "
            "OWRT_BASE=$(readlink -f images/openwrt-base.qcow2 2>/dev/null || echo images/openwrt-base.qcow2); "
            "echo \"Replacing base at $OWRT_BASE\"; "
            "ls -la \"$OWRT_BASE\"; "
            "qemu-img convert -f qcow2 -O qcow2 overlays/tollgate-poc.qcow2 /tmp/openwrt-base-flat.qcow2 && "
            "mv /tmp/openwrt-base-flat.qcow2 \"$OWRT_BASE\" && "
            "ls -la \"$OWRT_BASE\" && "
            "rm -f overlays/tollgate-poc.qcow2 overlays/tollgate-seller.qcow2 && "
            "echo BASE_REPLACED_OK"
        )
        r = _gcloud_ssh(vm_name, replace_cmd, zone, project, timeout=120)
        if r.returncode != 0 or "BASE_REPLACED_OK" not in (r.stdout or ""):
            print(f"ERROR: Base image replacement failed: {r.stderr[:300]}", file=sys.stderr)
            return 1
        print(f"  Base image replaced in {time.monotonic() - t0:.1f}s")

        # Step 12: Install BlossomFS (from Blossom cache or compile)
        _step(13, total_steps, "Installing BlossomFS (Blossom cache or compile)")
        t0 = time.monotonic()

        # Try cache first, fall back to compile
        blossomfs_cmd = (
            'export HOME=/root && '
            'BLOSSOMFS_SHA=$(nak req -k 1063 -l 1 -t "filename=blossomfs-8784100" '
            'wss://relay.cashu.email 2>/dev/null | python3 -c "'
            "import sys,json;[print(next(t[1] for t in json.loads(l)['tags'] if t[0]=='url')) for l in [sys.stdin.readline().strip()] if l]\" 2>/dev/null); "
            'if [ -n "$BLOSSOMFS_SHA" ]; then '
            '  echo "Downloading BlossomFS from Blossom cache: ${BLOSSOMFS_SHA:0:50}..."; '
            '  mkdir -p /opt/blossomfs/target/release && '
            '  curl -sfL -o /opt/blossomfs/target/release/blossomfs "$BLOSSOMFS_SHA" && '
            '  chmod +x /opt/blossomfs/target/release/blossomfs && '
            '  echo "BlossomFS downloaded from cache"; '
            'else '
            '  echo "BlossomFS not cached, compiling from source..."; '
            '  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq '
            '  fuse3 libfuse3-dev pkg-config build-essential libssl-dev openssl >/dev/null 2>&1 || true; '
            '  if command -v cargo >/dev/null 2>&1; then echo "Rust already installed"; else '
            '  curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y; fi && '
            '  . "$HOME/.cargo/env" && '
            '  rm -rf /opt/blossomfs && '
            '  git clone --depth 1 https://github.com/Amperstrand/blossomfs /opt/blossomfs && '
            '  cd /opt/blossomfs && cargo build --release 2>&1; '
            'fi && '
            'test -x /opt/blossomfs/target/release/blossomfs && echo BLOSSOMFS_OK'
        )
        r = _gcloud_ssh(vm_name, blossomfs_cmd, zone, project, timeout=900)
        if r.returncode != 0 or "BLOSSOMFS_OK" not in (r.stdout or ""):
            print(f"WARNING: BlossomFS build may have failed: {r.stderr[:300]}", file=sys.stderr)
        else:
            print(f"  BlossomFS pre-compiled in {time.monotonic() - t0:.1f}s")

        # Step 13: Stop VM and create snapshot
        _step(12, total_steps, "Stopping VM and creating snapshot")
        t0 = time.monotonic()
        r = _run_gcloud([
            "compute", "instances", "stop", vm_name,
            f"--project={project}", f"--zone={zone}", "--quiet",
        ], timeout=120)
        if r.returncode != 0:
            print(f"WARNING: VM stop failed: {r.stderr[:300]}", file=sys.stderr)

        disk_name = vm_name
        r = _run_gcloud([
            "compute", "disks", "snapshot", disk_name,
            f"--project={project}", f"--zone={zone}",
            f"--snapshot-names={snapshot_name}",
            "--storage-location=europe-west1",
        ], timeout=900)
        if r.returncode != 0:
            print(f"ERROR: Snapshot creation failed: {r.stderr}", file=sys.stderr)
            return 1
        print(f"  Snapshot created in {time.monotonic() - t0:.1f}s")

    finally:
        # Clean up temp VM
        print("\nCleaning up temporary VM...")
        _run_gcloud([
            "compute", "instances", "delete", vm_name,
            f"--project={project}", f"--zone={zone}",
            "--delete-disks=all", "--quiet",
        ], timeout=120)

    print(f"\n{'=' * 60}")
    print("Bake complete!")
    print(f"  New snapshot: {snapshot_name}")
    print("  To use: update SNAPSHOT_NAME in lib/cloud_lab/constants.py")
    print("  Verify: ./scripts/cloud-lab.py up --vm-name test-bake-vm")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    bake = sub.add_parser("bake", help="Bake a new snapshot from the current base snapshot")
    bake.add_argument("--snapshot-name", default="", help=f"Name for the new snapshot (default: auto-increment from {SNAPSHOT_NAME})")
    bake.add_argument("--base-snapshot", default=SNAPSHOT_NAME, help=f"Base snapshot to create VM from (default: {SNAPSHOT_NAME})")
    bake.add_argument("--from-scratch", action="store_true", help="Create the bake VM from a stock Debian 12 image (no base snapshot needed)")
    bake.add_argument("--debian-overlay", default="", help="Local debian-client.qcow2 to upload when --from-scratch (pre-provisioned, 10.99.99.100)")
    bake.add_argument("--openwrt-archive", default="", help="Local openwrt .img.gz to upload when --from-scratch (skips the flaky downloads.openwrt.org transfer from GCP)")
    bake.add_argument("--zone", default=DEFAULT_ZONE)
    bake.add_argument("--machine-type", default=DEFAULT_MACHINE_TYPE)
    bake.add_argument("--disk-size", type=int, default=DEFAULT_DISK_SIZE_GB)
    bake.set_defaults(func=cmd_bake)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    from collections.abc import Callable
    func = cast(Callable[[argparse.Namespace], int], args.func)
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
