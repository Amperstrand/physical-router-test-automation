import logging
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("tollgate.deploy")

REPO = "OpenTollGate/tollgate-module-basic-go"
WORKFLOW = "Build and Publish"
BUILD_DIR = Path("/tmp/tollgate-build")
COORDINATION_RELAYS = [
    "wss://relay.cashu.email",
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://nostr.mom",
    "wss://relay1.orangesync.tech",
]

#: Nostr pubkey CI uses to sign all release/build events (kind 1063 + 30078).
#: Matches AGENTS.md "Publisher pubkey".
#: NOTE: the CI bot key was rotated. The resolver now queries WITHOUT an author
#: filter (-a) and relies on the n=tollgate-wrt tag instead, so it works
#: regardless of which key the CI uses. Both keys are listed here for reference.
NOSTR_PUBLISHER_PUBKEY = "5075e61f0b048148b60105c1dd72bbeae1957336ae5824087e52efa374f8416a"
NOSTR_PUBLISHER_PUBKEY_NEW = "76c714199ad17278276d4cd51ddec7d0df0715a91b2f2f03f16c03925b3a0911"

#: Blossom servers tried in order when the primary URL from a NIP-94 event
#: returns 404. CI publishes to all of these (BLOSSOM_MIN_SUCCESS=2), but
#: free-tier files on blossom.psbt.me expire after a TTL, so the same
#: content-addressed blob (SHA256) is retried on the mirrors.
BLOSSOM_MIRROR_SERVERS = [
    "https://blossom.psbt.me",
    "https://blossom.primal.net",
    "https://blossom1.orangesync.tech",
]

# Packages required by the test framework on the router.
# Factory reset wipes all opkg packages; these must be reinstalled.
TEST_DEPS = ["curl", "socat", "nodogsplash", "jq", "luci", "px5g-mbedtls"]


def detect_package_manager(router) -> str:
    """Detect whether the router uses opkg (24.x) or apk (25.x).

    OpenWRT 25.x ships apk-tools and does NOT have opkg.
    24.x and earlier ship opkg and do NOT have apk.
    """
    try:
        out = router.ssh("command -v apk >/dev/null 2>&1 && echo apk || echo opkg", timeout=10)
        pm = out.strip()
        if pm in ("apk", "opkg"):
            return pm
    except Exception:
        pass
    return "opkg"


def detect_arch(router) -> str:
    """Detect the package architecture from a running OpenWrt router via SSH.

    Tries ``opkg print-architecture`` first (24.x), then ``/etc/apk/arch``
    (25.x), then falls back to ``/etc/openwrt_release`` DISTRIB_ARCH.
    """
    # opkg path (24.x)
    try:
        out = router.ssh("opkg print-architecture 2>/dev/null", timeout=10)
    except Exception:
        out = ""

    if out:
        best_name, best_prio = None, -1
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "arch":
                name, prio = parts[1], int(parts[2])
                if name in ("all", "noarch"):
                    continue
                if prio > best_prio:
                    best_name, best_prio = name, prio
        if best_name:
            log.info("Detected router arch via opkg: %s", best_name)
            return best_name

    # apk path (25.x)
    try:
        out = router.ssh("cat /etc/apk/arch 2>/dev/null", timeout=10)
        arch = out.strip()
        if arch:
            log.info("Detected router arch via apk: %s", arch)
            return arch
    except Exception:
        pass

    # Fallback: /etc/openwrt_release
    try:
        out = router.ssh(". /etc/openwrt_release && echo $DISTRIB_ARCH", timeout=10)
        if out.strip():
            log.info("Detected router arch via openwrt_release: %s", out.strip())
            return out.strip()
    except Exception:
        pass

    raise RuntimeError("Cannot detect router architecture via SSH")


def _ssh_env():
    pw = os.environ.get("TOLLGATE_SSH_PASSWORD") or os.environ.get("TOLLGATE_LUCI_PASSWORD")
    if not pw:
        return os.environ
    env = os.environ.copy()
    env["SSHPASS"] = pw
    return env


def _scp_to_router(router, local_path, remote_path):
    ssh_opts = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "LogLevel=ERROR",
    ]
    if router.identity_file:
        cmd = ["scp", "-O", "-i", router.identity_file] + ssh_opts
    else:
        pw = os.environ.get("TOLLGATE_SSH_PASSWORD") or os.environ.get("TOLLGATE_LUCI_PASSWORD")
        cmd = (["sshpass", "-e", "scp", "-O"] if pw else ["scp", "-O"]) + ssh_opts

    if router.jump_host:
        cmd += ["-J", router.jump_host]

    if router.port:
        cmd += ["-P", str(router.port)]

    cmd += [str(local_path), f"root@{router.host}:{remote_path}"]
    r = subprocess.run(cmd, capture_output=True, timeout=120, env=_ssh_env())
    if r.returncode != 0:
        raise RuntimeError(f"SCP failed (exit {r.returncode}): {r.stderr.decode(errors='replace').strip()[:300]}")


def _transfer_to_router(router, local_path, remote_path):
    """Transfer a binary file to router via SCP (bytes mode) with size verification."""
    _scp_to_router(router, local_path, remote_path)

    file_path = Path(local_path)
    local_size = file_path.stat().st_size
    remote_size_out = router.ssh(f"wc -c < {remote_path}", timeout=10).strip()
    remote_size = int(remote_size_out) if remote_size_out else 0
    if remote_size != local_size:
        raise RuntimeError(
            f"Transfer size mismatch: local={local_size} remote={remote_size} for {file_path.name}"
        )
    log.info("Transfer verified: %d bytes", local_size)


def _repack_ar_to_targz(ipk_path: Path) -> None:
    """Convert an ar-wrapped (Debian format) ipk to gzip tar (OpenWrt format).

    Some CI build systems (e.g. configurationwizzard) produce ipks in
    Debian's ar archive format.  OpenWrt's opkg expects gzip-compressed
    tar archives containing ``./debian-binary``, ``./control.tar.gz``,
    and ``./data.tar.gz``.  This function extracts the ar members and
    repacks them into the correct format.

    If the file is already gzip tar format, this is a no-op.
    """
    import tarfile
    import io

    with open(ipk_path, "rb") as f:
        data = f.read()

    # Already gzip tar format — nothing to do
    if data[:2] == b"\x1f\x8b":
        return

    ar_magic = b"!<arch>\n"
    if not data.startswith(ar_magic):
        return

    # Parse ar archive: extract debian-binary, control.tar.gz, data.tar.gz
    members: dict[str, bytes] = {}
    pos = len(ar_magic)

    while pos < len(data):
        if pos + 60 > len(data):
            break
        header = data[pos:pos + 60]
        name_field = header[0:16].decode("ascii", errors="replace")
        size_field = header[48:58].decode("ascii", errors="replace").strip()
        if not size_field:
            break
        member_size = int(size_field)

        # Strip trailing spaces and slashes from name
        name = name_field.strip().rstrip("/")
        content_start = pos + 60
        content_end = content_start + member_size
        if content_end > len(data):
            break

        members[name] = data[content_start:content_end]

        # ar pads to 2-byte boundary
        if member_size % 2:
            content_end += 1
        pos = content_end

    if not members:
        log.warning("No ar members found in %s — skipping repack", ipk_path.name)
        return

    log.info(
        "Repacking ar→targz: %s (members: %s)",
        ipk_path.name, ", ".join(members.keys()),
    )

    # Write gzip tar with ./ prefix (matching OpenWrt's buildroot output)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name=f"./{name}")
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))

    with open(ipk_path, "wb") as f:
        f.write(buf.getvalue())

    log.info("Repacked %s: %d bytes ar → %d bytes tar.gz",
             ipk_path.name, len(data), ipk_path.stat().st_size)


def _parse_version(opkg_line):
    if not opkg_line:
        return None
    parts = opkg_line.split()
    return parts[2] if len(parts) >= 3 else (parts[1] if len(parts) >= 2 else opkg_line)


def _write_rust_compat_config(router):
    from lib.constants import TEST_MINT_URL, DEFAULT_STEP_SIZE_MS
    config = {
        "accepted_mints": [{
            "url": TEST_MINT_URL,
            "min_balance": 0,
            "balance_tolerance_percent": 0,
            "payout_interval_seconds": 60,
            "min_payout_amount": 0,
            "price_per_step": 1,
            "price_unit": "sat",
            "purchase_min_steps": 0,
        }],
        "step_size": DEFAULT_STEP_SIZE_MS,
        "metric": "milliseconds",
        "profit_share": [
            {"factor": 1.0, "identity": "operator"},
        ],
    }
    router.write_remote_json("/etc/tollgate/config.json", config)
    log.info("Wrote Rust-compat config.json with mint=%s", TEST_MINT_URL)


def _wait_for_health(router, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if router.api_status("/") == 200:
            return True
        time.sleep(2)
    return False


def _wait_for_reboot(router, timeout=180):
    log.info("Waiting for router to come back online...")
    time.sleep(10)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = router.ssh("echo UP", timeout=5)
            if "UP" in out:
                log.info("Router is back online")
                return True
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, Exception):
            pass
        time.sleep(5)
    return False


def install_test_deps(router):
    log.info("Installing test dependencies: %s", ", ".join(TEST_DEPS))
    pm = detect_package_manager(router)
    if pm == "apk":
        router.ssh("apk update", timeout=60)
        router.ssh(f"apk add {' '.join(TEST_DEPS)}", timeout=120)
    else:
        router.ssh("opkg update", timeout=60)
        router.ssh(f"opkg install {' '.join(TEST_DEPS)}", timeout=120)
    log.info("Test dependencies installed")


def _list_workflow_runs(
    repo: str,
    workflow: str,
    *,
    branch: str | None = None,
    commit: str | None = None,
    limit: int = 10,
) -> list[dict]:
    cmd = [
        "gh", "run", "list",
        "--repo", repo,
        "--workflow", workflow,
        "--limit", str(limit),
        "--json", "databaseId,status,conclusion,headBranch,headSha,createdAt",
    ]
    if commit:
        cmd.extend(["--commit", commit])
    elif branch:
        cmd.extend(["--branch", branch])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if r.returncode != 0:
        err = r.stderr.strip() or r.stdout.strip() or "unknown gh error"
        raise RuntimeError(f"gh run list failed: {err}")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse gh run list output: {exc}") from exc
    return data if isinstance(data, list) else []


def _run_has_arch_artifact(repo: str, run_id: str, arch: str) -> bool:
    """Return True if the workflow run has a downloadable .ipk for arch.

    Uses the GitHub API to check artifact names without downloading.
    Falls back to download-based check if the API call fails.
    """
    try:
        r = subprocess.run(
            ["gh", "api",
             f"repos/{repo}/actions/runs/{run_id}/artifacts",
             "--paginate", "-q", ".artifacts[].name"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                if arch in line and ".ipk" in line and "upx" not in line:
                    log.info("Found artifact '%s' via API check", line)
                    return True
            return False
    except (subprocess.TimeoutExpired, Exception):
        pass

    with tempfile.TemporaryDirectory(prefix="tollgate-artifact-check-") as tmp:
        r = subprocess.run(
            ["gh", "run", "download", run_id, "--repo", repo, "--dir", tmp],
            capture_output=True, text=True, timeout=300, check=False,
        )
        if r.returncode != 0:
            return False
        matches = [
            p for p in Path(tmp).rglob("*.ipk")
            if p.is_file() and arch in p.name and "upx" not in p.name
        ]
        return bool(matches)


def _watch_run(repo: str, run_id: str, timeout_s: int) -> bool:
    """Wait for a workflow run to finish. Returns True if watch succeeded."""
    r = subprocess.run(
        [
            "gh", "run", "watch", run_id,
            "--repo", repo,
            "--exit-status",
            "--interval", "15",
        ],
        capture_output=True,
        text=True,
        timeout=max(timeout_s, 60),
        check=False,
    )
    return r.returncode == 0


def _probe_workflow_lane_dead(
    repo: str, workflow: str, dormant_after_days: int = 3
) -> str | None:
    """Classify the GitHub workflow lane as dead/dormant, or None if healthy.

    Returns a human-readable reason when the lane cannot produce artifacts:
    the workflow has never run, or its newest run is older than
    ``dormant_after_days``. Returns None when the lane looks alive (a push
    may still trigger it), so the caller keeps waiting.
    """
    try:
        runs = _list_workflow_runs(repo, workflow, limit=1)
    except RuntimeError:
        return f"workflow '{workflow}' is not listable for {repo}"
    if not runs:
        return f"workflow '{workflow}' has never run for {repo}"
    created = str(runs[0].get("createdAt") or "")
    if created:
        try:
            newest = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            return None  # unparseable timestamp — do not fail on bad data
        age_days = (datetime.now(timezone.utc) - newest).days
        if age_days > dormant_after_days:
            return (
                f"workflow '{workflow}' is dormant for {repo} "
                f"(newest run {age_days} days old)"
            )
    return None


def ensure_artifact(
    *,
    branch: str,
    arch: str,
    repo: str,
    workflow: str,
    commit: str | None = None,
    timeout_s: int = 1800,
    fmt: str = "",
    probe_after_polls: int = 10,
) -> str:
    """Wait until a CI run has a downloadable artifact for arch. Never triggers builds.

    Returns the GitHub Actions run database ID, or 'blossom' if the artifact
    is available on Blossom via Nostr.
    """
    deadline = time.time() + timeout_s
    empty_polls = 0

    while time.time() < deadline:
        # Try Blossom/Nostr first (instant if nak is available).
        # Pass branch so the resolver only returns artifacts whose filename
        # contains _{branch}. — without it, a commit miss silently falls back
        # to the newest artifact of ANY branch (e.g. feat-v3-rebase when
        # testing main), deploying the wrong firmware.
        blossom_binary = _resolve_blossom_binary(commit, arch, fmt=fmt, branch=branch)
        if blossom_binary:
            log.info(
                "Found artifact '%s' via Blossom/Nostr",
                blossom_binary.get("filename", "?"),
            )
            return "blossom"

        # Fall back to GitHub Actions artifact API
        try:
            runs = _list_workflow_runs(repo, workflow, branch=branch, commit=commit, limit=15)
        except RuntimeError as exc:
            log.warning("%s", exc)
            runs = []

        # gh run list --commit sometimes returns empty for older commits;
        # fall back to branch-scoped search and filter by SHA client-side.
        if commit and not runs and branch:
            try:
                runs = _list_workflow_runs(repo, workflow, branch=branch, limit=15)
            except RuntimeError:
                pass
            if runs:
                short = commit[:7] if len(commit) >= 7 else commit
                runs = [r for r in runs if (
                    (r.get("headSha") or "").startswith(short)
                    or commit.startswith((r.get("headSha") or "")[:len(commit)])
                )]

        if not runs:
            empty_polls += 1
            if empty_polls >= probe_after_polls:
                dead_reason = _probe_workflow_lane_dead(repo, workflow)
                if dead_reason:
                    ref = commit or branch
                    raise RuntimeError(
                        f"Artifact wait failed fast: {dead_reason}, and no "
                        f"Blossom/Nostr artifact matched {repo}@{ref} after "
                        f"{empty_polls} polls. The GitHub lane will not produce "
                        f"an artifact for this branch within the timeout. If the "
                        f"build of record is ngit/Blossom (no kind-1063 event "
                        f"exists for this head), build the artifact locally or "
                        f"push the branch to the mirror before submitting."
                    )
                empty_polls = 0  # lane alive elsewhere — keep waiting for a push
            remaining = int(deadline - time.time())
            log.info(
                "No workflow runs yet for %s@%s (workflow=%r). Waiting... (%ds left)",
                repo, branch or commit, workflow, max(remaining, 0),
            )
            time.sleep(min(30, max(remaining, 1)))
            continue

        empty_polls = 0

        for run in runs:
            run_id = str(run.get("databaseId", ""))
            if not run_id:
                continue
            status = str(run.get("status", "")).lower()
            conclusion = str(run.get("conclusion") or "").lower()

            if status in ("queued", "in_progress", "pending", "waiting", "requested"):
                remaining = max(int(deadline - time.time()), 60)
                log.info("Run %s is %s — waiting up to %ds", run_id, status, remaining)
                _watch_run(repo, run_id, remaining)
                status = "completed"
                conclusion = ""
                view = subprocess.run(
                    ["gh", "run", "view", run_id, "--repo", repo, "--json", "conclusion,status"],
                    capture_output=True, text=True, timeout=30, check=False,
                )
                if view.returncode == 0:
                    try:
                        info = json.loads(view.stdout)
                        conclusion = str(info.get("conclusion") or "").lower()
                        status = str(info.get("status", "")).lower()
                    except json.JSONDecodeError:
                        pass

            if status == "completed" and conclusion not in ("", "success"):
                log.info("Run %s has conclusion=%s — checking for usable artifacts anyway", run_id, conclusion)
                if _run_has_arch_artifact(repo, run_id, arch):
                    log.info("Artifact ready: run %s has %s .ipk (despite overall failure)", run_id, arch)
                    return run_id
                continue

            if status == "completed" or conclusion == "success":
                if _run_has_arch_artifact(repo, run_id, arch):
                    log.info("Artifact ready: run %s has %s .ipk", run_id, arch)
                    return run_id
                log.info("Run %s succeeded but has no %s artifact yet", run_id, arch)

        remaining = int(deadline - time.time())
        if remaining <= 0:
            break
        log.info("No downloadable artifact yet — rechecking in 30s (%ds left)", remaining)
        time.sleep(min(30, remaining))

    ref = commit or branch
    raise RuntimeError(
        f"No downloadable {arch} CI artifact for {repo}@{ref} within {timeout_s}s. "
        f"Push to the branch and wait for workflow '{workflow}' to complete."
    )


def _blossom_relays() -> list[str]:
    return COORDINATION_RELAYS


def _tags_as_dict(tags: list) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for t in tags:
        if isinstance(t, list) and t:
            out.setdefault(t[0], []).extend(t[1:])
    return out


def _nak_req(args: list[str], timeout: int = 30) -> list[dict]:
    nak = shutil.which("nak")
    if not nak:
        return []
    cmd = [nak, "req"] + args + _blossom_relays()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return []
    events = []
    for line in (r.stdout or "").strip().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _event_matches(event: dict, *, arch: str, fmt: str, branch: str, commit: str | None,
                   content_tags: dict[str, list[str]]) -> tuple[dict | None, int]:
    """Return (artifact_dict_or_None, created_at) if the event matches filters.

    ``content_tags`` is the parsed tag dict for one event; the function reads
    architecture/format/compression/filename/version/url/sha256 from it in a
    way that works for BOTH kind 1063 (NIP-94 tags) and kind 30078 (JSON
    ``content`` blob, pre-flattened into a tag-like dict by the caller).
    """
    archs = content_tags.get("A") or content_tags.get("architecture") or []
    if archs and arch not in archs and (len(archs) == 1 and archs[0] != arch):
        # 30078 content uses "architecture" (scalar); 1063 uses "A" (tag list).
        pass
    arch_val = (archs[0] if archs else content_tags.get("architecture", ""))
    if arch_val != arch:
        return None, 0
    comp = (content_tags.get("compression") or ["none"])[0]
    if comp != "none":
        return None, 0
    if fmt:
        fmt_val = (content_tags.get("format") or ["ipk"])[0]
        if fmt_val != fmt:
            return None, 0

    filename = (content_tags.get("filename") or [""])[0]
    if branch:
        sanitized = branch.replace("/", "-")
        if f"_{sanitized}." not in filename:
            return None, 0

    artifact = {
        "url": (content_tags.get("url") or content_tags.get("urls") or [""])[0],
        "filename": filename,
        "sha256": (content_tags.get("x") or content_tags.get("ox") or content_tags.get("sha256") or [""])[0],
        "architecture": arch,
        "format": (content_tags.get("format") or ["ipk"])[0],
        "compression": comp,
    }
    ts = int(event.get("created_at", 0) or 0)

    if commit:
        build_id = ""
        rtags = content_tags.get("r") or []
        if rtags:
            build_id = rtags[0]
        version = (content_tags.get("v") or [""])[0]
        short = commit[:7] if len(commit) >= 7 else commit
        if build_id.startswith(short) or (version and version.endswith("." + short)):
            return artifact, ts
        return None, 0  # commit pinned but no match → do not accept

    return artifact, ts


def _resolve_blossom_binary(commit: str | None, arch: str, fmt: str = "", branch: str = "") -> dict | None:
    """Resolve a deployable artifact from Blossom via Nostr.

    Primary source: kind **1063** NIP-94 file-metadata events — these are
    persistent (one per published package) and are the canonical consumer
    artifact index. Fallback: kind **30078** build-coordination events, which
    are transient (deleted with kind 5 once the 1063 publishes) and only useful
    for catching in-flight builds that have not yet published metadata.

    Args:
        commit: if set, require an exact short-SHA match (no fallback to a
            different commit). If unset, return the newest matching build.
        fmt: filter by 'ipk' or 'apk'.
        branch: if set, require the filename to contain ``_{branch}.`` so a
            feature-branch artifact is never used to test a different branch.

    Returns dict with url/filename/sha256, or None.
    """
    best_commit_match = None
    best_commit_ts = 0
    best_any = None
    best_any_ts = 0

    # Kind 1063 — persistent file metadata (authoritative).
    # No -a (author) filter: the CI bot key was rotated, and we rely on the
    # n=tollgate-wrt tag to identify TollGate packages. SHA256 content
    # addressing protects against forged events.
    for e in _nak_req(["-k", "1063",
                       "-t", "n=tollgate-wrt", "-l", "120"]):
        tags = _tags_as_dict(e.get("tags", []))
        art, ts = _event_matches(e, arch=arch, fmt=fmt, branch=branch,
                                 commit=commit, content_tags=tags)
        if not art:
            continue
        if commit:
            if ts > best_commit_ts:
                best_commit_ts, best_commit_match = ts, art
        elif ts > best_any_ts:
            best_any_ts, best_any = ts, art

    # Kind 30078 — transient build coordination (in-flight builds only).
    for e in _nak_req(["-k", "30078", "-t", "t=tollgate-build", "-l", "40"]):
        try:
            content = json.loads(e.get("content", "{}"))
        except json.JSONDecodeError:
            continue
        # Flatten the 30078 JSON content into a tag-like dict so _event_matches
        # handles both kinds uniformly.
        flat: dict[str, list[str]] = {}
        for k, v in content.items():
            if isinstance(v, list):
                flat[k] = v
            elif v != "":
                flat[k] = [v]
        # 30078 uses 'r' tag at the event level for the build id.
        rtag = [t[1] for t in e.get("tags", []) if isinstance(t, list) and t and t[0] == "r"]
        if rtag:
            flat["r"] = rtag
        art, ts = _event_matches(e, arch=arch, fmt=fmt, branch=branch,
                                 commit=commit, content_tags=flat)
        if not art:
            continue
        if commit:
            if ts > best_commit_ts:
                best_commit_ts, best_commit_match = ts, art
        elif ts > best_any_ts:
            best_any_ts, best_any = ts, art

    if best_commit_match:
        return best_commit_match
    if best_any:
        log.info("No exact commit match on Blossom — using newest %s build", arch)
        return best_any
    return None


def _download_blossom_binary(url: str, build_dir: Path, sha256: str = "", save_as: str = "") -> Path | None:
    """Download .ipk from Blossom. Tries primary URL, then falls back to
    mirror servers using the content-addressed SHA256 when the primary 404s."""
    filename = save_as or url.rsplit("/", 1)[-1]
    dest = build_dir / filename

    urls_to_try = [url]
    if sha256 and len(sha256) == 64:
        for server in BLOSSOM_MIRROR_SERVERS:
            alt = f"{server}/{sha256}"
            if alt not in urls_to_try:
                urls_to_try.append(alt)

    for try_url in urls_to_try:
        try:
            cache_bust = f"?cb={int(time.time())}"
            subprocess.run(
                ["curl", "-sL", "-o", str(dest), f"{try_url}{cache_bust}"],
                timeout=120, check=True, capture_output=True,
            )
            if dest.exists() and dest.stat().st_size > 1000:
                log.info("Downloaded from Blossom: %s (%d bytes) via %s",
                         filename, dest.stat().st_size, try_url)
                return dest
        except Exception:
            continue

    log.warning("Blossom download failed from all %d server(s): %s",
                len(urls_to_try), ", ".join(urls_to_try))
    return None


def download_artifact(branch: str, arch: str, run_id: str | None = None,
                      repo: str | None = None, workflow: str | None = None,
                      output_name: str | None = None, fmt: str = "") -> Path:
    artifact_repo = repo or REPO
    artifact_workflow = workflow or WORKFLOW
    if BUILD_DIR.exists():
        try:
            shutil.rmtree(BUILD_DIR)
        except (PermissionError, OSError):
            subprocess.run(["sudo", "rm", "-rf", str(BUILD_DIR)], timeout=15, capture_output=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    target_commit = os.environ.get("TOLLGATE_SUT_COMMIT", "")
    blossom_binary = _resolve_blossom_binary(target_commit or None, arch, fmt=fmt, branch=branch)
    if blossom_binary:
        log.info("Found Blossom binary: %s", blossom_binary.get("filename", "?"))
        _sha = blossom_binary.get("sha256", "")
        _url = blossom_binary.get("url") or (blossom_binary.get("urls") or [None])[0]
        if not _url and _sha and len(_sha) == 64:
            _url = f"{BLOSSOM_MIRROR_SERVERS[0]}/{_sha}"
            log.info("No url in event — constructed from sha256: %s", _url)
        if _url:
            blossom_path = _download_blossom_binary(
                _url, BUILD_DIR,
                sha256=_sha,
                save_as=blossom_binary.get("filename", ""),
            )
        if blossom_path:
            return blossom_path
        log.warning("Blossom download failed, falling back to GitHub Actions")
    else:
        log.info("No Blossom binary found for arch=%s, using GitHub Actions", arch)

    if not run_id:
        log.info("Finding latest build for branch '%s'", branch)
        for status_filter in ("success", "completed"):
            r = subprocess.run(
                [
                    "gh", "run", "list",
                    "--repo", artifact_repo,
                    "--branch", branch,
                    "--status", status_filter,
                    "--workflow", artifact_workflow,
                    "--limit", "1",
                    "--json", "databaseId,status",
                    "--jq", ".[0].databaseId",
                ],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0 and r.stdout.strip():
                run_id = r.stdout.strip()
                break
        if not run_id:
            if repo and repo != REPO:
                log.info("No build found on fork '%s', trying upstream '%s'", repo, REPO)
                return download_artifact(branch, arch, run_id=run_id, repo=None, workflow=workflow)
            raise RuntimeError(f"No builds found for branch '{branch}' on {artifact_repo}")
        log.info("Found run: %s", run_id)

    def _download(run: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["gh", "run", "download", run, "--repo", artifact_repo, "--dir", str(BUILD_DIR)],
            capture_output=True, text=True, timeout=300,
        )

    log.info("Downloading artifacts from run %s", run_id)
    r = _download(run_id)
    if r.returncode != 0:
        err = r.stderr.strip() or r.stdout.strip() or "unknown gh error"
        if "no valid artifacts" in err.lower() and not os.environ.get("TOLLGATE_DISABLE_ARTIFACT_RERUN"):
            log.warning("No valid artifacts for run %s; trying to rerun x86_64/arch-specific build job", run_id)
            rerun = _rerun_arch_job(artifact_repo, run_id, arch)
            if rerun:
                if BUILD_DIR.exists():
                    shutil.rmtree(BUILD_DIR)
                BUILD_DIR.mkdir(parents=True, exist_ok=True)
                r = _download(run_id)
                if r.returncode == 0:
                    log.info("Artifact download succeeded after rerun")
                    err = ""
            if r.returncode == 0:
                pass
            else:
                err = r.stderr.strip() or r.stdout.strip() or err
        if r.returncode == 0:
            pass
        else:
            hint = (
                f"Could not download CI artifacts for {artifact_repo}@{branch} "
                f"(workflow={artifact_workflow!r}, run={run_id}, required_arch={arch!r})."
            )
            if "no valid artifacts" in err.lower():
                hint += (
                    " GitHub reports no valid downloadable artifacts; this usually means "
                    "the run artifacts expired, were deleted, or the release/tag did not upload them. "
                    "For the GCP virtual lab, provide a fresh x86_64 .ipk via a new CI run/release "
                    "or use a branch with current x86_64 artifacts."
                )
            raise RuntimeError(f"{hint} gh run download failed: {err}")

    matches = [p for p in BUILD_DIR.rglob(f"*{arch}*.ipk") if p.is_file() and "upx" not in p.name]
    if not matches:
        available = [str(p.relative_to(BUILD_DIR)) for p in BUILD_DIR.rglob("*.ipk") if p.is_file()]
        raise RuntimeError(f"No .ipk found for arch '{arch}'. Available: {available or 'none'}")

    src = matches[0]
    flat_name = output_name or f"tollgate-wrt-{arch}.ipk"
    flat = BUILD_DIR / flat_name
    if src.resolve() != flat.resolve():
        shutil.copy2(src, flat)

    log.info("Artifact: %s (%.1f MB)", flat.name, flat.stat().st_size / (1024 * 1024))
    return flat


def _rerun_arch_job(repo: str, run_id: str, arch: str) -> bool:
    needle = "x86_64" if arch == "x86_64" else arch
    r = subprocess.run(
        [
            "gh", "run", "view", run_id,
            "--repo", repo,
            "--json", "jobs",
        ],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        log.warning("Could not inspect run jobs for %s: %s", run_id, r.stderr.strip())
        return False
    try:
        jobs = json.loads(r.stdout).get("jobs", [])
    except json.JSONDecodeError as exc:
        log.warning("Could not parse run jobs for %s: %s", run_id, exc)
        return False

    candidates = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = str(job.get("name", ""))
        database_id = job.get("databaseId")
        if needle in name and database_id:
            candidates.append((name, str(database_id)))
    if not candidates:
        log.warning("No rerunnable job matched arch '%s' in run %s", arch, run_id)
        return False

    name, job_id = candidates[0]
    log.info("Rerunning job %s (%s)", job_id, name)
    rerun = subprocess.run(
        ["gh", "run", "rerun", "--repo", repo, "--job", job_id],
        capture_output=True, text=True, timeout=30,
    )
    if rerun.returncode != 0:
        log.warning("Could not rerun job %s: %s", job_id, rerun.stderr.strip())
        return False
    watch = subprocess.run(
        ["gh", "run", "watch", run_id, "--repo", repo, "--exit-status", "--interval", "15"],
        capture_output=True, text=True, timeout=1800,
    )
    if watch.returncode != 0:
        log.warning("Rerun did not complete successfully for run %s: %s", run_id, watch.stderr.strip())
        return False
    return True


def deploy(router, ipk_path: Path, reboot: bool = False, backend=None) -> dict[str, object]:
    ipk_path = Path(ipk_path)
    if not ipk_path.exists():
        raise FileNotFoundError(f"IPK not found: {ipk_path}")

    log.info("Installing test dependencies on router")
    install_test_deps(router)

    log.info("Copying %s to router", ipk_path.name)
    remote_name = "tollgate-wrt.ipk" if ipk_path.suffix == ".ipk" else "tollgate-wrt.apk"
    _scp_to_router(router, ipk_path, f"/tmp/{remote_name}")

    log.info("Installing tollgate-wrt")
    pm = detect_package_manager(router)
    if pm == "apk":
        router.ssh(
            "/etc/init.d/tollgate-wrt stop 2>/dev/null;"
            "killall tollgate-wrt 2>/dev/null;"
            "sleep 1;"
            f"apk add --allow-untrusted /tmp/{remote_name}"
            " && /etc/init.d/tollgate-wrt restart"
            " && /etc/init.d/tollgate-basic restart 2>/dev/null"
            "; /etc/init.d/uhttpd restart 2>/dev/null"
            "; rm -f /tmp/tollgate-wrt.ipk",
            timeout=120,
        )
        version_out = router.ssh("apk info -e tollgate-wrt 2>/dev/null || opkg list-installed | grep tollgate-wrt", timeout=10)
    else:
        router.ssh(
            "/etc/init.d/tollgate-wrt stop 2>/dev/null;"
            "killall tollgate-wrt 2>/dev/null;"
            "sleep 1;"
            "opkg install /tmp/tollgate-wrt.ipk"
            " && /etc/init.d/tollgate-wrt restart"
            " && /etc/init.d/tollgate-basic restart 2>/dev/null"
            "; /etc/init.d/uhttpd restart 2>/dev/null"
            "; rm -f /tmp/tollgate-wrt.ipk",
            timeout=120,
        )
        version_out = router.ssh("opkg list-installed | grep tollgate-wrt", timeout=10)

    if reboot:
        return reboot_router(router)

    router.ssh(
        "echo 1 > /proc/sys/net/ipv4/conf/all/route_localnet 2>/dev/null;"
        "iptables -C ndsRTR -p tcp --dport 2121 -j ACCEPT 2>/dev/null"
        " || iptables -I ndsRTR 12 -p tcp --dport 2121 -j ACCEPT 2>/dev/null;"
        "iptables -C ndsRTR -p tcp --dport 80 -j ACCEPT 2>/dev/null"
        " || iptables -I ndsRTR 12 -p tcp --dport 80 -j ACCEPT 2>/dev/null;"
        "nft insert rule inet fw4 input tcp dport 2121 accept 2>/dev/null;"
        "true",
        timeout=10,
    )

    health_timeout = 120 if backend and backend.is_rust_family else 60
    log.info("Waiting for backend health on port 2121 (timeout=%ds)", health_timeout)
    healthy = _wait_for_health(router, timeout=health_timeout)
    installed_version = _parse_version(version_out)
    health_code = 200 if healthy else router.api_status("/")

    if backend and backend.is_rust_family and health_code == 200:
        _write_rust_compat_config(router)

    return {
        "installed_version": installed_version,
        "health_code": health_code,
        "success": health_code == 200,
    }


def reboot_router(router, wait: bool = True) -> dict[str, object]:
    log.info("Rebooting router")
    try:
        router.ssh("reboot", timeout=5)
    except (subprocess.TimeoutExpired, Exception):
        pass

    if wait:
        _wait_for_reboot(router)
        install_test_deps(router)
        log.info("Waiting for backend health after reboot")
        healthy = _wait_for_health(router, timeout=120)
    else:
        healthy = False

    version_out = ""
    if healthy:
        try:
            version_out = router.ssh("opkg list-installed | grep tollgate-wrt", timeout=10)
        except Exception:
            pass

    return {
        "installed_version": _parse_version(version_out) if version_out else None,
        "health_code": 200 if healthy else 0,
        "success": healthy,
        "rebooted": True,
    }


def check_deployed(router) -> dict[str, object]:
    try:
        version_out = router.ssh("opkg list-installed | grep tollgate-wrt", timeout=10)
    except Exception:
        version_out = ""
    version = _parse_version(version_out) if version_out else None

    health_code = router.api_status("/")

    try:
        ps_out = router.ssh("ps | grep tollgate-wrt | grep -v grep", timeout=10)
        running = bool(ps_out.strip())
    except Exception:
        running = False

    return {
        "version": version,
        "healthy": health_code == 200,
        "running": running,
        "health_code": health_code,
    }


def factory_reset(router, reboot: bool = False, expected_mac: str | None = None) -> dict[str, object]:
    guard_mac = expected_mac or os.environ.get("TOLLGATE_EXPECTED_MAC", "")
    if guard_mac:
        log.info("Verifying router MAC address before factory reset")
        try:
            mac_out = router.ssh("cat /sys/class/net/br-lan/address 2>/dev/null || cat /sys/class/net/eth0/address 2>/dev/null", timeout=5)
            actual_mac = mac_out.strip().lower()
            expected = guard_mac.lower()
            if actual_mac != expected:
                raise RuntimeError(
                    f"MAC MISMATCH — aborting factory reset! "
                    f"Expected {expected}, got {actual_mac}. "
                    f"Wrong router?"
                )
            log.info("MAC verified: %s", actual_mac)
        except RuntimeError:
            raise
        except Exception as e:
            log.warning("Could not verify MAC (%s) — proceeding anyway", e)

    log.info("Removing tollgate-wrt package")
    router.ssh("opkg remove tollgate-wrt 2>/dev/null", timeout=30)

    log.info("Cleaning config, firewall rules, uci-defaults")
    router.ssh(
        "rm -rf /etc/tollgate"
        " /etc/config/firewall-tollgate"
        " /etc/nodogsplash/htdocs"
        " /tmp/tollgate-debug.log"
        " /tmp/tollgate-portal.log",
        timeout=10,
    )
    router.ssh("rm -f /etc/uci-defaults/90-tollgate-captive-portal-symlink"
               " /etc/uci-defaults/95-tollgate*"
               " /etc/uci-defaults/98-tollgate*"
               " /etc/uci-defaults/99-tollgate*"
               " /etc/uci-defaults/99a-tollgate*"
               " /etc/uci-defaults/99b-tollgate*", timeout=10)

    log.info("Restoring uhttpd to port 80")
    router.ssh(
        "uci get uhttpd.main.listen_http | grep -q 8080"
        " && uci delete uhttpd.main.listen_http"
        " && uci add_list uhttpd.main.listen_http='0.0.0.0:80'"
        " && uci add_list uhttpd.main.listen_http='[::]:80'"
        " && uci commit uhttpd"
        " || true",
        timeout=15,
    )

    log.info("Disabling nodogsplash")
    router.ssh("/etc/init.d/nodogsplash stop 2>/dev/null; /etc/init.d/nodogsplash disable 2>/dev/null", timeout=10)

    router.ssh("fw4 restart 2>/dev/null", timeout=15)
    router.ssh("/etc/init.d/uhttpd restart 2>/dev/null", timeout=15)

    if reboot:
        return reboot_router(router)

    return {"success": True, "rebooted": False}


def firstboot_reset(router, expected_mac: str | None = None) -> dict[str, object]:
    guard_mac = expected_mac or os.environ.get("TOLLGATE_EXPECTED_MAC", "")
    if guard_mac:
        log.info("Verifying router MAC address before firstboot reset")
        try:
            mac_out = router.ssh("cat /sys/class/net/br-lan/address 2>/dev/null || cat /sys/class/net/eth0/address 2>/dev/null", timeout=5)
            actual_mac = mac_out.strip().lower()
            expected = guard_mac.lower()
            if actual_mac != expected:
                raise RuntimeError(
                    f"MAC MISMATCH — aborting firstboot reset! "
                    f"Expected {expected}, got {actual_mac}. "
                    f"Wrong router?"
                )
            log.info("MAC verified: %s", actual_mac)
        except RuntimeError:
            raise
        except Exception as e:
            log.warning("Could not verify MAC (%s) — proceeding anyway", e)

    log.info("Running firstboot -y && reboot")
    try:
        router.ssh("firstboot -y && reboot", timeout=10)
    except (subprocess.TimeoutExpired, Exception):
        pass

    if not _wait_for_reboot(router):
        return {"success": False, "rebooted": True, "error": "Router did not come back after firstboot"}

    install_test_deps(router)

    return {"success": True, "rebooted": True}


def deploy_portal(router, portal, arch: str | None = None, branch: str = "main") -> dict[str, object]:
    """Download and install an alternative portal .ipk on the router.

    Only runs when ``portal.needs_separate_deploy`` is True (i.e. not
    the built-in portal).  The portal package must PROVIDE
    ``tollgate-captive-portal-site`` and CONFLICT with the built-in
    portal so that ``opkg`` handles the symlink swap automatically.
    """
    from lib.portal import PortalConfig

    assert isinstance(portal, PortalConfig)
    if not portal.needs_separate_deploy:
        return {"skipped": True, "reason": "builtin portal"}

    if not arch:
        env_arch = os.environ.get("TOLLGATE_ROUTER_ARCH")
        if env_arch:
            arch = env_arch
        else:
            arch = detect_arch(router)
            log.info("Auto-detected router arch: %s", arch)

    assert portal.repo is not None
    assert portal.workflow is not None

    log.info("Downloading portal artifact from %s@%s", portal.repo, branch)
    run_id = ensure_artifact(
        branch=branch,
        arch=arch,
        repo=portal.repo,
        workflow=portal.workflow,
    )
    ipk_path = download_artifact(
        branch, arch,
        run_id=run_id,
        repo=portal.repo,
        workflow=portal.workflow,
        output_name=f"portal-{portal.type}-{arch}.ipk",
    )

    _repack_ar_to_targz(ipk_path)

    log.info("Installing portal package %s from %s", portal.package_name, ipk_path.name)
    _transfer_to_router(router, ipk_path, "/tmp/portal.ipk")

    install_out = router.ssh(
        "opkg install --force-overwrite --force-depends /tmp/portal.ipk 2>&1"
        "; rm -f /tmp/portal.ipk",
        timeout=120,
    )
    log.info("opkg install output: %s", (install_out or "").strip()[:500])

    pkg_name = portal.package_name or portal.type
    check = router.ssh(f"opkg list-installed | grep -w {pkg_name}", timeout=10)
    installed = bool(check.strip())
    if installed:
        log.info("Portal %s installed successfully", portal.type)
        router.ssh("/etc/init.d/nodogsplash restart 2>/dev/null || true", timeout=30)
    else:
        log.error(
            "Portal %s failed to install. opkg output: %s",
            portal.type, (install_out or "").strip()[:500],
        )

    return {
        "success": installed,
        "portal_type": portal.type,
        "package": pkg_name,
    }


def deploy_branch(router, branch: str, arch: str | None = None,
                  run_id: str | None = None, force: bool = False,
                  reboot: bool = False, repo: str | None = None,
                  backend=None) -> dict[str, object]:
    if not arch:
        env_arch = os.environ.get("TOLLGATE_ROUTER_ARCH")
        if env_arch:
            arch = env_arch
        else:
            arch = detect_arch(router)
            log.info("Auto-detected router arch: %s", arch)

    if not force:
        status = check_deployed(router)
        if status["healthy"] and status["running"] and status["version"]:
            log.info("Already deployed: version=%s — skipping", status["version"])
            return {
                "installed_version": status["version"],
                "health_code": 200,
                "success": True,
                "skipped": True,
            }

    artifact_repo = repo or (backend.repo if backend else None)
    artifact_workflow = backend.workflow if backend else None
    pm = detect_package_manager(router)
    fmt = "apk" if pm == "apk" else "ipk"
    log.info("Router package manager: %s — requesting %s artifact", pm, fmt)
    ipk_path = download_artifact(branch, arch, run_id=run_id,
                                 repo=artifact_repo, workflow=artifact_workflow, fmt=fmt)
    return deploy(router, ipk_path, reboot=reboot, backend=backend)
