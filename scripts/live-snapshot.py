#!/usr/bin/env python3
"""Live-router state snapshot / diff / restore for commissioned runs.

The 2026-10-01 incident (docs/live-commission-incident-2026-10-01.md): a
"read-only" commission silently rewrote a live router's accepted_mints
because nothing diffed router state before/after. This tool is the audit
rail: snapshot before the run, diff after, restore on drift.

  snapshot   capture router state into a snapshot directory
  diff       re-capture and compare against a base snapshot (exit 1 = drift)
  restore    push the base config.json back (verify hash; --restart applies)

Reads only (snapshot/diff). Restore writes exactly one file and never
touches wallet.db, sessions.json, or uci.

Usage:
  python3 scripts/live-snapshot.py snapshot --host 192.168.1.1 \
      --ssh-key ~/.ssh/id_ed25519 [--out DIR]
  python3 scripts/live-snapshot.py diff --base DIR [--host ...]
  python3 scripts/live-snapshot.py restore --base DIR [--restart] [--yes]

Env: TOLLGATE_SSH_HOST, TOLLGATE_SSH_KEY, TOLLGATE_SSH_PASSWORD (sshpass).
Exit: 0 clean/ok, 1 drift/failure, 2 usage/ssh error.
"""

import argparse
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

TEXT_CAPTURES = {
    "config.json": "cat /etc/tollgate/config.json 2>/dev/null",
    "uci-show.txt": "uci show 2>/dev/null",
    "ndsctl-status.txt": "ndsctl status 2>/dev/null",
    "nft-ruleset.txt": "nft list ruleset 2>/dev/null || iptables-save 2>/dev/null",
    "processes.txt": "ps w 2>/dev/null | grep -E 'tollgate|nodogsplash|uhttpd' | grep -v grep",
    "version.txt": "tollgate-wrt --version 2>&1",
}

# Live-behavior captures that change with normal traffic (counters, session
# state) — reported as notes, never counted as config drift.
VOLATILE_CAPTURES = {"ndsctl-status.txt", "nft-ruleset.txt", "processes.txt"}

HASHED_FILES = [
    "/etc/tollgate/config.json",
    "/etc/tollgate/wallet.db",
    "/etc/tollgate/sessions.json",
    "/etc/tollgate/quotes.json",
    "/etc/tollgate/identities.json",
    "/etc/tollgate/install.json",
]

DEFAULT_ROOT = os.path.join("results", "live-snapshots")


class SshError(RuntimeError):
    pass


def ssh_run(host, cmd, key=None, timeout=30):
    args = ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
    if key:
        args += ["-i", key]
    args.append(f"root@{host}")
    args.append(cmd)
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise SshError(f"ssh exit {proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def capture(host, key, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for name, cmd in TEXT_CAPTURES.items():
        try:
            content = ssh_run(host, cmd, key)
        except SshError as e:
            content = f"<<CAPTURE FAILED: {e}>>"
        with open(os.path.join(out_dir, name), "w") as f:
            f.write(content)

    hashes = {}
    for path in HASHED_FILES:
        out = ssh_run(host, f"md5sum {path} 2>/dev/null || echo MISSING")
        hashes[path] = out.split()[0] if not out.startswith("MISSING") else "MISSING"
    meta = {
        "host": host,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "file_md5": hashes,
    }
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    return out_dir


def snapshot_paths(snap_dir):
    return sorted(p for p in os.listdir(snap_dir) if not p.startswith("."))


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compare_snapshots(base_dir, cur_dir):
    """Compare two snapshot dirs. Returns (strict, volatile) diff lists.

    Each entry is (kind, name, detail); kind is 'changed'|'added'|'removed'.
    strict covers configuration (config.json, uci, meta file_md5) and any
    unexpected file; volatile covers live-counter captures that change with
    normal traffic. Only strict diffs should fail a commissioned run.
    """
    base_names = set(snapshot_paths(base_dir)) | {"meta.json"}
    cur_names = set(snapshot_paths(cur_dir)) | {"meta.json"}
    strict, volatile = [], []

    for name in sorted(base_names - cur_names):
        (volatile if name in VOLATILE_CAPTURES else strict).append(("removed", name, ""))
    for name in sorted(cur_names - base_names):
        (volatile if name in VOLATILE_CAPTURES else strict).append(("added", name, ""))

    for name in sorted(base_names & cur_names):
        base_path = os.path.join(base_dir, name)
        cur_path = os.path.join(cur_dir, name)
        bucket = volatile if name in VOLATILE_CAPTURES else strict
        if name == "meta.json":
            detail = _meta_diff(base_path, cur_path)
            if detail is None:
                continue
            bucket.append(("changed", name, detail))
            continue
        if file_sha256(base_path) == file_sha256(cur_path):
            continue
        bucket.append(("changed", name, _text_diff(base_path, cur_path)))
    return strict, volatile


def _meta_diff(base_path, cur_path):
    """Compare only the meaningful payload (file_md5, host). None = no drift.

    captured_at always differs between captures and must not count.
    """
    with open(base_path) as f:
        base = json.load(f)
    with open(cur_path) as f:
        cur = json.load(f)
    lines = []
    if base.get("host") != cur.get("host"):
        lines.append(f"  host: {base.get('host')} -> {cur.get('host')}")
    base_md5, cur_md5 = base.get("file_md5", {}), cur.get("file_md5", {})
    for path in sorted(set(base_md5) | set(cur_md5)):
        b, c = base_md5.get(path, "<absent>"), cur_md5.get(path, "<absent>")
        if b != c:
            lines.append(f"  {path}: {b} -> {c}")
    return "\n".join(lines) if lines else None


def _text_diff(base_path, cur_path):
    with open(base_path, errors="replace") as f:
        base_lines = f.readlines()
    with open(cur_path, errors="replace") as f:
        cur_lines = f.readlines()
    diff = list(difflib.unified_diff(base_lines, cur_lines,
                                     fromfile="base", tofile="current", n=1))
    return "".join(diff[:80])


def format_diffs(strict, volatile):
    if not strict and not volatile:
        return "CLEAN — no drift detected"
    out = []
    if strict:
        out.append("=== DRIFT (strict) ===")
        for kind, name, detail in strict:
            out.append(f"[{kind.upper()}] {name}")
            if detail:
                out.append(detail)
    if volatile:
        out.append("=== notes (volatile captures — not config drift) ===")
        for kind, name, detail in volatile:
            out.append(f"[{kind.upper()}] {name}")
            if detail:
                out.append(detail[:400])
    return "\n".join(out)


def cmd_snapshot(args):
    out = args.out or os.path.join(
        DEFAULT_ROOT, time.strftime("%Y%m%dT%H%M%S") + f"-{args.host}"
    )
    capture(args.host, args.ssh_key, out)
    print(f"snapshot -> {out}")
    return 0


def cmd_diff(args):
    with open(os.path.join(args.base, "meta.json")) as f:
        base_host = json.load(f).get("host")
    if base_host and base_host != args.host:
        print(f"WARNING: base snapshot is host {base_host}, probing {args.host}",
              file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="live-snap-diff-") as tmp:
        capture(args.host, args.ssh_key, tmp)
        strict, volatile = compare_snapshots(args.base, tmp)
    print(format_diffs(strict, volatile))
    return 1 if strict else 0


def cmd_restore(args):
    base_config = os.path.join(args.base, "config.json")
    if not os.path.exists(base_config):
        print(f"no config.json in {args.base}", file=sys.stderr)
        return 2
    want = file_sha256(base_config)
    cur = ssh_run(args.host, "md5sum /etc/tollgate/config.json 2>/dev/null || echo MISSING")
    if cur.split()[0] == hashlib.md5(open(base_config, "rb").read()).hexdigest():
        print("config.json already matches base — nothing to restore")
        return 0
    if not args.yes:
        print("restore would overwrite /etc/tollgate/config.json; pass --yes", file=sys.stderr)
        return 2
    scp_args = ["scp", "-O", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
    if args.ssh_key:
        scp_args += ["-i", args.ssh_key]
    proc = subprocess.run(scp_args + [base_config, f"root@{args.host}:/etc/tollgate/config.json"],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        print(f"scp failed: {proc.stderr.strip()}", file=sys.stderr)
        return 2
    got = ssh_run(args.host, "md5sum /etc/tollgate/config.json").split()[0]
    if got != hashlib.md5(open(base_config, "rb").read()).hexdigest():
        print("post-restore md5 mismatch — verify manually", file=sys.stderr)
        return 2
    print("config.json restored (md5 verified)")
    if args.restart:
        ssh_run(args.host, "/etc/init.d/tollgate-wrt restart", timeout=60)
        print("backend restart issued")
    else:
        print("apply with: ssh root@%s '/etc/init.d/tollgate-wrt restart'" % args.host)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--host", default=os.environ.get("TOLLGATE_SSH_HOST", "192.168.1.1"))
        p.add_argument("--ssh-key", default=os.environ.get("TOLLGATE_SSH_KEY"))

    p = sub.add_parser("snapshot"); common(p)
    p.add_argument("--out", default=None); p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("diff"); common(p)
    p.add_argument("--base", required=True); p.set_defaults(func=cmd_diff)

    p = sub.add_parser("restore"); common(p)
    p.add_argument("--base", required=True)
    p.add_argument("--restart", action="store_true")
    p.add_argument("--yes", action="store_true"); p.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (SshError, subprocess.TimeoutExpired) as e:
        print(f"ssh error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
