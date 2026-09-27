#!/usr/bin/env python3
"""Fresh-flash the bench router — the destructive prerequisite, done carefully.

Flashing is wipes ``/etc/tollgate`` (config, identities and **real ecash**), so
this script:

  1. takes the bench lock (``~/.hermes/state/bench-mt3000.lock``) — the bench is
     a single-owner resource;
  2. verifies the image name + sha256 against the upstream ``sha256sums``
     (OpenWrt 25.12.x / mediatek-filogic / glinet_gl-mt3000 sysupgrade);
  3. probes the wallet and **REFUSES to flash a non-empty wallet** unless
     ``--allow-nonempty-wallet`` is given.  Draining is the operator's step:
     ``tollgate wallet drain cashu --yes`` on the router prints the Cashu tokens;
  4. stages the image with ``scp -O`` (OpenWrt has no sftp-server), re-verifies
     the sha256 **on the router**, and only then runs ``sysupgrade -n``;
  5. waits for SSH to come back (this router drops ICMP — TCP 22 only) and
     verifies the board is a *fresh* 25.12.x MT3000 with no TollGate state.

Usage::

    scripts/fresh-flash.py --check                    # probe only, changes nothing
    scripts/fresh-flash.py --flash                    # for real (lock + wallet gate)
    scripts/fresh-flash.py --flash --allow-nonempty-wallet

After a real flash the LAN is OpenWrt's default ``192.168.1.1`` and the root
password is EMPTY.  Re-address the host NIC (``--readdress enp0s31f6``) and set a
password before running the install-path scenarios.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib import fresh_flash as ff  # noqa: E402
from lib import install_paths as ip  # noqa: E402
from lib.bench_lock import BenchBusy, BenchLock  # noqa: E402

DEFAULT_HOST = os.environ.get("TOLLGATE_SSH_HOST") or os.environ.get("ROUTER_IP") or "192.168.1.1"


def ssh(host: str, command: str, *, timeout: int = 60, password: str | None = None) -> tuple[str, int]:
    pw = password
    if pw is None:
        pw = os.environ.get("TOLLGATE_SSH_PASSWORD") or os.environ.get("TOLLGATE_LUCI_PASSWORD") or ""
    args = [
        "sshpass", "-e", "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "LogLevel=ERROR",
        f"root@{host}",
        command,
    ]
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, env={**os.environ, "SSHPASS": pw}
    )
    return (result.stdout + result.stderr).strip(), result.returncode


def scp_to(host: str, local: Path, remote: str, *, timeout: int = 900) -> None:
    pw = os.environ.get("TOLLGATE_SSH_PASSWORD") or os.environ.get("TOLLGATE_LUCI_PASSWORD") or ""
    result = subprocess.run(
        ["sshpass", "-e", "scp", "-O",
         "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null",
         "-o", "LogLevel=ERROR",
         str(local), f"root@{host}:{remote}"],
        capture_output=True, text=True, timeout=timeout, env={**os.environ, "SSHPASS": pw},
    )
    if result.returncode != 0:
        raise SystemExit(f"scp -O failed ({result.returncode}): {result.stderr.strip()[:300]}")


def wait_for_ssh(host: str, *, timeout: int = 300) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ip.tcp_probe(host, 22, timeout=3):
            out, rc = ssh(host, "echo ready", timeout=15)
            if rc == 0 and "ready" in out:
                return True
        time.sleep(5)
    return False


def check(args: argparse.Namespace) -> int:
    """Read-only: image, wallet and lock state.  Changes nothing."""
    print("== fresh-flash preconditions (read-only) ==")
    rc = 0

    lock = BenchLock(purpose="fresh-flash-check", path=args.lock)
    free, holder = lock.status()
    print(f"lock   : {lock.path} -> {'free' if free else 'HELD by ' + holder.raw}")

    try:
        image = ff.resolve_image_path(args.image)
        problems = ff.verify_image(image)
        print(f"image  : {image}")
        print(f"sha256 : {'OK' if not problems else 'PROBLEM: ' + '; '.join(problems)}")
        rc |= 1 if problems else 0
    except ff.ImageInvalid as exc:
        print(f"image  : MISSING — {exc}")
        rc |= 1

    out, _ = ssh(args.host, ff.WALLET_BALANCE_COMMAND, timeout=60)
    listing, _ = ssh(args.host, ff.ECASH_LISTING_COMMAND, timeout=30)
    state = ff.parse_wallet_state(out, listing)
    print(f"wallet : {state.summary()}")
    if not state.empty:
        print(f"         -> drain before flashing:  {ff.DRAIN_COMMAND}")
        rc |= 2
    release, _ = ssh(args.host, ip.openwrt_release_command(), timeout=20)
    print(f"router : {release}")
    return rc


def flash(args: argparse.Namespace) -> int:
    image = ff.resolve_image_path(args.image)
    problems = ff.verify_image(image)
    if problems:
        raise SystemExit("image refused: " + "; ".join(problems))

    lock = BenchLock(
        purpose="fresh-flash-sysupgrade",
        task_id=os.environ.get("TOLLGATE_BENCH_TASK_ID", "t_a05094ad"),
        path=args.lock,
    )
    try:
        lock.acquire()
    except BenchBusy as exc:
        raise SystemExit(str(exc)) from exc

    try:
        print(f"[lock] holding {lock.path} as {lock.holder.raw}")
        # 1. wallet gate ------------------------------------------------------
        out, _ = ssh(args.host, ff.WALLET_BALANCE_COMMAND, timeout=60)
        listing, _ = ssh(args.host, ff.ECASH_LISTING_COMMAND, timeout=30)
        state = ff.parse_wallet_state(out, listing)
        print(f"[wallet] {state.summary()}")
        ff.flash_guard(state, allow_nonempty=args.allow_nonempty_wallet)

        # 2. stage + verify on the router ------------------------------------
        remote = ff.remote_image_path(os.path.basename(image))
        print(f"[stage] {image} -> {args.host}:{remote} (scp -O)")
        scp_to(args.host, Path(image), remote)
        local_sha = ip.sha256_file(image)
        remote_sha, _ = ssh(args.host, f"sha256sum {remote} | cut -d' ' -f1", timeout=120)
        if remote_sha.strip() != local_sha:
            raise SystemExit(f"staged image sha256 mismatch: {remote_sha.strip()} != {local_sha}")
        print(f"[stage] sha256 verified on the router: {local_sha}")

        # 3. the flash itself -------------------------------------------------
        cmd = ff.sysupgrade_command(remote)
        print(f"[flash] {cmd}   (config is NOT kept)")
        if not args.yes_i_mean_it:
            raise SystemExit("refusing to flash without --yes-i-mean-it")
        ssh(args.host, cmd, timeout=30)  # the link drops here; expected

        # 4. wait, then verify freshness -------------------------------------
        if not wait_for_ssh(args.host, timeout=args.timeout):
            raise SystemExit(
                "router did not come back — recover over ethernet/serial. The fresh image uses "
                "192.168.1.1 with an EMPTY root password."
            )
        release, _ = ssh(args.host, ip.board_identity_command(), timeout=30)
        print(f"[verify] {release}")
        troubles = ff.fresh_image_ready_violations(release)
        installed, _ = ssh(args.host, ip.package_version_command("apk"), timeout=30)
        dir_listing, _ = ssh(args.host, f"ls {ff.TOLLGATE_DIR} 2>/dev/null", timeout=20)
        troubles += ff.no_tollgate_state_violations(installed, dir_listing)
        if troubles:
            raise SystemExit("post-flash checks failed: " + "; ".join(troubles))
        print("[verify] fresh image confirmed (no tollgate-wrt, no /etc/tollgate state)")

        if args.readdress:
            print(
                "[host] re-address the NIC: "
                + ff.post_flash_readdress_command(args.readdress)
                + "   # the fresh image defaults to 192.168.1.1"
            )
        print(
            "\n[next] the image has an EMPTY root password. Set one before the install-path run:\n"
            "       printf '%s\\n%s\\n' '<pw>' '<pw>' | ssh root@<router> passwd root\n"
            "       # (chpasswd does not exist on OpenWrt)"
        )
        return 0
    finally:
        lock.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--image", default=os.environ.get("TOLLGATE_FRESH_FLASH_IMAGE"))
    parser.add_argument("--lock", default=None)
    parser.add_argument("--check", action="store_true", help="read-only precondition check")
    parser.add_argument("--flash", action="store_true", help="flash (destructive, needs --yes-i-mean-it)")
    parser.add_argument("--yes-i-mean-it", action="store_true")
    parser.add_argument(
        "--allow-nonempty-wallet",
        dest="allow_nonempty_wallet",
        action="store_true",
        help="flash even though the wallet holds ecash (you accept losing it)",
    )
    parser.add_argument("--readdress", default=None, help="host NIC to re-address after the flash")
    parser.add_argument("--timeout", type=int, default=300, help="seconds to wait for the router to return")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.flash:
        return flash(args)
    return check(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
