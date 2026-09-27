#!/usr/bin/env python3
"""Switch-side recovery orchestrator — VLAN quarantine + TFTP + PoE cycle.

Implements the port-scoped recovery state machine on the GS1900-8HP
(OpenWrt + realtek-poe), following the conwrt-verified recipes:

    ISOLATE  -> move ONLY the target port into VLAN 999 (CPU stays member)
    SERVE    -> dnsmasq TFTP on br-lan.999 with the bootloader's bootfile
    POWER    -> PoE cycle the port (min off-time enforced by PoePowerController)
    DETECT   -> watch for DHCPDISCOVER / TFTP RRQ from the target
    RESTORE  -> return the port to the default bridge, stop TFTP

The image-push step itself (zycast for NR7101, sysupgrade after TFTP boot
for AP3915i) stays with the conwrt recipes — this tool builds and tears
down the safe environment around them. Delegating the flash is deliberate:
flashing is device-specific and destructive; isolation, power, detection
and cleanup are shared infrastructure.

SAFETY:
- DRY-RUN BY DEFAULT. Every switch mutation requires --execute.
- `network restart` blips ALL switch ports for ~10s (documented DSA
  gotcha) — requires a free bench (BenchLock is taken first).
- Never run while another session drives devices on this switch.

Usage:
    python3 scripts/recovery/switch_tftp_recovery.py plan --port lan2 --device nr7101
    python3 scripts/recovery/switch_tftp_recovery.py isolate --port lan2 --execute
    python3 scripts/recovery/switch_tftp_recovery.py restore --port lan2 --execute
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from lib.lab_inventory import load_inventory, poe_controller_config  # noqa: E402
from tollgate_lab.hardware.bench_lock import (  # noqa: E402
    BenchLockHeldError,
    acquire_bench_lock,
)
from tollgate_lab.hardware.poe import PoePowerController  # noqa: E402

ISOLATION_VLAN = 999
HELPER_IP = "192.168.1.2"
HELPER_IF = f"br-lan.{ISOLATION_VLAN}"

# Bootloader TFTP bootfiles (conwrt model JSONs, hardware-verified)
BOOTFILES = {
    "nr7101": None,  # zycast multicast — no TFTP bootfile
    "ap3915i": "vmlinux.gz.uImage.3912",
}


def switch_ssh(cfg, cmd: str, timeout: float = 30.0) -> str:
    args = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
    ]
    if cfg.keyfile:
        args += ["-i", cfg.keyfile, "-o", "IdentitiesOnly=yes"]
    args += [f"{cfg.username}@{cfg.host}", cmd]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"switch ssh rc={proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def isolate_cmds(port: str) -> list[str]:
    return [
        "uci add network bridge-vlan",
        "uci set network.@bridge-vlan[-1].device='br-lan'",
        f"uci set network.@bridge-vlan[-1].vlan='{ISOLATION_VLAN}'",
        f"uci add_list network.@bridge-vlan[-1].ports='{port}:u*'",
        "uci set network.@bridge-vlan[-1].local='1'",
        "uci commit network",
        "/etc/init.d/network restart",
        f"ip addr add {HELPER_IP}/24 dev {HELPER_IF}",
    ]


def restore_cmds(port: str) -> list[str]:
    find_idx = (
        "uci show network | grep 'bridge-vlan.*vlan=%d' | cut -d. -f2 "
        "| cut -d[ -f2 | cut -d] -f1" % ISOLATION_VLAN
    )
    return [
        f"IDX=$({find_idx}) && uci delete network.@bridge-vlan[$IDX]",
        "uci commit network",
        "/etc/init.d/network restart",
        f"ip addr del {HELPER_IP}/24 dev {HELPER_IF} 2>/dev/null; true",
    ]


def serve_cmds(bootfile: str) -> list[str]:
    return [
        "mkdir -p /tmp/tftpboot",
        "uci set dhcp.@dnsmasq[0].enable_tftp=1",
        "uci set dhcp.@dnsmasq[0].tftp_root=/tmp/tftpboot",
        f"uci set dhcp.@dnsmasq[0].dhcp_boot={bootfile}",
        "uci commit dhcp",
        "/etc/init.d/dnsmasq restart",
        "nft insert rule inet fw4 input iifname \"br-lan.%d\" accept" % ISOLATION_VLAN,
    ]


def unserve_cmds() -> list[str]:
    return [
        "uci delete dhcp.@dnsmasq[0].enable_tftp",
        "uci delete dhcp.@dnsmasq[0].tftp_root",
        "uci delete dhcp.@dnsmasq[0].dhcp_boot",
        "uci commit dhcp",
        "/etc/init.d/dnsmasq restart",
    ]


def detect(cfg, seconds: int = 90) -> str:
    """Watch the isolated VLAN for the first DHCP/TFTP packet from the target."""
    cmd = (
        f"timeout {seconds} tcpdump -i {HELPER_IF} -nn -c 1 "
        "'port 67 or port 69' 2>/dev/null | head -1"
    )
    out = switch_ssh(cfg, cmd, timeout=seconds + 30)
    return out.strip()


def run_step(cfg, cmd: str, execute: bool) -> None:
    if execute:
        print(f"  EXEC  {cmd}")
        switch_ssh(cfg, cmd, timeout=90)
    else:
        print(f"  DRY   {cmd}")


def cmd_plan(args: argparse.Namespace) -> int:
    bootfile = BOOTFILES.get(args.device)
    print(f"# recovery plan: device={args.device} port={args.port} "
          f"vlan={ISOLATION_VLAN} helper={HELPER_IP}")
    print("# 0. bench lock (BenchLock) — cross-project exclusivity")
    print("\n# 1. ISOLATE (network restart blips ALL ports ~10s!)")
    for c in isolate_cmds(args.port):
        print(f"#    {c}")
    if bootfile:
        print("\n# 2. SERVE TFTP (stage image to /tmp/tftpboot first)")
        for c in serve_cmds(bootfile):
            print(f"#    {c}")
    else:
        print("\n# 2. SERVE: n/a — device uses zycast multicast (conwrt recipe)")
    print("\n# 3. POWER cycle (PoePowerController, min off-time 8s)")
    print("\n# 4. DETECT DHCP/TFTP on the isolated VLAN")
    print("\n# 5. FLASH via conwrt recipe (zycast / TFTP-boot + sysupgrade)")
    print("\n# 6. RESTORE")
    for c in (unserve_cmds() if bootfile else []) + restore_cmds(args.port):
        print(f"#    {c}")
    print("\nRun with --execute to perform steps 1,3,4,6 (flash stays manual).")
    return 0


def _with_lock_and_ctl(args):
    inv = load_inventory()
    try:
        bench = acquire_bench_lock(
            name="prta-poe-bench", project="physical-router-test-automation", cwd=str(REPO_ROOT)
        )
    except BenchLockHeldError as e:
        print(f"REFUSING: bench busy: {e}", file=sys.stderr)
        raise SystemExit(2)
    cfg = poe_controller_config(inv)
    ctl = PoePowerController(cfg)
    return bench, cfg, ctl, inv


def cmd_isolate(args: argparse.Namespace) -> int:
    bench, cfg, _ctl, _inv = _with_lock_and_ctl(args)
    with bench:
        for c in isolate_cmds(args.port):
            run_step(cfg, c, args.execute)
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    bench, cfg, _ctl, _inv = _with_lock_and_ctl(args)
    with bench:
        for c in unserve_cmds() + restore_cmds(args.port):
            run_step(cfg, c, args.execute)
    return 0


def cmd_power(args: argparse.Namespace) -> int:
    bench, _cfg, ctl, _inv = _with_lock_and_ctl(args)
    with bench:
        print(f"# power-cycling {args.port} (dry-run does nothing)")
        if args.execute:
            result = ctl.cycle(args.port, min_off_s=8.0)
            print(f"  -> {result.status.name} ({result.consumption_w:.1f}W)")
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    _bench, cfg, _ctl, _inv = _with_lock_and_ctl(args)
    print(f"# watching {HELPER_IF} for {args.seconds}s (DHCP/TFTP)")
    hit = detect(cfg, args.seconds)
    if hit:
        print(f"  PACKET {hit}")
        return 0
    print("  no DHCP/TFTP traffic observed")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="step", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--port", required=True, help="switch port, e.g. lan2")
        p.add_argument("--device", default="nr7101", choices=sorted(BOOTFILES))
        p.add_argument("--execute", action="store_true",
                       help="actually mutate the switch (default: dry-run)")

    p = sub.add_parser("plan", help="print the full recovery sequence")
    common(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("isolate", help="move port into VLAN %d" % ISOLATION_VLAN)
    common(p)
    p.set_defaults(func=cmd_isolate)

    p = sub.add_parser("restore", help="return port to default bridge + stop TFTP")
    common(p)
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("power", help="PoE power-cycle the port")
    common(p)
    p.set_defaults(func=cmd_power)

    p = sub.add_parser("detect", help="watch isolated VLAN for DHCP/TFTP")
    common(p)
    p.add_argument("--seconds", type=int, default=90)
    p.set_defaults(func=cmd_detect)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
