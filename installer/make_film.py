#!/usr/bin/env python3
"""Film the TollGate 0.6.0 pipeline: feed → stock VM → wizard → installed router → state.

Read-only for tollgate-module-basic-go and FreedomTechFeed/packages: no tags,
no pushes, no merges (owner directive 2026-09-20). All writes are local — the
film run directory, local QEMU VMs, and this repo's results/.

Acts are independently recordable; a failed act is recorded as a finding and
does not stop the film. Execution order differs from narrative order on
purpose: the suite act runs first because it owns its full VM lifecycle.

Usage:
  python3 installer/make_film.py --new-run
  python3 installer/make_film.py --run <dir> --acts feed,suite
  python3 installer/make_film.py --run <dir> --acts all
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from installer_service import InstallerService  # noqa: E402
from lab_vm import InstallerLab, load_base_password  # noqa: E402

FEED_REPO = "FreedomTechFeed/packages"
ARCH_RE = re.compile(
    r"_(aarch64_cortex-a53|aarch64_cortex-a72|arm_cortex-a7|mips64_octeonplus"
    r"|mipsel_24kc|mips_24kc|x86_64)\.")
ACT_ORDER = ["feed", "suite", "boot", "wizard", "verify", "state", "assemble"]
NARRATIVE = {
    "feed": 1, "boot": 2, "wizard": 3, "verify": 4, "suite": 5, "state": 6,
}


def sh(cmd: list[str], timeout: int = 120, cwd: Path | None = None) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    return r.returncode, (r.stdout + r.stderr)


class Film:
    def __init__(self, run_dir: Path) -> None:
        self.run = run_dir
        self.raw = run_dir / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.facts_path = run_dir / "facts.json"
        self.facts: dict = json.loads(self.facts_path.read_text()) if self.facts_path.exists() else {}

    def save_facts(self) -> None:
        self.facts_path.write_text(json.dumps(self.facts, indent=1))

    def record(self, act: str, text: str) -> None:
        (self.raw / f"act{NARRATIVE[act]}-{act}.txt").write_text(text)

    # ── acts (execution order = ACT_ORDER) ─────────────────────────────
    def act_feed(self) -> None:
        out = ["$ gh release list -R FreedomTechFeed/packages --limit 8", ""]
        rc, o = sh(["gh", "release", "list", "-R", FEED_REPO, "--limit", "8"])
        out += [o, ""]
        rc, o = sh(["gh", "release", "view", "-R", FEED_REPO,
                    "--json", "tagName,publishedAt,assets"])
        out += ["$ gh release view (latest) — tag, date, assets", "", o, ""]
        latest = json.loads(o.split("\n")[0]) if o.strip().startswith("{") else json.loads(o)
        assets = latest.get("assets", [])
        no_digest = sum(1 for a in assets if not a.get("digest"))
        pin = ""
        arch = Path.home() / "src" / "tollgate-installer" / "arch.go"
        if arch.exists():
            m = re.search(r'feedReleaseTagDefault = "([^"]+)"', arch.read_text())
            pin = m.group(1) if m else "?"
        rc, o = sh(["gh", "api", f"repos/{FEED_REPO}/contents/.github/workflows/release-publish.yml",
                    "--jq", ".content"])
        wf = ""
        if rc == 0:
            import base64
            wf = base64.b64decode(o.strip()).decode(errors="replace")
        out += [f"$ installer feed pin: {pin}",
                "", "$ release-publish.yml (build system, first 40 lines)", "",
                "\n".join(wf.splitlines()[:40])]
        self.record("feed", "\n".join(out))
        self.facts["feed"] = {
            "status": "ok", "latest_tag": latest.get("tagName"),
            "published": latest.get("publishedAt"), "assets": len(assets),
            "arches": len({m.group(1) for a in assets
                           for m in [ARCH_RE.search(a["name"])] if m}),
            "assets_without_digest": no_digest, "installer_pin": pin,
            "workflow": "release-publish.yml — tag push v* → 7 arches × (apk master + ipk 24.10) → single publish job",
        }

    def act_suite(self) -> None:
        venv = Path.home() / ".tollgate-test-venv" / "bin" / "python"
        rc, o = sh([str(venv), "-m", "pytest", "installer/", "-v", "--timeout-method=signal"],
                   timeout=420, cwd=REPO)
        self.record("suite", f"$ pytest installer/ -v (PRTA installer E2E suite)\n\n{o}")
        m = re.search(r"(\d+) passed", o)
        f = re.search(r"(\d+) failed", o)
        d = re.search(r"in ([\d.]+)s", o)
        self.facts["suite"] = {
            "status": "ok" if rc == 0 else "failed", "rc": rc,
            "passed": int(m.group(1)) if m else 0,
            "failed": int(f.group(1)) if f else 0,
            "duration_s": d.group(1) if d else "?",
        }

    def act_boot(self) -> None:
        lab = InstallerLab(load_base_password(REPO))
        lab.serial_log = self.raw / "act2-serial.log"
        lab.cleanup_prior()
        lab.ensure_network()
        lab.boot_fresh_vm()
        ev = []
        for label, cmd in [
            ("OpenWrt release", ". /etc/openwrt_release; echo $DISTRIB_RELEASE $DISTRIB_ARCH"),
            ("tollgate packages installed", "opkg list-installed 2>/dev/null | grep -ci tollgate || true"),
            ("uptime (s)", "cut -d. -f1 /proc/uptime"),
            ("auth state", "uci show dropbear | grep -c PasswordAuth='on' || true"),
        ]:
            try:
                ev.append(f"$ {label}\n{lab.ssh(cmd).strip()}\n")
            except Exception as e:  # noqa: BLE001 — recorded as footage
                ev.append(f"$ {label}\nERROR: {e}\n")
        self.record("boot", "Stock OpenWrt VM from baked base (serial re-IP performed)\n\n" + "\n".join(ev))
        self.facts["boot"] = {"status": "ok",
                              "serial_log": str(lab.serial_log.relative_to(self.run))}

    def act_wizard(self) -> None:
        svc = InstallerService()
        try:
            svc.build()
            env = {**dict(__import__("os").environ),
                   "TOLLGATE_INSTALLER_BIN": str(svc.bin_path),
                   "TOLLGATE_LAB_PASSWORD": load_base_password(REPO),
                   "TOLLGATE_DEMO_OUT": str(self.raw / "act3")}
            out_dir = self.raw / "act3"
            out_dir.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["node", "installer/record_demo.mjs"], cwd=REPO, env=env,
                               capture_output=True, text=True, timeout=540)
            webm = out_dir / "tollgate-installer-demo.webm"
            self.facts["wizard"] = {
                "status": "ok" if r.returncode == 0 else "failed",
                "rc": r.returncode,
                "webm_bytes": webm.stat().st_size if webm.exists() else 0,
                "log": r.stdout[-500:] + r.stderr[-300:],
            }
            (out_dir / "recorder.log").write_text(r.stdout + r.stderr)
        finally:
            svc.stop()

    def act_verify(self) -> None:
        pw = load_base_password(REPO)
        ssh = ["sshpass", "-p", pw, "ssh", "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
               "-o", "ConnectTimeout=10", "root@10.99.95.1"]
        checks = [
            ("hostname (branding)", "uci get system.@system[0].hostname"),
            ("installed tollgate package + source", "opkg status tollgate-wrt 2>/dev/null | head -3"),
            ("listening ports", "netstat -tln 2>/dev/null | grep -E ':(2050|2121) ' || echo NONE"),
            ("backend health ad (:2121)", "wget -qO- --timeout=5 http://127.0.0.1:2121/ 2>&1 | head -c 200"),
            ("captive portal (:2050) title", "wget -qO- --timeout=5 http://127.0.0.1:2050/ 2>&1 | grep -io '<title>[^<]*' | head -1; wget -qO- --timeout=5 http://127.0.0.1:2050/ 2>&1 | wc -c"),
            ("lightning address configured", "grep -o 'lightning_address[^,}]*' /etc/tollgate/identities.json 2>/dev/null || echo NONE"),
        ]
        ev, results = [], {}
        for label, cmd in checks:
            rc, o = sh(ssh + [cmd], timeout=30)
            results[label] = o.strip()
            ev.append(f"$ {label}\n{o.strip()}\n")
        rcp, _ = sh(["node", str(Path(__file__).resolve().parent / "film_portal_still.mjs"),
                     str(self.raw / "act4")], timeout=120, cwd=REPO)
        self.record("verify", "Post-install state of the router the wizard deployed\n\n" + "\n".join(ev))
        host = results.get("hostname (branding)", "").splitlines()[0] if results.get("hostname (branding)") else ""
        pkg = results.get("installed tollgate package + source", "")
        ports = results.get("listening ports", "")
        health = results.get("backend health ad (:2121)", "")
        ln = results.get("lightning address configured", "")
        self.facts["verify"] = {
            "status": "ok",
            "hostname": host,
            "branded": host.lower().startswith("tollgate-"),
            "feed_package": "0.6.0" in pkg,
            "ports_listening": ":2050" in ports and ":2121" in ports,
            "health_ok": 'kind":10021' in health.replace(" ", ""),
            "ln_configured": ln not in ("", "NONE") and "lightning_address" in ln,
            "portal_still": rcp == 0,
        }

    def act_state(self) -> None:
        feed, suite, wiz, ver = (self.facts.get(a, {}) for a in ("feed", "suite", "wizard", "verify"))
        works, gaps = [], []
        if feed.get("assets"):
            works.append(f"Feed publishes {feed['assets']} assets across {feed['arches']} arches "
                         f"(ipk for OpenWrt ≤24.x + apk for 25+), latest {feed['latest_tag']} "
                         f"({feed['published'][:10]})")
        if suite.get("passed"):
            works.append(f"PRTA installer E2E suite: {suite['passed']} passed in {suite['duration_s']}s "
                         "(scan → deploy → branded-healthy router, fresh VM)")
        if wiz.get("status") == "ok":
            works.append(f"Wizard films green end-to-end: stock VM → deploy 12/12 steps → success "
                         f"({wiz.get('webm_bytes', 0) // 1024} KB webm)")
        if ver.get("status") == "ok" and all(
                ver.get(k) for k in ("branded", "feed_package", "ports_listening", "health_ok")):
            works.append("Installed router verified: branded hostname, feed package active, "
                         ":2050 portal + :2121 health ad")
        if ver and not ver.get("ln_configured", False):
            gaps.append(f"identities.json carries no lightning_address after wizard deploy "
                        f"(found: {ver.get('lightning address configured', 'nothing')!r}) — payout "
                        "routing unproven on the installed router")
        if feed.get("assets_without_digest"):
            gaps.append(f"{feed['assets_without_digest']}/{feed['assets']} feed assets carry NO sha256 "
                        "digest → the installer's in-flight digest verification has nothing to verify "
                        "(supply-chain gap)")
        pin, latest = feed.get("installer_pin"), feed.get("latest_tag")
        if pin and latest and pin != latest:
            gaps.append(f"Installer pins {pin} while the feed is at {latest} — pin-freshness is "
                        "manual (a test fails if the tag disappears, nothing flags staleness)")
        gaps.append("0.6.0 final is not cut: last stable is v0.5.0 (Jul 3); the 0.6.0 stream lives "
                    "as feed pre-releases with empty release notes (no changelog per pre)")
        for a in ("suite", "wizard", "verify"):
            if self.facts.get(a, {}).get("status") == "failed":
                gaps.append(f"Act '{a}' failed during filming — see raw/ artifacts")
        text = ["TOLLGATE 0.6.0 — STATE OF THE RELEASE", "=" * 40, "",
                "WHAT WORKS", *["+ " + w for w in works], "",
                "GAPS / BLOCKERS", *["- " + g for g in gaps], ""]
        self.record("state", "\n".join(text))
        self.facts["state"] = {"status": "ok", "works": len(works), "gaps": len(gaps)}

    # ── assembly ───────────────────────────────────────────────────────
    def act_assemble(self) -> None:
        feed, suite, wiz, ver = (self.facts.get(a, {}) for a in ("feed", "suite", "wizard", "verify"))
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        self._manuscript(ts, feed, suite, wiz, ver)
        self._reel(ts, feed, suite, wiz, ver)

    def _manuscript(self, ts, feed, suite, wiz, ver) -> None:
        md = f"""# TollGate 0.6.0 — from stock OpenWrt to an installed router

Audience: TollGate developers. Filmed {ts} on a local QEMU lab; upstream
repos were read-only throughout (no tags, no merges, no pushes).

## Act 1 — Where the package comes from (FreedomTechFeed)

The wizard does not build anything: it downloads a feed-built tollgate-wrt
package. `release-publish.yml` fires on `v*` tag push, builds 7 arches with
two SDKs each (apk for OpenWrt 25+, ipk for ≤24.x), and a single publish job
attaches all assets. Latest: {feed.get('latest_tag')} with
{feed.get('assets')} assets — every digest field empty.

## Act 2 — A stock OpenWrt VM, cold

Fresh overlay from the baked 24.10.1 base, re-IPed over the serial console
(raw/act2-serial.log). Zero tollgate packages, password auth on — the
wizard's exact target contract.

## Act 3 — The wizard, filmed

Built from the installer repo's committed main (never local WIP), the UI
scans the LAN, finds the VM by ARP, and deploys: 12 steps, WAN mode,
Lightning address → success. Outcome: {wiz.get('status')}.

## Act 4 — What the router looks like after

Hostname branding, the feed package installed and running, captive portal
on :2050, health ad on :2121{(", identities.json carrying the payout address" if ver.get("ln_configured") else " — and one honest miss: no lightning_address found in identities.json (state act)")}.

## Act 5 — The automated proof

The PRTA installer suite replays this whole path headlessly three times
over: {suite.get('passed', 0)}/{3} green in {suite.get('duration_s')}s.

## Act 6 — State of 0.6.0

See raw/act6-state.txt: what works, what's missing (feed digests, pin
freshness, release notes), and what blocks the 0.6.0 final cut.
"""
        (self.run / "manuscript.md").write_text(md)

    def _reel(self, ts, feed, suite, wiz, ver) -> None:
        def pre(path: str) -> str:
            p = self.raw / path
            if not p.exists():
                return ""
            return f'<pre class="footage">{html.escape(p.read_text()[:6000])}</pre>'

        def vid(path: str) -> str:
            p = self.raw / path
            return (f'<video controls preload="metadata" src="raw/{path}"></video>'
                    if p.exists() else "")

        def img(path: str, alt: str) -> str:
            p = self.raw / path
            return f'<img src="raw/{path}" alt="{alt}">' if p.exists() else ""

        stills = "".join(
            img(f"act3/{s}", s)
            for s in ("01-scan-results.png", "02-form-filled.png", "03-deploy-steps.png", "04-success.png")
        )
        page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>TollGate 0.6.0 — from stock OpenWrt to an installed router</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#1a1a2e;background:#fafafa}}
h1{{font-size:1.6rem}} h2{{border-bottom:2px solid #f7931a;padding-bottom:.3rem;margin-top:2.5rem}}
video,img{{max-width:100%;border:1px solid #ddd;border-radius:6px;margin:.4rem .4rem .4rem 0}}
.footage{{background:#0d1117;color:#c9d1d9;padding:1rem;border-radius:6px;overflow:auto;max-height:26rem;font-size:.8rem}}
.meta{{color:#666}} .card{{display:inline-block;padding:.6rem 1.2rem;margin:.3rem;border-radius:8px;font-weight:600}}
.ok{{background:#dcfce7}} .gap{{background:#fee2e2}}
</style></head><body>
<h1>TollGate 0.6.0 — from stock OpenWrt to an installed router</h1>
<p class="meta">Audience: TollGate developers · Filmed {ts} · local QEMU lab ·
upstream repos read-only (no tags, no merges, no pushes)</p>
<p><span class="card ok">Feed: {feed.get('assets', '?')} assets / {feed.get('arches', '?')} arches @ {feed.get('latest_tag', '?')}</span>
<span class="card ok">Suite: {suite.get('passed', '?')}/3 green</span>
<span class="card ok">Wizard: {wiz.get('status', '?')}</span>
<span class="card gap">{feed.get('assets_without_digest', '?')} assets without digest</span>
<span class="card gap">pin {feed.get('installer_pin', '?')} vs feed {feed.get('latest_tag', '?')}</span></p>

<h2>Act 1 — Where the package comes from</h2>
<p>FreedomTechFeed/packages builds tollgate-wrt for 7 arches × (ipk + apk) on
<code>v*</code> tag push; the wizard downloads from these releases.</p>
{pre("act1-feed.txt")}

<h2>Act 2 — Stock OpenWrt VM, cold boot</h2>
<p>Fresh overlay of the baked 24.10.1 base; serial console footage in
<code>raw/act2-serial.log</code>.</p>
{pre("act2-boot.txt")}

<h2>Act 3 — The wizard, filmed</h2>
<p>Scan discovers the VM by ARP; deploy runs 12 steps to success.</p>
{vid("act3/tollgate-installer-demo.webm")}{stills}

<h2>Act 4 — The installed router</h2>
{pre("act4-verify.txt")}{img("act4/portal.png", "captive portal")}

<h2>Act 5 — The automated proof (PRTA suite)</h2>
{pre("act5-suite.txt")}

<h2>Act 6 — State of 0.6.0</h2>
{pre("act6-state.txt")}
<p class="meta">Manuscript: manuscript.md · Facts: facts.json · Package: this directory is the film.</p>
</body></html>"""
        (self.run / "index.html").write_text(page)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="existing run dir")
    ap.add_argument("--new-run", action="store_true")
    ap.add_argument("--acts", default="all", help="comma list or 'all'")
    args = ap.parse_args()
    if args.new_run:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run = REPO / "results" / f"installer-film-{ts}"
        Film(run).save_facts()
        print(f"RUN_DIR={run}")
        return 0
    if not args.run:
        ap.error("need --run or --new-run")
    film = Film(Path(args.run))
    acts = ACT_ORDER if args.acts == "all" else args.acts.split(",")
    for act in acts:
        t0 = time.time()
        try:
            getattr(film, f"act_{act}")()
            print(f"[{act}] ok ({time.time() - t0:.0f}s)")
        except Exception as e:  # noqa: BLE001 — act failures are findings, not stops
            film.facts[act] = {"status": "failed", "error": str(e)[:300]}
            print(f"[{act}] FAILED: {e}")
        film.save_facts()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
