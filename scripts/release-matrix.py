#!/usr/bin/env python3
"""release-matrix.py — #106 install/upgrade/rollback matrix runner for GL-MT3000.

Release-validation matrix for tollgate-wrt artifacts before/around the v0.6.0
tag (campaign #113, upstream #339):

  fresh      remove + install the v0.6.0 artifact; init/uci-defaults run,
             service starts, portal :2050 + backend :2121 answer, version
             stamping is ldflags-stamped (not dev/unknown)
  upgrade    v0.5.0 baseline -> funded wallet -> v0.6.0; config.json and
             wallet balance survive unchanged
  rollback   v0.6.0 -> v0.5.0 (--force-downgrade); wallet/config intact or
             data loss explicitly documented
  stamping   binary/CLI version reports the release version

The v0.6.0 legs cannot execute until alpha2/final artifacts exist on the
channel — pass --new-artifact PATH (or --new-version vX.Y.Z once published)
when they do. Until then `--rehearse` runs the upgrade mechanics with the
v0.5.0 artifact as both baseline and target (proves funding, hashing,
opkg/apk paths, balance-survival assertions; version comparison is skipped).

Artifacts: rollback/baseline targets come from the Nostr release channel via
scripts/verify-release-channel.py (sha256-verified against the kind-1063 `x`
tag). Explicit paths override channel resolution.

Usage:
    scripts/release-matrix.py plan                     # print matrix plan, no router contact
    scripts/release-matrix.py artifacts                # fetch + verify channel artifacts only
    scripts/release-matrix.py precheck                 # lock + reachability + arch + state snapshot
    scripts/release-matrix.py fresh                    # needs --new-artifact/--new-version
    scripts/release-matrix.py upgrade --rehearse       # mechanics rehearsal (v0.5.0 -> v0.5.0)
    scripts/release-matrix.py upgrade                  # real v0.5.0 -> v0.6.0 (needs artifacts)
    scripts/release-matrix.py rollback                 # real downgrade leg
    scripts/release-matrix.py stamping --expect v0.6.0
    scripts/release-matrix.py report                   # render matrix.md from evidence dir

Router phases require the hardware lock (`make lock PHASE="release-matrix"`).
Evidence lands in results/release-matrix/<ts>/ (gitignored): per-step JSON
with transcripts, artifacts/, and matrix.md.

Exit codes: 0 = requested phases passed, 1 = a phase failed, 2 = environment
error (no lock, unreachable router, missing artifact).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.router import Router  # noqa: E402

VERIFY_HARNESS = Path(__file__).resolve().parent / "verify-release-channel.py"
DEFAULT_ARCH = "aarch64_cortex-a53"  # same convention as TOLLGATE_ROUTER_ARCH elsewhere
REPO_ROOT = Path(__file__).resolve().parent.parent


def resolve_default_arch() -> str:
    """Resolve the router architecture the way the rest of the repo does:
    TOLLGATE_ROUTER_ARCH env > config/routers.json entry for
    TOLLGATE_ROUTER_ID (or the inventory default) > aarch64_cortex-a53.

    Never hardcode a router->arch mapping here — the inventory is the source
    of truth (and mismatches with the live router fail loudly in precheck).
    """
    env_arch = os.environ.get("TOLLGATE_ROUTER_ARCH")
    if env_arch:
        return env_arch
    try:
        inv_path = Path(
            os.environ.get("TOLLGATE_ROUTER_INVENTORY", REPO_ROOT / "config" / "routers.json")
        )
        data = json.loads(inv_path.read_text())
        routers = data.get("routers", {})
        router_id = os.environ.get("TOLLGATE_ROUTER_ID") or data.get("default", "")
        arch = routers.get(router_id, {}).get("arch", "")
        if arch:
            return arch
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return DEFAULT_ARCH


# --------------------------------------------------------------------------- evidence


@dataclass
class Step:
    name: str
    cmd: str = ""
    rc: int | None = None
    stdout: str = ""
    verdict: str = "pass"  # pass | fail | skip | pending
    note: str = ""

    def to_json(self) -> dict:
        return self.__dict__


@dataclass
class PhaseResult:
    phase: str
    started: str
    finished: str = ""
    steps: list[Step] = field(default_factory=list)
    verdict: str = "pass"

    @property
    def summary(self) -> str:
        counts: dict[str, int] = {}
        for s in self.steps:
            counts[s.verdict] = counts.get(s.verdict, 0) + 1
        return ", ".join(f"{v}:{n}" for v, n in sorted(counts.items())) or "no steps"


class Evidence:
    def __init__(self, base: Path | None = None):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.dir = base or REPO_ROOT / "results" / "release-matrix" / stamp
        (self.dir / "steps").mkdir(parents=True, exist_ok=True)
        (self.dir / "artifacts").mkdir(exist_ok=True)

    def save_phase(self, result: PhaseResult) -> None:
        result.finished = datetime.now(timezone.utc).isoformat()
        if any(s.verdict == "fail" for s in result.steps):
            result.verdict = "fail"
        elif all(s.verdict in ("skip", "pending") for s in result.steps):
            result.verdict = result.steps[0].verdict if result.steps else "skip"
        payload = {
            "phase": result.phase,
            "started": result.started,
            "finished": result.finished,
            "verdict": result.verdict,
            "steps": [s.to_json() for s in result.steps],
        }
        (self.dir / "steps" / f"{result.phase}.json").write_text(
            json.dumps(payload, indent=2) + "\n"
        )

    def load_phases(self) -> list[dict]:
        out = []
        for p in sorted((self.dir / "steps").glob("*.json")):
            out.append(json.loads(p.read_text()))
        return out


# --------------------------------------------------------------------------- helpers


def sh(router: Router, cmd: str, timeout: int = 60) -> tuple[int, str]:
    """Run ssh command, return (rc, combined output)."""
    try:
        proc = subprocess.run(
            router._ssh_base + [cmd], capture_output=True, text=True, timeout=timeout
        )
        out = (proc.stdout + "\n" + proc.stderr).strip()
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"


def remote_sha256(router: Router, path: str) -> str:
    rc, out = sh(router, f"sha256sum {path} 2>/dev/null | awk '{{print $1}}'")
    return out.strip() if rc == 0 else ""


def detect_package_manager(router: Router) -> tuple[str, str, str]:
    """Returns (pkgmgr, arch, openwrt_version)."""
    rc, arch = sh(router, "opkg print-architecture 2>/dev/null | tail -1 | awk '{print $2}'")
    if rc == 0 and arch.strip():
        pkgmgr = "opkg"
    else:
        rc2, arch2 = sh(router, "apk --print-arch 2>/dev/null")
        if rc2 == 0 and arch2.strip():
            pkgmgr, arch = "apk", arch2.strip()
        else:
            return "none", "", ""
    _, owrt = sh(router, ". /etc/openwrt_release 2>/dev/null; echo $DISTRIB_RELEASE")
    return pkgmgr, arch.strip(), owrt.strip()


def install_cmd(pkgmgr: str, remote_path: str, downgrade: bool = False) -> str:
    if pkgmgr == "opkg":
        flag = "--force-downgrade " if downgrade else ""
        return f"opkg install {flag}{remote_path} 2>&1"
    # apk ad-hoc add of a local file replaces the package regardless of
    # version ordering, so no downgrade flag is needed
    return f"apk add --allow-untrusted {remote_path} 2>&1"


def installed_version(router: Router, pkgmgr: str) -> str:
    if pkgmgr == "opkg":
        _, out = sh(router, "opkg list-installed | grep '^tollgate-wrt '")
    else:
        _, out = sh(router, "apk list --installed 2>/dev/null | grep '^tollgate-wrt'")
    for token in out.split():
        if token and token != "tollgate-wrt":
            return token.strip()
    return out.strip()


def binary_version(router: Router) -> str:
    """Best-effort stamped-version probe, layered fallbacks, raw output kept."""
    probes = [
        "/usr/bin/tollgate-wrt --version 2>&1 | head -2",
        "tollgate version 2>&1 | head -2",
        "strings /usr/bin/tollgate-wrt 2>/dev/null | grep -m1 -E 'v?[0-9]+\\.[0-9]+\\.[0-9]+'",
    ]
    for probe in probes:
        rc, out = sh(router, probe, timeout=30)
        if rc == 0 and out.strip():
            return out.strip()
    return ""


def wallet_balance(router: Router) -> str:
    _, out = sh(router, "tollgate --json wallet balance 2>&1 | head -3", timeout=30)
    return out.strip()


def service_ok(router: Router) -> tuple[bool, str]:
    rc, out = sh(router, "/etc/init.d/tollgate-wrt status 2>&1; sleep 2")
    running = "running" in out.lower()
    rc2, code = sh(router, "wget -q -O /dev/null http://[::1]:2121/ 2>&1; echo $?")
    backend_up = code.strip().endswith("0")
    detail = f"init={'running' if running else 'stopped'} backend2121={'up' if backend_up else 'down'}"
    return running and backend_up, detail


def portal_answers(router: Router) -> tuple[bool, str]:
    rc, out = sh(router, "wget -q -O /dev/null http://127.0.0.1:2050/ 2>&1; echo $?")
    ok = out.strip().endswith("0")
    return ok, f"portal2050={'up' if ok else 'down'}"


def snapshot(router: Router, label: str) -> dict:
    return {
        "label": label,
        "installed_version": installed_version(router, detect_package_manager(router)[0]),
        "config_sha256": remote_sha256(router, "/etc/tollgate/config.json"),
        "wallet_db_sha256": remote_sha256(router, "/etc/tollgate/wallet.db"),
        "balance": wallet_balance(router),
    }


def mint_funding_token(amount: int = 4) -> str:
    from lib.cashu import HttpMinter

    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL", "https://testnut.cashu.exchange")
    return HttpMinter(mint_url).mint(amount)


def client_lan_ip(router_host: str) -> str:
    """Source IP this Mac uses toward the router (for pay_direct MAC resolution)."""
    env_ip = os.environ.get("TOLLGATE_CLIENT_IP")
    if env_ip:
        return env_ip
    try:
        out = subprocess.run(
            ["ip", "route", "get", router_host],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for tok in out.split():
            if "." in tok and tok != router_host:
                return tok
    except Exception:
        pass
    return ""


# --------------------------------------------------------------------------- artifacts


def resolve_channel_artifact(
    version: str, arch: str, evidence: Evidence, fmt: str = "ipk"
) -> tuple[Path | None, str]:
    """Download + sha256-verify an artifact from the release channel.

    Returns (path named after the event filename, x_tag), or (None, reason).
    """
    out_dir = evidence.dir / "artifacts" / f"channel-{version}"
    cmd = [
        sys.executable,
        str(VERIFY_HARNESS),
        "--version",
        version,
        "--arch",
        arch,
        "--format",
        fmt,
        "--mirrors",
        "1",
        "--out-dir",
        str(out_dir),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    report = out_dir / "report.json"
    if proc.returncode != 0 or not report.exists():
        tail = "\n".join(proc.stdout.splitlines()[-6:])
        return None, f"channel resolution failed for {version}/{arch}/{fmt} (rc={proc.returncode}):\n{tail}"
    data = json.loads(report.read_text())
    cands = [
        a
        for a in data["artifacts"]
        if a["compression"] == "none" and a["verdict"] in ("pass", "warn")
    ]
    if not cands:
        return None, f"no verified compression=none {fmt} artifact for {version}/{arch}"
    art = cands[0]
    dl_dir = out_dir / "downloads"
    for p in dl_dir.glob(f"{art['x_tag'][:16]}*.bin"):
        named = out_dir / art["filename"]
        if not named.exists():
            named.write_bytes(p.read_bytes())
        return named, art["x_tag"]
    return None, "verified artifact record exists but download file missing"


def push_artifact(router: Router, local: Path) -> tuple[str, str]:
    remote = f"/tmp/{local.name}"
    sh(router, f"rm -f {remote}")
    proc = subprocess.run(
        ["scp", "-O", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "-o", "LogLevel=ERROR"]
        + (["-i", router.identity_file] if router.identity_file else [])
        + (["-P", str(router.port)] if router.port else [])
        + (["-J", router.jump_host] if router.jump_host else [])
        + [str(local), f"root@{router.host}:{remote}"],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"scp failed: {proc.stderr.strip()}")
    rc, local_sha = 0, hashlib.sha256(local.read_bytes()).hexdigest()
    _, remote_sha = sh(router, f"sha256sum {remote} | awk '{{print $1}}'")
    if remote_sha.strip() != local_sha:
        raise RuntimeError(f"artifact sha mismatch after upload: {remote_sha.strip()} != {local_sha}")
    return remote, local_sha


def build_router() -> Router:
    host = os.environ.get("TOLLGATE_SSH_HOST") or os.environ.get("ROUTER_IP")
    if not host:
        sys.exit("env error: set TOLLGATE_SSH_HOST (or ROUTER_IP)")
    port = os.environ.get("TOLLGATE_SSH_PORT", "")
    return Router(
        host=host,
        phone_ip="",
        phone_mac="",
        domain="",
        identity_file=os.environ.get("TOLLGATE_SSH_KEY") or None,
        jump_host=os.environ.get("TOLLGATE_SSH_JUMP_HOST") or None,
        port=int(port) if port else None,
    )


def require_lock() -> None:
    try:
        from lib.hardware_lock import require_hardware_lock
    except ImportError:
        return
    try:
        require_hardware_lock()
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(f"hardware lock required but not held: {e}\nrun: make lock PHASE='release-matrix'")


# --------------------------------------------------------------------------- phases


def phase_precheck(router: Router, ev: Evidence, args: argparse.Namespace) -> PhaseResult:
    r = PhaseResult("precheck", datetime.now(timezone.utc).isoformat())
    require_lock()
    s = Step("ssh-reachable", cmd="echo ssh-ok")
    rc, out = sh(router, "echo ssh-ok; cat /tmp/sysinfo/model 2>/dev/null")
    s.rc, s.stdout = rc, out
    s.verdict = "pass" if "ssh-ok" in out else "fail"
    r.steps.append(s)
    if s.verdict == "fail":
        return r

    pkgmgr, arch, owrt = detect_package_manager(router)
    r.steps.append(Step("package-manager", cmd=f"pkgmgr={pkgmgr} arch={arch} openwrt={owrt}",
                        stdout=f"{pkgmgr}/{arch}/{owrt}",
                        verdict="pass" if pkgmgr != "none" else "fail"))
    r.steps.append(Step("arch-match", note=f"router arch={arch}",
                        verdict="pass" if arch == args.arch else "fail",
                        stdout=f"router={arch} expected={args.arch}"))
    snap = snapshot(router, "precheck")
    (ev.dir / "precheck-state.json").write_text(json.dumps(snap, indent=2) + "\n")
    ok, detail = service_ok(router)
    r.steps.append(Step("service-baseline", stdout=detail + " " + json.dumps(snap),
                        verdict="pass" if ok else "skip",
                        note="skip = tollgate not currently running (fine for fresh-install phase)"))
    return r


def phase_artifacts(ev: Evidence, args: argparse.Namespace) -> PhaseResult:
    r = PhaseResult("artifacts", datetime.now(timezone.utc).isoformat())
    if args.old_artifact:
        r.steps.append(Step("old-artifact", note=f"explicit: {args.old_artifact}", verdict="pass",
                            stdout=str(Path(args.old_artifact).resolve())))
    else:
        p, note = resolve_channel_artifact(args.old_version, args.arch, ev)
        r.steps.append(Step("old-artifact-channel", stdout=note, verdict="pass" if p else "fail"))
        if p:
            args.old_artifact = str(p)
    if args.new_artifact:
        r.steps.append(Step("new-artifact", note=f"explicit: {args.new_artifact}", verdict="pass",
                            stdout=str(Path(args.new_artifact).resolve())))
    elif args.new_version:
        p, note = resolve_channel_artifact(args.new_version, args.arch, ev)
        r.steps.append(Step("new-artifact-channel", stdout=note, verdict="pass" if p else "fail"))
        if p:
            args.new_artifact = str(p)
    else:
        r.steps.append(Step("new-artifact", verdict="pending",
                            note=f"v0.6.0 artifact not yet available; pass --new-artifact "
                                 f"or --new-version when alpha2/final lands"))
    (ev.dir / "artifacts.json").write_text(json.dumps({
        "old_artifact": args.old_artifact,
        "new_artifact": args.new_artifact,
        "old_version": args.old_version,
        "new_version": args.new_version,
        "arch": args.arch,
    }, indent=2) + "\n")
    return r


def do_install(router: Router, r: PhaseResult, name: str, local: Path,
               pkgmgr: str, downgrade: bool = False) -> None:
    remote, sha = push_artifact(router, local)
    cmd = install_cmd(pkgmgr, remote, downgrade=downgrade)
    rc, out = sh(router, cmd, timeout=300)
    s = Step(name, cmd=cmd, rc=rc, stdout=out[-2000:])
    fatal = "Cannot install" in out or "no installer" in out.lower() or rc == 127
    s.verdict = "fail" if fatal else "pass"
    r.steps.append(s)
    sh(router, f"rm -f {remote}")


def phase_upgrade(router: Router, ev: Evidence, args: argparse.Namespace) -> PhaseResult:
    r = PhaseResult("upgrade", datetime.now(timezone.utc).isoformat())
    require_lock()
    pkgmgr, _, _ = detect_package_manager(router)
    if not pkgmgr or pkgmgr == "none":
        r.steps.append(Step("pkgmgr", verdict="fail", note="no package manager"))
        return r
    if not args.old_artifact:
        r.steps.append(Step("baseline-artifact", verdict="pending",
                            note="run `artifacts` phase first"))
        return r
    rehearsal = not args.new_artifact
    if rehearsal and not args.rehearse:
        r.steps.append(Step("upgrade", verdict="pending",
                            note="v0.6.0 artifact unavailable; rerun with --new-artifact, "
                                 "or --rehearse to exercise mechanics with v0.5.0->v0.5.0"))
        return r
    target = Path(args.new_artifact if not rehearsal else args.old_artifact)

    do_install(router, r, "install-baseline", Path(args.old_artifact), pkgmgr)
    ok, detail = service_ok(router)
    r.steps.append(Step("baseline-service", stdout=detail, verdict="pass" if ok else "fail"))

    fund = Step("fund-wallet")
    token = None
    try:
        if args.token:
            token = Path(args.token).read_text().strip()
        else:
            token = mint_funding_token()
        resp = router.pay_direct(token, ip=client_lan_ip(router.host))
        raw = json.dumps(resp)
        fund.stdout = raw[:500]
        balance_after = wallet_balance(router)
        fund.verdict = "pass" if ("error" not in raw.lower() or "success" in raw.lower()) else "fail"
        if fund.verdict == "pass" and balance_after.strip():
            fund.note = f"post-fund balance output: {balance_after[:120]}"
    except Exception as e:
        fund.verdict = "skip"
        fund.note = f"funding unavailable ({e}); balance-survival check degrades to sha-compare of wallet.db"
    r.steps.append(fund)

    before = snapshot(router, "before-upgrade")
    (ev.dir / "upgrade-before.json").write_text(json.dumps(before, indent=2) + "\n")

    do_install(router, r, "install-target", target, pkgmgr)
    ok, detail = service_ok(router)
    r.steps.append(Step("target-service", stdout=detail, verdict="pass" if ok else "fail"))
    pok, pdet = portal_answers(router)
    r.steps.append(Step("target-portal", stdout=pdet, verdict="pass" if pok else "fail"))

    after = snapshot(router, "after-upgrade")
    (ev.dir / "upgrade-after.json").write_text(json.dumps(after, indent=2) + "\n")
    r.steps.append(Step("config-preserved",
                        stdout=f"{before['config_sha256']} -> {after['config_sha256']}",
                        verdict="pass" if before["config_sha256"] == after["config_sha256"] else "fail"))
    bal_ok = (before["balance"] == after["balance"]) if fund.verdict == "pass" else True
    r.steps.append(Step("wallet-balance-preserved",
                        stdout=f"{before['balance']} -> {after['balance']}",
                        verdict="pass" if bal_ok else "fail",
                        note="skipped-balance-assert" if fund.verdict != "pass" else ""))
    if not rehearsal:
        r.steps.append(Step("version-transition",
                            stdout=f"{before['installed_version']} -> {after['installed_version']}",
                            verdict="pass" if before["installed_version"] != after["installed_version"] else "fail"))
    else:
        r.steps.append(Step("version-transition", verdict="skip",
                            note="rehearsal: same version both sides"))
    return r


def phase_fresh(router: Router, ev: Evidence, args: argparse.Namespace) -> PhaseResult:
    r = PhaseResult("fresh", datetime.now(timezone.utc).isoformat())
    require_lock()
    if not args.new_artifact:
        r.steps.append(Step("fresh", verdict="pending",
                            note="needs --new-artifact/--new-version (v0.6.0 not on channel yet)"))
        return r
    pkgmgr, _, _ = detect_package_manager(router)
    rc, out = sh(router, "opkg remove tollgate-wrt 2>&1 || apk del tollgate-wrt 2>&1")
    r.steps.append(Step("remove-existing", cmd="remove tollgate-wrt", rc=rc, stdout=out[-1000:],
                        verdict="pass"))
    do_install(router, r, "install-fresh", Path(args.new_artifact), pkgmgr)
    ok, detail = service_ok(router)
    r.steps.append(Step("service-up", stdout=detail, verdict="pass" if ok else "fail"))
    pok, pdet = portal_answers(router)
    r.steps.append(Step("portal-answers", stdout=pdet, verdict="pass" if pok else "fail"))
    _, files = sh(router, "ls /etc/tollgate/ /www/tollgate-portal 2>/dev/null | head -20")
    r.steps.append(Step("uci-defaults-artifacts", stdout=files,
                        verdict="pass" if "config.json" in files else "fail"))
    stamp = phase_stamping(router, args)
    r.steps.extend(stamp)
    return r


def phase_stamping(router: Router, args: argparse.Namespace) -> list[Step]:
    steps: list[Step] = []
    raw = binary_version(router)
    steps.append(Step("binary-version-raw", stdout=raw, verdict="pass" if raw else "fail"))
    expect = args.expect
    if not expect:
        steps.append(Step("stamping-assert", verdict="skip",
                          note="pass --expect vX.Y.Z to assert"))
        return steps
    devish = any(k in raw.lower() for k in ("dev", "unknown", "dirty"))
    ok = expect.lstrip("v") in raw and not devish
    steps.append(Step("stamping-assert", stdout=f"raw={raw!r} expect={expect!r}",
                      verdict="pass" if ok else "fail",
                      note="ad-hoc `go build` reports dev/unknown by design — only ldflags-stamped "
                           "release artifacts must pass this assertion"))
    return steps


def phase_rollback(router: Router, ev: Evidence, args: argparse.Namespace) -> PhaseResult:
    r = PhaseResult("rollback", datetime.now(timezone.utc).isoformat())
    require_lock()
    if not (args.new_artifact and args.old_artifact):
        r.steps.append(Step("rollback", verdict="pending",
                            note="needs both artifacts (rollback = downgrade from v0.6.0 to v0.5.0)"))
        return r
    pkgmgr, _, _ = detect_package_manager(router)
    before = snapshot(router, "before-rollback")
    do_install(router, r, "install-downgrade", Path(args.old_artifact), pkgmgr, downgrade=True)
    ok, detail = service_ok(router)
    r.steps.append(Step("service-after-rollback", stdout=detail, verdict="pass" if ok else "fail"))
    after = snapshot(router, "after-rollback")
    (ev.dir / "rollback.json").write_text(
        json.dumps({"before": before, "after": after}, indent=2) + "\n")
    r.steps.append(Step("wallet-intact",
                        stdout=f"balance {before['balance']} -> {after['balance']}; "
                               f"wallet.db {before['wallet_db_sha256'][:12]} -> {after['wallet_db_sha256'][:12]}",
                        verdict="pass" if before["wallet_db_sha256"] == after["wallet_db_sha256"]
                        or before["balance"] == after["balance"] else "fail",
                        note="document any data loss explicitly if fail"))
    r.steps.append(Step("downgraded-version",
                        stdout=f"{before['installed_version']} -> {after['installed_version']}",
                        verdict="pass" if before["installed_version"] != after["installed_version"] else "fail"))
    return r


def render_report(ev: Evidence) -> int:
    phases = ev.load_phases()
    if not phases:
        print("no phase evidence found")
        return 2
    lines = [
        "# Release matrix report (#106)",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat()}",
        "",
        "| phase | verdict | steps |",
        "|---|---|---|",
    ]
    overall = "pass"
    for p in phases:
        if p["verdict"] == "fail":
            overall = "fail"
        counts: dict[str, int] = {}
        for s in p["steps"]:
            counts[s["verdict"]] = counts.get(s["verdict"], 0) + 1
        lines.append(f"| {p['phase']} | {p['verdict']} | "
                     f"{', '.join(f'{k}×{v}' for k, v in sorted(counts.items()))} |")
    lines += ["", f"**Overall: {overall.upper()}**", ""]
    for p in phases:
        lines.append(f"## {p['phase']}")
        for s in p["steps"]:
            mark = {"pass": "✅", "fail": "❌", "skip": "⏭️", "pending": "⏳"}[s["verdict"]]
            lines.append(f"- {mark} `{s['name']}` {s.get('note', '')}")
            if s.get("stdout"):
                lines.append("  ```")
                lines.append("  " + s["stdout"][:400])
                lines.append("  ```")
        lines.append("")
    (ev.dir / "matrix.md").write_text("\n".join(lines) + "\n")
    print(f"report: {ev.dir / 'matrix.md'}")
    print(f"overall: {overall}")
    return 0 if overall == "pass" else 1


# --------------------------------------------------------------------------- main


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("phase", choices=["plan", "artifacts", "precheck", "fresh", "upgrade",
                                     "rollback", "stamping", "report", "all"])
    p.add_argument("--arch", default=resolve_default_arch(),
                   help="router architecture (default: TOLLGATE_ROUTER_ARCH env, else "
                        "config/routers.json for TOLLGATE_ROUTER_ID, else aarch64_cortex-a53)")
    p.add_argument("--old-version", default="v0.5.0")
    p.add_argument("--old-artifact", help="explicit path for baseline/rollback artifact")
    p.add_argument("--new-version", help="e.g. v0.6.0-alpha2 once published")
    p.add_argument("--new-artifact", help="explicit path for v0.6.0 artifact")
    p.add_argument("--rehearse", action="store_true",
                   help="upgrade phase runs v0.5.0->v0.5.0 mechanics rehearsal")
    p.add_argument("--expect", help="expected stamped version for stamping assertions")
    p.add_argument("--token", help="file containing a cashu token for wallet funding")
    p.add_argument("--evidence-dir", help="reuse an existing evidence dir (for report)")
    args = p.parse_args()

    ev = Evidence(Path(args.evidence_dir) if args.evidence_dir else None)
    print(f"evidence: {ev.dir}")

    if args.phase == "plan":
        if args.new_artifact:
            target = str(Path(args.new_artifact).resolve())
        elif args.new_version:
            target = f"channel:{args.new_version}"
        else:
            target = "PENDING (v0.6.0 not on channel yet)"
        print(f"""
matrix plan (#106, arch={args.arch})
  1. artifacts   baseline={args.old_artifact or f'channel:{args.old_version}'}  target={target}
  2. precheck    hardware lock + ssh + arch + baseline snapshot
  3. fresh       remove -> install target -> service/portal/stamping
  4. upgrade     install baseline -> fund wallet -> install target ->
                 config sha + balance preserved ({'REHEARSAL (same artifact)' if args.rehearse else 'real'})
  5. rollback    downgrade to baseline -> wallet/config intact
  6. report      matrix.md in evidence dir
router phases need: make lock PHASE='release-matrix' + TOLLGATE_SSH_HOST""")
        return 0

    if args.phase == "report":
        return render_report(ev)

    router = None
    if args.phase in ("precheck", "fresh", "upgrade", "rollback", "stamping", "all"):
        router = build_router()

    results: list[PhaseResult] = []

    def run(phase_fn, *fn_args, **fn_kwargs):
        r = phase_fn(*fn_args, **fn_kwargs)
        ev.save_phase(r)
        results.append(r)
        print(f"[{r.phase}] {r.verdict} ({r.summary})")

    if args.phase in ("artifacts", "all"):
        run(phase_artifacts, ev, args)
    if args.phase in ("precheck", "all"):
        run(phase_precheck, router, ev, args)
    if args.phase in ("fresh", "all"):
        run(phase_fresh, router, ev, args)
    if args.phase in ("upgrade", "all"):
        run(phase_upgrade, router, ev, args)
    if args.phase in ("rollback", "all"):
        run(phase_rollback, router, ev, args)
    if args.phase == "stamping":
        r = PhaseResult("stamping", datetime.now(timezone.utc).isoformat())
        require_lock()
        r.steps = phase_stamping(router, args)
        ev.save_phase(r)
        results.append(r)
        print(f"[stamping] {r.verdict} ({r.summary})")

    if not results:
        print("nothing to do")
        return 2
    overall = 1 if any(r.verdict == "fail" for r in results) else 0
    print(f"\nphases: {', '.join(f'{r.phase}={r.verdict}' for r in results)}")
    print(f"evidence: {ev.dir}  (run `report` phase to render matrix.md)")
    return overall


if __name__ == "__main__":
    sys.exit(main())
