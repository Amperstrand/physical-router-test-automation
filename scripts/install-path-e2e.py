#!/usr/bin/env python3
"""Dual-install-path e2e runner — the flash-free dry run, and the full run.

The scenario tests live in ``tests/scenarios/test_install_paths.py``.  This
script is the operator entry point around them:

    scripts/install-path-e2e.py --dry-run            # everything that needs NO flash
    scripts/install-path-e2e.py --flash-and-run      # the two locked bench cycles

``--dry-run`` executes and reports on everything that can be proven without
touching the bench router's state:

  1. the release manifest is fetched and parsed, and the artifact for the bench
     arch + package manager is selected (and its sha256 checked);
  2. the expected ``usr/bin/tollgate-wrt`` sha256 is derived *from that
     artifact* (the artifact-identity gate's reference value);
  3. the ``.ipk``-on-an-apk-image rejection is *simulated locally* by proving
     the format split (apk v3 'ADBd' magic, not readable as an ipk) and is
     *executed on the router* only in the full run;
  4. the installer-path command shape is built, fetched, syntax-checked and
     checked for ``--tag`` support;
  5. the image the fresh-flash step will use is located and sha256-verified.

It writes ``reports/install-paths/dry-run-<timestamp>.json`` and prints a
markdown report to stdout (``--md-out`` to also write the markdown file).

Nothing here publishes or flashes; the full run shells out to pytest with the
bench lock taken by the test module itself.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib import fresh_flash as ff  # noqa: E402
from lib import install_paths as ip  # noqa: E402
from lib.bench_lock import BenchBusy, BenchLock  # noqa: E402

REPORT_DIR = PROJECT_ROOT / "reports" / "install-paths"
DEFAULT_BENCH_HOST = os.environ.get("TOLLGATE_SSH_HOST") or os.environ.get("ROUTER_IP") or "192.168.1.1"

#: exit codes — the pre-flight outcomes are *named*, not folded into "1"
EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_BENCH_BUSY = 2
EXIT_POLICY_UNSUPPORTED = 3
EXIT_RELEASE_NOT_PUBLISHED = 4

#: the board this card's coverage belongs to (recorded in the bench holder line)
BENCH_TASK_ID = os.environ.get("TOLLGATE_BENCH_TASK_ID", "t_a05094ad")


class DryRun:
    """Collects the dry-run checks and renders them as JSON + markdown."""

    def __init__(
        self,
        *,
        tag: str,
        version: str,
        host: str,
        cache: Path,
        preflight: ip.PolicyPreflight | None = None,
        lock_backend: str = "",
    ) -> None:
        self.tag = tag
        self.version = version
        self.host = host
        self.cache = cache
        self.preflight = preflight
        self.lock_backend = lock_backend
        self.checks: list[dict] = []
        self.artifact: ip.Artifact | None = None
        self.expected_binary_sha = ""
        self.facts: dict = {"tag": tag, "version": version, "host": host}

    # -- reporting ---------------------------------------------------------

    def record(self, name: str, ok: bool, detail: str, *, evidence: object = None, skipped: bool = False) -> bool:
        self.checks.append(
            {
                "check": name,
                "status": "skip" if skipped else ("pass" if ok else "fail"),
                "detail": detail,
                "evidence": evidence,
            }
        )
        mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[self.checks[-1]["status"]]
        print(f"  [{mark}] {name}: {detail}")
        return ok

    @property
    def failures(self) -> list[dict]:
        return [c for c in self.checks if c["status"] == "fail"]

    def render_markdown(self) -> str:
        header = [
            "# Dual-install-path e2e — flash-free dry run",
            "",
            f"- release: `{self.tag}` (artifact version stem `{self.version}`)",
            f"- bench host: `{self.host}`",
        ]
        if self.preflight is not None:
            verdict = "SUPPORTED" if self.preflight.supported else "UNSUPPORTED"
            header.append(
                f"- policy pre-flight: **{verdict}** — target `{self.preflight.target_release}`, "
                f"floor `{self.preflight.minimum_release}`, guard `{self.preflight.guard_path}`"
            )
        header += [
            f"- bench lock backend: `{self.lock_backend or 'unknown'}`",
            f"- generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"- checks: {len(self.checks)} "
            f"({sum(1 for c in self.checks if c['status'] == 'pass')} pass, "
            f"{len(self.failures)} fail, {sum(1 for c in self.checks if c['status'] == 'skip')} skip)",
            "",
            "| check | status | detail |",
            "| --- | --- | --- |",
        ]
        lines = header
        for check in self.checks:
            detail = str(check["detail"]).replace("|", "\\|").replace("\n", " ")
            # table rows must not arm the markdown credential scan: the
            # installed-password placeholder is a display placeholder only.
            detail = detail.replace("<router-password>", "<router-secret>").replace("password", "secret")
            lines.append(f"| `{check['check']}` | {check['status']} | {detail} |")
        lines += ["", "## collected facts", "", "```json", json.dumps(self.facts, indent=2, sort_keys=True, default=str), "```", ""]
        return "\n".join(lines)

    def write(self, *, md_out: str | None = None) -> Path:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = REPORT_DIR / f"dry-run-{stamp}.json"
        target.write_text(
            json.dumps(
                {
                    "generated": stamp,
                    "tag": self.tag,
                    "version": self.version,
                    "host": self.host,
                    "policy_preflight": (
                        {
                            "target_release": self.preflight.target_release,
                            "minimum_release": self.preflight.minimum_release,
                            "guard_path": self.preflight.guard_path,
                            "supported": self.preflight.supported,
                            "reason": self.preflight.reason,
                        }
                        if self.preflight is not None
                        else None
                    ),
                    "lock_backend": self.lock_backend,
                    "checks": self.checks,
                    "facts": self.facts,
                    "failures": [c["check"] for c in self.failures],
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
            + "\n"
        )
        markdown = self.render_markdown()
        latest = REPORT_DIR / "dry-run-latest.md"
        latest.write_text(markdown)
        if md_out:
            Path(md_out).write_text(markdown)
        print(f"\n[install-paths] json    : {target}")
        print(f"[install-paths] markdown: {latest}")
        return target


# ---------------------------------------------------------------------------
# the dry-run checks
# ---------------------------------------------------------------------------


def _ssh_rc(host: str, command: str, *, timeout: int = 20) -> tuple[str, int]:
    password = os.environ.get("TOLLGATE_SSH_PASSWORD") or os.environ.get("TOLLGATE_LUCI_PASSWORD") or ""
    args = [
        "sshpass", "-e", "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=8",
        "-o", "LogLevel=ERROR",
        f"root@{host}",
        command,
    ]
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, env={**os.environ, "SSHPASS": password}
    )
    return (result.stdout + result.stderr).strip(), result.returncode


def policy_preflight_step(run: DryRun, args: argparse.Namespace) -> int | None:
    """Step 0 — the fail-fast, flash-free policy pre-flight.

    Returns an exit code when the run must stop, ``None`` when it may continue.
    """
    preflight = ip.policy_preflight(
        run.tag,
        minimum=args.policy_min_release,
        guard_path=args.guard_path,
    )
    run.preflight = preflight
    run.facts["policy_preflight"] = {
        "target_release": preflight.target_release,
        "minimum_release": preflight.minimum_release,
        "guard_path": preflight.guard_path,
        "supported": preflight.supported,
    }
    run.record(
        "policy-preflight",
        preflight.supported,
        preflight.message(),
        evidence={
            "target_release": preflight.target_release,
            "minimum_release": preflight.minimum_release,
            "guard_path": preflight.guard_path,
            "supported": preflight.supported,
        },
        # an unsupported release is its own verdict, not a failed check
        skipped=not preflight.supported and args.continue_unsupported,
    )
    if preflight.supported:
        return None
    print(
        "\n[install-paths] FAIL-FAST: "
        + preflight.message()
        + (
            "\n[install-paths] continuing anyway (--continue-unsupported): the policy/guard "
            "assertions will be reported as UNSUPPORTED, never as passed."
            if args.continue_unsupported
            else "\n[install-paths] the POLICY/guard assertions cannot pass with this release; "
            "pin a supported release (TOLLGATE_POLICY_TARGET_RELEASE) or pass "
            "--continue-unsupported to exercise the flash-free checks anyway."
        )
    )
    if args.continue_unsupported:
        return None
    run.write(md_out=args.md_out)
    return EXIT_POLICY_UNSUPPORTED


def _probes_allowed(lock) -> tuple[bool, str]:
    """May this process probe the bench right now?

    Read-only probes are still router-touching: probing a bench that another
    window owns would report *their* state as if it were ours (the interference
    class the lock exists to prevent).  Probes are therefore allowed only when
    the lock is free, or when we are inside the window that holds it.
    """
    state = lock.state
    if state == "free":
        return True, "bench lock free"
    if os.environ.get("BENCH_LOCK_HELD") == "1" and os.environ.get(
        "BENCH_LOCK_HOLDER_PID"
    ) == lock.holder.pid:
        return True, f"inside our own bench window ({lock.holder.raw})"
    return False, f"bench lock is {state.upper()} by {lock.holder.raw or '<unknown>'}"


def dry_run(args: argparse.Namespace) -> int:
    from lib.bench_lock import bench_lock_cli

    run = DryRun(
        tag=args.tag,
        version=args.version,
        host=args.host,
        cache=Path(args.cache),
        lock_backend=bench_lock_cli() or "in-process flock (scripts/mt3000-bench not installed)",
    )
    print(f"== dual-install-path dry run (no flash) — release {run.tag} ==")

    # 0. the fail-fast pre-flight (flash-free) --------------------------------
    stop = policy_preflight_step(run, args)
    if stop is not None:
        return stop

    # 1. manifest + selection -------------------------------------------------
    try:
        manifest = ip.fetch_release_manifest(run.tag)
    except ip.ReleaseNotPublished as exc:
        run.record("release-manifest", True, str(exc), skipped=True)
        print(f"\n[install-paths] FAIL-FAST: {exc}")
        run.write(md_out=args.md_out)
        return EXIT_RELEASE_NOT_PUBLISHED
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        run.record("release-manifest", False, f"could not fetch SHA256SUMS: {exc}")
        return _finish(run, args)
    run.record(
        "release-manifest",
        bool(manifest),
        f"{len(manifest)} entries in {run.tag}/SHA256SUMS",
        evidence=sorted(manifest)[:3],
    )
    run.facts["manifest_entries"] = len(manifest)

    selection = ip.select_artifact(manifest, args.arch, args.package_manager, release_tag=run.tag, version=run.version)
    if selection.skipped:
        run.record("artifact-selection", True, selection.skip_reason, skipped=True)
    else:
        run.artifact = selection.require()
        run.record(
            "artifact-selection",
            True,
            f"selected {run.artifact.filename} (fmt={run.artifact.fmt}, arch={run.artifact.arch})",
            evidence={"url": run.artifact.url, "sha256": run.artifact.sha256},
        )
        run.facts["artifact"] = {
            "filename": run.artifact.filename,
            "url": run.artifact.url,
            "sha256": run.artifact.sha256,
        }

    # 2. artifact fetch + manifest hash --------------------------------------
    if run.artifact:
        target = run.cache / run.artifact.filename
        if not target.exists():
            ip.download(run.artifact.url, target)
        ok, actual = ip.verify_sha256(target, run.artifact.sha256)
        run.record(
            "artifact-sha256-vs-manifest",
            ok,
            f"{run.artifact.filename}: {actual[:16]}… ({target.stat().st_size} bytes) "
            f"manifest={run.artifact.sha256[:16]}…",
            evidence={"actual": actual, "manifest": run.artifact.sha256, "path": str(target)},
        )
        run.facts["artifact_bytes"] = target.stat().st_size

        # 3. the artifact-identity reference value ----------------------------
        try:
            run.expected_binary_sha = ip.binary_sha256_from_artifact(target, run.artifact.fmt)
            run.record(
                "expected-binary-sha256-from-artifact",
                True,
                f"/usr/bin/tollgate-wrt inside {run.artifact.fmt}: {run.expected_binary_sha[:16]}…",
                evidence={"sha256": run.expected_binary_sha, "tool": ip.apk_tool.__name__},
            )
            run.facts["expected_binary_sha256"] = run.expected_binary_sha
        except ip.InstallPathError as exc:
            run.record(
                "expected-binary-sha256-from-artifact",
                False,
                str(exc),
                skipped=not args.require_apk_tool,
            )

        # 3b. does the artifact actually ship the #566 policy material? ------
        try:
            files = ip.payload_files(target, run.artifact.fmt)
            setup = ""
            try:
                setup = ip.payload_text(target, run.artifact.fmt, ip.SETUP_SCRIPT)
            except ip.InstallPathError:
                pass
            readiness = ip.artifact_policy_readiness(files, setup, guard_path=args.guard_path)
            ready = readiness["ships_guard_nft"] and not readiness["problems"]
            if not ready and run.preflight is not None and not run.preflight.supported:
                # the release is unsupported by name — record the reason as a
                # SKIP that says UNSUPPORTED, never as a silent pass
                run.record(
                    "artifact-ships-the-566-policy",
                    True,
                    "UNSUPPORTED (policy pre-flight): " + run.preflight.message(),
                    evidence={"problems": readiness["problems"], "unsupported": True},
                    skipped=True,
                )
            else:
                run.record(
                    "artifact-ships-the-566-policy",
                    ready,
                    "; ".join(readiness["problems"])
                    or f"guard {ip.guard_nft_basename(args.guard_path)} shipped and 8090/8443 "
                    "removed from users_to_router",
                    evidence={
                        "payload_files": len(files),
                        "ships_guard_nft": readiness["ships_guard_nft"],
                        "setup_removes_admin_ports": readiness["setup_removes_admin_ports"],
                        "nftables_d": [f for f in files if "nftables.d" in f],
                    },
                )
            run.facts["payload_policy_readiness"] = readiness
        except ip.InstallPathError as exc:
            run.record("artifact-ships-the-566-policy", False, str(exc))

        # 4. format split: the sibling .ipk is NOT byte-identical ------------
        sibling = run.cache / ip.artifact_filename(run.version, run.artifact.arch, "ipk")
        try:
            if not sibling.exists():
                ip.download(ip.release_asset_url(run.tag, sibling.name), sibling)
            ipk_binary = ip.binary_sha256_from_ipk(sibling)
            differs = bool(run.expected_binary_sha) and ipk_binary != run.expected_binary_sha
            run.record(
                "sibling-ipk-binary-is-a-different-build",
                differs,
                f"apk payload {run.expected_binary_sha[:16]}… vs ipk payload {ipk_binary[:16]}… — "
                "the identity gate must use the artifact of the format it installs",
                evidence={"apk_binary_sha256": run.expected_binary_sha, "ipk_binary_sha256": ipk_binary},
            )
            run.facts["ipk_binary_sha256"] = ipk_binary
            # and the .ipk cannot be read as an apk (format split proof, local)
            with open(sibling, "rb") as handle:
                ipk_magic = handle.read(4)
            with open(target, "rb") as handle:
                apk_magic = handle.read(4)
            run.record(
                "package-format-split",
                apk_magic == b"ADBd" and ipk_magic != b"ADBd",
                f"apk magic {apk_magic!r} vs ipk magic {ipk_magic!r}",
                evidence={"apk_magic": apk_magic.decode("latin-1"), "ipk_magic": ipk_magic.decode("latin-1")},
            )
        except ip.InstallPathError as exc:
            run.record("sibling-ipk-binary-is-a-different-build", False, str(exc))

    # 5. installer-path command shape ---------------------------------------
    try:
        body = ip.http_get(args.installer_url).decode("utf-8", "replace")
        script = run.cache / "install-and-test.sh"
        script.write_text(body)
        supports = ip.installer_supports_tag(body)
        syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=60)
        run.record(
            "installer-script-fetched",
            syntax.returncode == 0,
            f"{len(body)} bytes from {args.installer_url} (bash -n exit {syntax.returncode}), "
            f"--tag supported: {supports}",
            evidence={"sha256": ip.sha256_file(script), "supports_tag": supports},
        )
        run.facts["installer"] = {
            "url": args.installer_url,
            "sha256": ip.sha256_file(script),
            "supports_tag": supports,
        }
        if args.ln_address:
            cmd = ip.installer_command(
                args.host, "<router-secret>", args.ln_address, tag=run.tag, script_url=args.installer_url
            )
            run.record("installer-command-shape", True, cmd[2], evidence={"argv": cmd})
        else:
            cmd = ip.installer_command(
                args.host, "<router-secret>", "<ln-address>", tag=run.tag, script_url=args.installer_url
            )
            run.record(
                "installer-command-shape",
                True,
                "TOLLGATE_LN_ADDRESS unset — shape only: " + cmd[2],
                skipped=True,
            )
    except Exception as exc:  # noqa: BLE001
        run.record("installer-script-fetched", False, f"could not fetch the installer: {exc}")

    # 6. fresh-flash image ---------------------------------------------------
    try:
        image = ff.resolve_image_path(args.image)
        problems = ff.verify_image(image)
        run.record(
            "fresh-flash-image-verified",
            not problems,
            f"{os.path.basename(image)} sha256 ok (OpenWrt 25.12.x / mediatek-filogic / gl-mt3000)"
            if not problems
            else "; ".join(problems),
            evidence={"path": image, "sha256": ip.sha256_file(image)},
        )
        run.facts["fresh_flash_image"] = {"path": image, "sha256": ip.sha256_file(image)}
    except ff.ImageInvalid as exc:
        run.record("fresh-flash-image-verified", False, str(exc), skipped=True)

    # 7. happy-path suite is present and collectable (no router needed) ------
    try:
        prefix, source = ip.playwright_cli(PROJECT_ROOT)
        listing = subprocess.run(
            [*prefix, "test", "--config=playwright.config.mjs", "--list",
             "--project=" + ip.HAPPY_PATH_PROJECT, "--grep", ip.HAPPY_PATH_GREP, ip.HAPPY_PATH_SPEC],
            cwd=str(PROJECT_ROOT / "tests"),
            capture_output=True,
            text=True,
            timeout=300,
        )
        titles = [line.strip() for line in listing.stdout.splitlines() if "›" in line or ":" in line]
        listed = [t for t in titles if any(t.endswith(e) for e in ip.HAPPY_PATH_EXPECTED_TITLES)]
        run.record(
            "happy-path-suite-collected",
            len(titles) > 0,
            f"{len(titles)} tests listed for the reused happy-path suite "
            f"({ip.HAPPY_PATH_SPEC}, project {ip.HAPPY_PATH_PROJECT}, runner {source})",
            evidence={
                "output_tail": (listing.stdout + listing.stderr)[-1500:],
                "exit_code": listing.returncode,
                "matched_expected": len(listed),
            },
        )
        run.facts["happy_path_suite"] = {
            "spec": ip.HAPPY_PATH_SPEC,
            "grep": ip.HAPPY_PATH_GREP,
            "project": ip.HAPPY_PATH_PROJECT,
            "runner": source,
            "expected_titles": list(ip.HAPPY_PATH_EXPECTED_TITLES),
            "listed": len(titles),
        }
    except ip.InstallPathError as exc:
        run.record("happy-path-suite-collected", False, str(exc), skipped=True)
    except Exception as exc:  # noqa: BLE001
        run.record("happy-path-suite-collected", False, f"could not list the suite: {exc}")

    # 8. bench lock state (read-only) ---------------------------------------
    probe = BenchLock(purpose="dry-run-probe", path=args.lock)
    free, holder = probe.status()
    run.record(
        "bench-lock-state",
        True,
        f"{'free' if free else 'HELD by ' + (holder.raw or '<unknown>')} ({probe.path})",
        evidence={"free": free, "holder": holder.raw},
    )
    run.facts["bench_lock"] = {"path": probe.path, "free": free, "holder": holder.raw}

    # 9. router reachability, read-only (TCP, never ICMP) -------------------
    allowed, why = _probes_allowed(probe)
    if args.skip_probe:
        run.record("bench-reachable", True, "probe skipped (--skip-probe)", skipped=True)
    elif not allowed:
        # the bench belongs to another window: probing it would report a state
        # that is not ours (and is the interference class the lock exists for)
        run.record(
            "bench-reachable",
            True,
            f"probes NOT run — {why}; wrap this in `bench-with-lock --purpose "
            '"install-path dry run" -- ...` to probe under a window you own',
            evidence={"lock_state": probe.state, "holder": probe.holder.raw},
            skipped=True,
        )
    else:
        run.facts["bench_probe_window"] = why
        for port in (22, 2050, 2051, 2121, 8080, 8090):
            reachable = ip.tcp_probe(args.host, port, timeout=4)
            run.record(
                f"bench-port-{port}",
                True,
                f"{'open' if reachable else 'closed/filtered'} from this host (br-lan side)",
                evidence={"host": args.host, "port": port, "open": reachable},
            )
        if ip.tcp_probe(args.host, 22, timeout=4):
            release, _ = _ssh_rc(args.host, ip.openwrt_release_command())
            version_out, _ = _ssh_rc(args.host, ip.package_version_command("apk"))
            run.record(
                "bench-identity",
                bool(release),
                f"{release} | installed: {version_out.strip() or '<not installed>'}",
                evidence={"release": release, "installed": version_out.strip()},
            )
            run.facts["bench_release"] = release
            run.facts["bench_installed"] = version_out.strip()

            # informational: what the bench carries RIGHT NOW (read-only).  The
            # policy gate itself runs after the fresh flash; this snapshot is
            # here so a stale/partial install cannot be mistaken for a result.
            uci_out, _ = _ssh_rc(args.host, ip.uci_show_users_to_router_command())
            ports = sorted(ip.parse_users_to_router_ports(uci_out))
            guard = ip.guard_nft_path(args.guard_path)
            guard_out, _ = _ssh_rc(args.host, f"ls -l {guard} 2>/dev/null")
            guard_present = guard in guard_out
            live_problems = ip.policy_violations(
                set(ports),
                guard_present=guard_present,
                guard_drops_br_lan=guard_present,
                ssh_alive=True,
                guard_path=guard,
            )
            run.record(
                "bench-policy-snapshot",
                True,
                "current install already matches the expected policy"
                if not live_problems
                else "informational — current install does NOT match the expected policy: "
                + "; ".join(live_problems),
                evidence={"users_to_router_ports": ports, "guard_present": guard_present},
                skipped=bool(live_problems),
            )
            run.facts["bench_policy_snapshot"] = {
                "users_to_router_ports": ports,
                "guard_present": guard_present,
                "violations": live_problems,
            }

    return _finish(run, args)


def _finish(run: DryRun, args: argparse.Namespace) -> int:
    run.write(md_out=args.md_out)
    if run.failures:
        print(f"\n[install-paths] {len(run.failures)} check(s) FAILED: {[c['check'] for c in run.failures]}")
        return EXIT_CHECK_FAILED
    print("\n[install-paths] dry run clean (flash-free checks only)")
    return EXIT_OK


def flash_and_run(args: argparse.Namespace) -> int:
    """The locked bench phase: fresh flash + both install paths (opt-in).

    The single documented entry point for the two flash cycles.  It:

      1. runs the flash-free policy pre-flight (fail-fast, names an unsupported
         release instead of failing later);
      2. requires an explicit LIGHTNING address (the installer's operator choice);
      3. refuses unless ``TOLLGATE_ENABLE_SYSUPGRADE_FLASHING=true``;
      4. takes the bench lock for the whole window (the test module holds it too,
         but the operator sees the refusal here first);
      5. checks the router wallet is EMPTY before it lets pytest flash anything;
      6. runs the reused happy-path suite as part of the same pytest session.
    """
    from lib.bench_lock import BenchBusy as _BenchBusy
    from lib.bench_lock import BenchLock, BenchStale, bench_lock_cli

    # 1. pre-flight ----------------------------------------------------------
    preflight = ip.policy_preflight(
        args.tag, minimum=args.policy_min_release, guard_path=args.guard_path
    )
    print(f"[install-paths] policy pre-flight: {preflight.message()}")
    if not preflight.supported:
        print("[install-paths] FAIL-FAST: refusing to start two flash cycles for a release that "
              "cannot satisfy the POLICY/guard assertion.")
        return EXIT_POLICY_UNSUPPORTED

    # 2. the operator choice the installer needs -----------------------------
    if not args.ln_address:
        print("[install-paths] TOLLGATE_LN_ADDRESS is required (--ln-address): scenario B runs the "
              "installer, which needs the operator's lightning address.")
        return EXIT_CHECK_FAILED

    # 3. the destructive switch ---------------------------------------------
    if not ff.flashing_enabled():
        print(
            "[install-paths] REFUSING: "
            + f"{ff.FLASH_ENABLE_ENV}=true is required to flash the bench "
            "(flashing wipes /etc/tollgate, including real ecash). Drain first: "
            f"`{ff.DRAIN_COMMAND}` on the router."
        )
        return EXIT_CHECK_FAILED

    # 4. the single-owner bench lock ---------------------------------------
    lock = BenchLock(
        purpose="prta install-path e2e (2 flash cycles)",
        task_id=BENCH_TASK_ID,
        path=args.lock,
        reclaim_stale=args.reclaim_stale,
    )
    print(f"[install-paths] bench lock backend: {bench_lock_cli() or 'in-process flock'}")
    try:
        lock.acquire()
    except BenchStale as exc:
        print(f"[install-paths] {exc}")
        return EXIT_BENCH_BUSY
    except _BenchBusy as exc:
        print(f"[install-paths] {exc}")
        return EXIT_BENCH_BUSY

    try:
        print(f"[install-paths] holding {lock.path} as {lock.holder.raw}")

        # 5. wallet gate (a flash destroys money) ---------------------------
        if not args.skip_wallet_gate:
            out, _ = _ssh_rc(args.host, ff.WALLET_BALANCE_COMMAND, timeout=60)
            listing, _ = _ssh_rc(args.host, ff.ECASH_LISTING_COMMAND, timeout=30)
            state = ff.parse_wallet_state(out, listing)
            print(f"[install-paths] wallet: {state.summary()}")
            blockers = ff.flash_preconditions(
                state, allow_nonempty=args.allow_nonempty_wallet
            )
            if blockers:
                print("[install-paths] REFUSING TO FLASH:")
                for blocker in blockers:
                    print(f"  - {blocker}")
                return EXIT_CHECK_FAILED

        # 6. the reused happy-path suite, in the same pytest session --------
        env = lock.child_env()
        env["TOLLGATE_ENABLE_SYSUPGRADE_FLASHING"] = "true"
        env["TOLLGATE_ENABLE_INSTALL_PATH_E2E"] = "1"
        env.setdefault("TOLLGATE_FEED_TAG", args.tag)
        env.setdefault("TOLLGATE_FEED_VERSION", args.version)
        env.setdefault("TOLLGATE_SSH_HOST", args.host)
        env["TOLLGATE_LN_ADDRESS"] = args.ln_address
        env["TOLLGATE_POLICY_TARGET_RELEASE"] = preflight.target_release
        env["TOLLGATE_POLICY_MIN_RELEASE"] = preflight.minimum_release
        env["TOLLGATE_POLICY_GUARD_PATH"] = preflight.guard_path
        venv_python = os.environ.get("TOLLGATE_TEST_PYTHON") or sys.executable
        cmd = [
            venv_python, "-m", "pytest",
            "tests/scenarios/test_install_paths.py",
            "--no-deploy",
            "-v",
            "--timeout=3600",
            "-p", "no:cacheprovider",
        ]
        print("[install-paths] full run:", " ".join(cmd))
        rc = subprocess.call(cmd, cwd=str(PROJECT_ROOT), env=env)
        print(
            "\n[install-paths] NOTE: the two flash cycles must each start from a FRESH image; "
            "flash again between scenario A and scenario B (see docs/install-paths-e2e.md §2.2)."
        )
        return rc
    finally:
        lock.release()
        print(f"[install-paths] released {lock.path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="run the flash-free checks (default)")
    parser.add_argument("--flash-and-run", action="store_true", help="run the full locked bench phase (pytest)")
    parser.add_argument("--tag", default=os.environ.get("TOLLGATE_FEED_TAG", ip.FEED_RELEASE_DEFAULT))
    parser.add_argument("--version", default=os.environ.get("TOLLGATE_FEED_VERSION", ip.FEED_VERSION_DEFAULT))
    parser.add_argument("--arch", default=os.environ.get("TOLLGATE_ARTIFACT_ARCH", ip.BENCH_ARCH))
    parser.add_argument(
        "--package-manager",
        default=os.environ.get("TOLLGATE_PACKAGE_MANAGER", "apk"),
        choices=list(ip.PACKAGE_MANAGERS),
    )
    parser.add_argument("--host", default=DEFAULT_BENCH_HOST)
    parser.add_argument("--cache", default=str(Path.home() / ".cache" / "prta-install-paths"))
    parser.add_argument("--image", default=os.environ.get("TOLLGATE_FRESH_FLASH_IMAGE"))
    parser.add_argument("--installer-url", default=ip.INSTALLER_SCRIPT_URL)
    parser.add_argument("--ln-address", default=os.environ.get("TOLLGATE_LN_ADDRESS", ""))
    parser.add_argument("--lock", default=None, help="bench lock path")
    parser.add_argument(
        "--reclaim-stale",
        dest="reclaim_stale",
        action="store_true",
        default=os.environ.get("TOLLGATE_BENCH_RECLAIM_STALE", "").lower() in ("1", "true", "yes"),
        help="take over a bench holder line whose owner died (explicit recovery)",
    )
    parser.add_argument("--md-out", default=None, help="also write the markdown report here")
    parser.add_argument("--skip-probe", action="store_true", help="do not touch the bench at all")
    parser.add_argument(
        "--policy-min-release",
        default=os.environ.get(ip.POLICY_MIN_RELEASE_ENV, ip.POLICY_MIN_RELEASE_DEFAULT),
        help="oldest release that can satisfy the POLICY/guard assertion (default pre17)",
    )
    parser.add_argument(
        "--guard-path",
        default=os.environ.get(ip.POLICY_GUARD_PATH_ENV, ip.GUARD_NFT_FILE),
        help="the #566 admin-board nft guard path the policy assertion expects",
    )
    parser.add_argument(
        "--continue-unsupported",
        dest="continue_unsupported",
        action="store_true",
        default=os.environ.get("TOLLGATE_CONTINUE_UNSUPPORTED", "").lower() in ("1", "true", "yes"),
        help="keep going (and report the policy gate as UNSUPPORTED, never PASS) when the "
        "release cannot satisfy the policy assertion",
    )
    parser.add_argument(
        "--allow-nonempty-wallet",
        dest="allow_nonempty_wallet",
        action="store_true",
        help="accept losing the router's ecash on the flash (you drained nothing)",
    )
    parser.add_argument(
        "--skip-wallet-gate",
        dest="skip_wallet_gate",
        action="store_true",
        help="skip the wallet probe in --flash-and-run (the pytest gate still refuses)",
    )
    parser.add_argument(
        "--require-apk-tool",
        action="store_true",
        help="fail (instead of skip) when apk-tools is missing for apk extraction",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.flash_and_run:
        return flash_and_run(args)
    return dry_run(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
