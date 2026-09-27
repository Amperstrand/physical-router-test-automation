#!/usr/bin/env python3
"""verify-release-channel.py — consumer-side verification of the TollGate Nostr release channel.

Discovers kind-1063 (NIP-94) release events via `nak`, downloads each artifact
from >=N mirrors, verifies sha256 against the event's `x` tag, and validates
the ipk/apk container structure (including the inner control version stamp).

Channel spec (upstream AGENTS.md):
  - publisher key 5075e61f0b048148b60105c1dd72bbeae1957336ae5824087e52efa374f8416a
  - tags: url (one per mirror), x/ox (sha256), filename, n=tollgate-wrt,
    v=<version>, c=<stable|beta|alpha|dev>, A=<arch>, format=<ipk|apk>,
    compression=<none|upx-*>

Relay-filtering gotchas (learned in the 2026-09-17 probe, see PRTA #112):
  - combined single-letter tag filters (A= + v=) return 0 events on some
    relays (index limitation) -> query with n= + c= + author only, filter
    v/A/format client-side.
  - individual relays are frequently down (damus 503, orangesync relay1
    timeouts) -> nak must be allowed to fail per-relay; success requires
    >=1 relay delivering the events.

Usage:
    scripts/verify-release-channel.py --version v0.5.0                 # dry-run vs stable
    scripts/verify-release-channel.py --version latest --channel stable
    scripts/verify-release-channel.py --version v0.6.0 --mirrors 3 --keep-downloads
    scripts/verify-release-channel.py --version v0.5.0 --arch aarch64_cortex-a72

Exit codes: 0 = all checks passed, 1 = verification failure (bad hash /
structure / missing expected artifact), 2 = discovery failure (no events
from any relay), 3 = usage error.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_RELAYS = [
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://nostr.mom",
    "wss://relay1.orangesync.tech",
    "wss://relay2.orangesync.tech",
]

# Publisher key (hex) from upstream AGENTS.md channel spec
# (npub12p67v8ctqjq53dspqhqa6u4matse2uek4evzgzr72th6xa8cg94qxks7ks).
DEFAULT_PUBLISHER = "5075e61f0b048148b60105c1dd72bbeae1957336ae5824087e52efa374f8416a"

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- data model


@dataclass
class MirrorResult:
    url: str
    status: str  # "ok" | "http_404" | "http_<code>" | "curl_error" | "hash_mismatch"
    bytes_downloaded: int = 0
    sha256: str = ""
    elapsed_s: float = 0.0
    detail: str = ""


@dataclass
class Artifact:
    event_id: str
    filename: str
    arch: str
    format: str
    version: str
    channel: str
    compression: str
    x_tag: str
    ox_tag: str
    urls: list[str]
    created_at: int
    pubkey: str
    sig: str
    mirrors: list[MirrorResult] = field(default_factory=list)
    structure: dict = field(default_factory=dict)
    verdict: str = "pending"  # pending | pass | fail | warn

    @property
    def verified_downloads(self) -> int:
        return sum(1 for m in self.mirrors if m.status == "ok")


@dataclass
class Report:
    started_at: str
    finished_at: str
    args: dict
    relay_stderr: str
    events_seen: int
    artifacts: list[Artifact]
    duplicates: list[str]

    def to_json(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "args": self.args,
            "relay_stderr": self.relay_stderr,
            "events_seen": self.events_seen,
            "duplicates": self.duplicates,
            "artifacts": [
                {
                    "event_id": a.event_id,
                    "filename": a.filename,
                    "arch": a.arch,
                    "format": a.format,
                    "version": a.version,
                    "channel": a.channel,
                    "compression": a.compression,
                    "x_tag": a.x_tag,
                    "urls": a.urls,
                    "created_at": a.created_at,
                    "pubkey": a.pubkey,
                    "sig": a.sig,
                    "mirrors": [m.__dict__ for m in a.mirrors],
                    "structure": a.structure,
                    "verdict": a.verdict,
                }
                for a in self.artifacts
            ],
        }


# --------------------------------------------------------------------------- discovery


def nak_discover(
    relays: list[str], publisher: str, channel: str, limit: int, timeout: int
) -> tuple[list[dict], str]:
    """Run `nak req` and return (events, stderr). Raises RuntimeError on hard failure."""
    cmd = [
        "nak",
        "req",
        *relays,
        "-k",
        "1063",
        "-a",
        publisher,
        "--tag",
        "n=tollgate-wrt",
        "-l",
        str(limit),
    ]
    if channel != "any":
        cmd += ["--tag", f"c={channel}"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        # nak may still have flushed events before the timeout; that path is
        # handled by the caller via an empty result -> raise instead.
        raise RuntimeError(f"nak timed out after {timeout}s")

    events: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("kind") == 1063:
            events.append(ev)
    return events, proc.stderr


def tags_of(event: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for tag in event.get("tags", []):
        if len(tag) >= 2:
            out.setdefault(tag[0], []).append(tag[1])
    return out


def event_to_artifact(event: dict) -> Artifact:
    t = tags_of(event)
    first = lambda k, d="": t.get(k, [d])[0]  # noqa: E731
    return Artifact(
        event_id=event.get("id", "?"),
        filename=first("filename"),
        arch=first("A"),
        format=first("format"),
        version=first("v"),
        channel=first("c"),
        compression=first("compression", "none"),
        x_tag=first("x"),
        ox_tag=first("ox"),
        urls=t.get("url", []),
        created_at=event.get("created_at", 0),
        pubkey=event.get("pubkey", ""),
        sig=event.get("sig", ""),
    )


def filter_events(
    events: list[dict], version: str, arch: Optional[str], fmt: Optional[str]
) -> tuple[list[Artifact], list[str], int]:
    """Client-side filtering (relay-side v/A combo filtering is unreliable)."""
    artifacts: list[Artifact] = []
    rejected: list[str] = []
    for ev in events:
        a = event_to_artifact(ev)
        if version not in ("latest", "any") and a.version != version:
            rejected.append(f"{a.filename or a.event_id}: v={a.version!r} != {version!r}")
            continue
        if arch and a.arch != arch:
            rejected.append(f"{a.filename or a.event_id}: A={a.arch!r} != {arch!r}")
            continue
        if fmt and a.format != fmt:
            rejected.append(f"{a.filename or a.event_id}: format={a.format!r} != {fmt!r}")
            continue
        artifacts.append(a)
    # latest -> keep only the newest event per (arch, format)
    if version == "latest":
        newest: dict[tuple[str, str], Artifact] = {}
        for a in artifacts:
            key = (a.arch, a.format)
            if key not in newest or a.created_at > newest[key].created_at:
                newest[key] = a
        artifacts = list(newest.values())
    artifacts.sort(key=lambda a: (a.format, a.arch))
    return artifacts, rejected, len(events)


# --------------------------------------------------------------------------- download + hash


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_mirror(url: str, dest: Path, max_time: int) -> tuple[str, int, str]:
    """Returns (status, http_code, detail). dest written only on HTTP 200."""
    proc = subprocess.run(
        [
            "curl",
            "-fsSL",
            "--max-time",
            str(max_time),
            "--output",
            str(dest),
            "--write-out",
            "%{http_code}",
            url,
        ],
        capture_output=True,
        text=True,
    )
    code = proc.stdout.strip()[-3:] if proc.stdout.strip() else ""
    if proc.returncode == 0 and dest.exists():
        return "ok", 200 if not code.isdigit() else int(code), ""
    # -f makes curl fail on HTTP >= 400 without writing a body; map the code
    detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
    if code.isdigit() and int(code) >= 400:
        return f"http_{code}", int(code), detail
    if proc.returncode == 28:
        return "timeout", 0, detail
    return "curl_error", int(code) if code.isdigit() else 0, detail


def verify_artifact(a: Artifact, mirrors_needed: int, workdir: Path, keep: bool, max_time: int) -> Path | None:
    """Download from mirrors until mirrors_needed hash-verified copies exist.

    Records every attempt in a.mirrors. Returns path to a verified download
    (the first one) or None if no mirror ever produced a hash match.
    """
    verified_path: Path | None = None
    for url in a.urls:
        if a.verified_downloads >= mirrors_needed:
            break
        host = urllib.parse.urlparse(url).netloc
        name = f"{a.x_tag[:16]}-{host}.bin" if len(a.urls) > 1 else f"{a.x_tag[:16]}.bin"
        dest = workdir / name
        t0 = time.monotonic()
        status, http_code, detail = download_mirror(url, dest, max_time)
        elapsed = time.monotonic() - t0
        if status == "ok":
            digest = sha256_file(dest)
            if digest == a.x_tag:
                a.mirrors.append(
                    MirrorResult(url, "ok", dest.stat().st_size, digest, elapsed)
                )
                if verified_path is None:
                    verified_path = dest
                elif not keep:
                    dest.unlink(missing_ok=True)
            else:
                a.mirrors.append(
                    MirrorResult(url, "hash_mismatch", dest.stat().st_size, digest, elapsed,
                                 f"x tag {a.x_tag}")
                )
                dest.unlink(missing_ok=True)
        else:
            a.mirrors.append(MirrorResult(url, status, 0, "", elapsed, detail))
            dest.unlink(missing_ok=True)
    return verified_path


# --------------------------------------------------------------------------- package structure


def _read_control_tarball(payload: bytes) -> tuple[dict, str]:
    """Parse a control.tar.{gz,zst} payload into ({fields}, status)."""
    fields: dict = {}
    if payload[:2] == b"\x1f\x8b":
        try:
            payload = gzip.decompress(payload)
        except OSError as e:
            return fields, f"unreadable: {e}"
    try:
        with tarfile.open(fileobj=io.BytesIO(payload)) as tf:
            control_bytes = None
            for member in tf.getmembers():
                if member.name.lstrip("./") == "control":
                    control_bytes = tf.extractfile(member).read()  # type: ignore[union-attr]
                    break
        if control_bytes is None:
            return fields, "missing in control.tar"
        for line in control_bytes.decode("utf-8", "replace").splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                fields[k.strip()] = v.strip()
        return fields, "ok"
    except (OSError, tarfile.TarError) as e:
        return fields, f"unreadable: {e}"


def check_ipk(blob: bytes, expected_version: str, expected_arch: str) -> dict:
    """An .ipk contains debian-binary, control.tar.*, data.tar.* — the
    container is either a gzip-compressed POSIX tar (what our CI publishes)
    or a (optionally gzip-compressed) ar archive (Debian style)."""
    out: dict = {"format": "ipk", "checks": {}, "control_fields": {}}
    if blob[:2] == b"\x1f\x8b":
        try:
            blob = gzip.decompress(blob)
            out["checks"]["gzip"] = "ok"
        except OSError as e:
            out["checks"]["gzip"] = f"fail: {e}"
            out["error"] = "invalid gzip stream"
            return out
    else:
        out["checks"]["gzip"] = "not-compressed"

    members: dict[str, bytes] = {}
    if blob[:8] == b"!<arch>\n":
        out["checks"]["container"] = "ar"
        off = 8
        while off + 60 <= len(blob):
            hdr = blob[off : off + 60]
            name = hdr[0:16].decode("ascii", "replace").strip()
            size = int(hdr[48:58].decode("ascii", "replace").strip())
            members[name.rstrip("/")] = blob[off + 60 : off + 60 + size]
            off += 60 + size + (size % 2)
    else:
        try:
            with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
                for member in tf.getmembers():
                    members[member.name.lstrip("./")] = tf.extractfile(member).read()  # type: ignore[union-attr]
            out["checks"]["container"] = "tar"
        except (OSError, tarfile.TarError) as e:
            out["error"] = f"neither ar nor tar container: {e}"
            return out
    if not members:
        out["error"] = "empty package container"
        return out
    out["members"] = sorted(members.keys())
    out["checks"]["debian_binary"] = (
        "ok" if members.get("debian-binary", b"").strip() == b"2.0" else "missing/invalid"
    )
    has_control = any(m.startswith("control.tar") for m in members)
    has_data = any(m.startswith("data.tar") for m in members)
    out["checks"]["control_tar"] = "ok" if has_control else "missing"
    out["checks"]["data_tar"] = "ok" if has_data else "missing"

    if has_control:
        ctl_blob = next(members[m] for m in members if m.startswith("control.tar"))
        fields, status = _read_control_tarball(ctl_blob)
        out["checks"]["control_file"] = status
        out["control_fields"] = fields

    cf = out["control_fields"]
    want_ver = expected_version.lstrip("v")
    got_ver = cf.get("Version", "")
    out["checks"]["version_stamp"] = (
        "ok" if want_ver and want_ver in got_ver else f"mismatch: control {got_ver!r} vs event {expected_version!r}"
    )
    out["checks"]["arch_stamp"] = (
        "ok" if cf.get("Architecture") in (expected_arch, "all") else f"mismatch: control {cf.get('Architecture')!r} vs event {expected_arch!r}"
    )
    hard = out["checks"]["debian_binary"] == "ok" and has_control and has_data
    if not hard:
        out["error"] = "ipk container incomplete"
    return out


def check_apk(blob: bytes, expected_version: str, expected_arch: str) -> dict:
    """Alpine packages: APKv3 ("ADBd" magic, apk-tools 3.x binary ADB format —
    what current OpenWrt apk targets use; deep parsing needs apk-tools) or
    APKv2 (gzip'd newc tar with .PKGINFO)."""
    out: dict = {"format": "apk", "checks": {}, "pkginfo_fields": {}}
    if blob[:4] == b"ADBd":
        out["checks"]["magic"] = "APKv3 (ADB)"
        out["head_hex"] = blob[:32].hex()
        out["checks"]["version_stamp"] = (
            "not-verifiable-client-side (APKv3 ADB; verify at install with apk-tools)"
        )
        out["checks"]["arch_stamp"] = "not-verifiable-client-side"
        return out
    try:
        if blob[:2] == b"\x1f\x8b":
            blob = gzip.decompress(blob)
            out["checks"]["gzip"] = "ok"
        else:
            out["checks"]["gzip"] = "not-compressed"
        with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
            names = tf.getnames()
            out["members_head"] = names[:5]
            if ".PKGINFO" in names:
                out["checks"]["pkginfo"] = "ok"
                data = tf.extractfile(".PKGINFO").read().decode("utf-8", "replace")  # type: ignore[union-attr]
                for line in data.splitlines():
                    if "=" in line:
                        k, _, v = line.partition("=")
                        out["pkginfo_fields"][k.strip()] = v.strip()
            else:
                out["checks"]["pkginfo"] = "missing"
    except (OSError, tarfile.TarError) as e:
        out["error"] = f"not a valid apk tar: {e}"
        return out

    pf = out["pkginfo_fields"]
    want_ver = expected_version.lstrip("v")
    got_ver = pf.get("pkgver", "")
    out["checks"]["version_stamp"] = (
        "ok" if want_ver and want_ver in got_ver else f"mismatch: pkginfo {got_ver!r} vs event {expected_version!r}"
    )
    out["checks"]["arch_stamp"] = (
        "ok" if pf.get("arch") in (expected_arch, "noarch", "all") else f"mismatch: pkginfo {pf.get('arch')!r} vs event {expected_arch!r}"
    )
    if out["checks"]["pkginfo"] != "ok":
        out["error"] = "apk container incomplete"
    return out


# --------------------------------------------------------------------------- main


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", required=True,
                   help="exact version tag (e.g. v0.5.0), 'latest', or 'any'")
    p.add_argument("--channel", default="stable", help="stable|beta|alpha|dev|any (default stable)")
    p.add_argument("--arch", help="filter to one architecture (client-side)")
    p.add_argument("--format", choices=["ipk", "apk"], help="filter to one package format")
    p.add_argument("--relays", nargs="+", default=DEFAULT_RELAYS)
    p.add_argument("--publisher", default=DEFAULT_PUBLISHER, help="expected publisher hex key")
    p.add_argument("--mirrors", type=int, default=2,
                   help="number of hash-verified mirror downloads required per artifact (default 2)")
    p.add_argument("--strict-mirrors", action="store_true",
                   help="treat mirror-redundancy shortfall as failure instead of warning")
    p.add_argument("--limit", type=int, default=200, help="nak event limit (default 200)")
    p.add_argument("--nak-timeout", type=int, default=90)
    p.add_argument("--dl-timeout", type=int, default=180, help="per-mirror curl --max-time")
    p.add_argument("--out-dir", help="evidence dir (default results/release-channel/<...>-<ts>)")
    p.add_argument("--keep-downloads", action="store_true",
                   help="keep all verified mirror copies in the evidence dir")
    args = p.parse_args()

    started = datetime.now(timezone.utc)
    stem = f"{args.channel}-{args.version}-{started.strftime('%Y%m%dT%H%M%SZ')}"
    out_dir = Path(args.out_dir) if args.out_dir else REPO_ROOT / "results" / "release-channel" / stem
    out_dir.mkdir(parents=True, exist_ok=True)
    dl_dir = out_dir / "downloads"
    dl_dir.mkdir(exist_ok=True)

    print(f"== TollGate release-channel verification ==")
    print(f"version={args.version} channel={args.channel} arch={args.arch or 'all'} "
          f"format={args.format or 'all'} mirrors={args.mirrors}")
    print(f"evidence: {out_dir}")

    # -- discovery
    print("\n[1/3] discovering kind-1063 events via nak ...")
    try:
        events, stderr = nak_discover(
            args.relays, args.publisher, args.channel, args.limit, args.nak_timeout
        )
    except RuntimeError as e:
        print(f"DISCOVERY FAILED: {e}")
        return 2
    (out_dir / "nak-stderr.log").write_text(stderr)
    (out_dir / "events.json").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    relay_summary = [l for l in stderr.splitlines() if "connecting" in l or "status" in l]
    for l in relay_summary:
        print(f"      {l}")

    artifacts, rejected, seen = filter_events(events, args.version, args.arch, args.format)
    print(f"      events seen: {seen}, matching filters: {len(artifacts)}")
    for r in rejected[:5]:
        print(f"      filtered out: {r}")
    if len(rejected) > 5:
        print(f"      ... and {len(rejected) - 5} more filtered")

    if not artifacts:
        print("DISCOVERY FAILED: no artifacts match the requested version/arch/format.")
        print("If the version was never published to this channel, that is the finding — see PRTA #112.")
        return 2

    # duplicates: same (arch, format) from multiple events
    seen_keys: dict[tuple[str, str], Artifact] = {}
    duplicates: list[str] = []
    for a in artifacts:
        key = (a.arch, a.format)
        if key in seen_keys:
            duplicates.append(
                f"{a.format}/{a.arch}: {seen_keys[key].event_id[:8]} (created {seen_keys[key].created_at}) "
                f"vs {a.event_id[:8]} (created {a.created_at})"
            )
        else:
            seen_keys[key] = a

    # event-level sanity
    print("\n[2/3] verifying artifacts (download + sha256 + structure) ...")
    verified_any = False
    for a in artifacts:
        print(f"\n  -- {a.filename}  [{a.format}/{a.arch}] x={a.x_tag[:16]}…")
        problems: list[str] = []
        warnings: list[str] = []
        if a.x_tag and len(a.x_tag) != 64:
            problems.append(f"x tag malformed ({len(a.x_tag)} chars)")
        if not a.urls:
            problems.append("no url tags")
        if a.pubkey != args.publisher:
            problems.append(f"unexpected publisher {a.pubkey[:12]}…")
        if a.compression not in ("none",) and not a.compression.startswith("upx"):
            problems.append(f"unknown compression {a.compression!r}")

        path = verify_artifact(a, args.mirrors, dl_dir, args.keep_downloads, args.dl_timeout)
        for m in a.mirrors:
            mark = "OK " if m.status == "ok" else "!! "
            extra = f" {m.detail[:70]}" if m.detail else ""
            print(f"     mirror {mark} {m.status:<14} {m.elapsed_s:5.1f}s "
                  f"{m.bytes_downloaded:>10,}B  {urllib.parse.urlparse(m.url).netloc}{extra}")
            if m.status == "hash_mismatch":
                problems.append(f"hash mismatch from {urllib.parse.urlparse(m.url).netloc}: "
                                f"got {m.sha256[:16]}…, x tag says {a.x_tag[:16]}…")
        if path is None:
            problems.append("no mirror produced a hash-matching download")
        else:
            verified_any = True
            blob = path.read_bytes()
            if a.format == "apk":
                a.structure = check_apk(blob, a.version, a.arch)
            else:
                a.structure = check_ipk(blob, a.version, a.arch)
            for k, v in a.structure.get("checks", {}).items():
                print(f"     {k:<16} {v}")
            if a.structure.get("error"):
                problems.append(f"structure: {a.structure['error']}")
            if str(a.structure.get("checks", {}).get("version_stamp", "")).startswith("mismatch"):
                # stamped artifacts must carry the release version; this is a
                # publish-pipeline defect, not a transport one
                problems.append(f"version stamp: {a.structure['checks']['version_stamp']}")
        if a.verified_downloads < args.mirrors:
            redundancy = (f"mirror redundancy: only {a.verified_downloads}/{args.mirrors} "
                          f"hash-verified downloads available")
            if args.strict_mirrors:
                problems.append(redundancy)
            else:
                warnings.append(redundancy)

        if problems:
            a.verdict = "fail"
        elif warnings:
            a.verdict = "warn"
        else:
            a.verdict = "pass"
        if problems or warnings:
            print(f"     VERDICT: {a.verdict.upper()}")
            for pr in problems:
                print(f"       - FAIL  {pr}")
            for w in warnings:
                print(f"       - WARN  {w}")
        else:
            print(f"     VERDICT: PASS ({a.verified_downloads} mirror(s) verified)")

    finished = datetime.now(timezone.utc)
    report = Report(
        started_at=started.isoformat(), finished_at=finished.isoformat(),
        args=vars(args), relay_stderr=stderr, events_seen=seen,
        artifacts=artifacts, duplicates=duplicates,
    )
    (out_dir / "report.json").write_text(json.dumps(report.to_json(), indent=2) + "\n")

    # -- console summary
    passed = sum(1 for a in artifacts if a.verdict == "pass")
    warned = sum(1 for a in artifacts if a.verdict == "warn")
    failed = sum(1 for a in artifacts if a.verdict == "fail")
    total_urls = sum(len(a.urls) for a in artifacts)
    ok_urls = sum(1 for a in artifacts for m in a.mirrors if m.status == "ok")
    dead = {}
    for a in artifacts:
        for m in a.mirrors:
            if m.status != "ok":
                dead.setdefault(urllib.parse.urlparse(m.url).netloc, m.status)
    print(f"\n[3/3] summary")
    print(f"  artifacts: {len(artifacts)} ({passed} pass, {warned} warn, {failed} fail)")
    print(f"  mirror attempts: {ok_urls}/{total_urls} urls listed; verified downloads: "
          f"{sum(a.verified_downloads for a in artifacts)}")
    if dead:
        print(f"  unhealthy mirrors: {', '.join(f'{h}={s}' for h, s in sorted(dead.items()))}")
    if duplicates:
        print(f"  duplicate arch/format events: {len(duplicates)}")
        for d in duplicates:
            print(f"    {d}")
    print(f"  evidence: {out_dir}")

    if failed:
        print("\nRESULT: FAIL")
        return 1
    if not verified_any:
        print("\nRESULT: FAIL (nothing could be verified)")
        return 1
    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
