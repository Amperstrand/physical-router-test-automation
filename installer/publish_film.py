#!/usr/bin/env python3
"""Publish an installer film run directory to Blossom + Nostr.

Uploads the reel's footage to blossom.psbt.me (NUT-24 auto-pay from the
testnut fakewallet if the >1MB video hits the live free tier), builds a
standalone viewer page with absolute URLs, then emits kind 1063 (video
file event) and kind 30078 (run index, what the tollgate dashboard reads).

Usage (system python3 — nostr_publish is installed there):
  python3 installer/publish_film.py --run results/installer-film-<ts> [--nsec ~/.config/prta/nsec]
"""
from __future__ import annotations

import argparse
import html
import json
import subprocess
from pathlib import Path

from nostr_publish.blossom import compute_sha256
from nostr_publish.publisher import publish_nip94_event, publish_test_run_event


def nak_upload(path: Path, server: str, nsec_hex: str) -> str:
    """Upload via nak (strict BUD-11) — works on both psbt.me and primal,
    and the server records the content type from the extension."""
    r = subprocess.run(
        ["nak", "blossom", "upload", "--server", server, "--sec", nsec_hex, str(path)],
        capture_output=True, text=True, timeout=180,
    )
    if r.returncode != 0:
        raise RuntimeError(f"nak upload failed: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout.strip().splitlines()[-1])["url"]

ARTIFACTS = [
    ("webm", "raw/act3/tollgate-installer-demo.webm"),
    ("still_scan", "raw/act3/01-scan-results.png"),
    ("still_form", "raw/act3/02-form-filled.png"),
    ("still_steps", "raw/act3/03-deploy-steps.png"),
    ("still_success", "raw/act3/04-success.png"),
    ("still_portal", "raw/act4/portal.png"),
    ("txt_feed", "raw/act1-feed.txt"),
    ("txt_boot", "raw/act2-boot.txt"),
    ("txt_serial", "raw/act2-serial.log"),
    ("txt_verify", "raw/act4-verify.txt"),
    ("txt_suite", "raw/act5-suite.txt"),
    ("txt_state", "raw/act6-state.txt"),
]

TEXT_TITLES = {
    "txt_feed": "Act 1 — Where the package comes from (feed)",
    "txt_boot": "Act 2 — Stock VM evidence",
    "txt_serial": "Act 2 — Serial console log",
    "txt_verify": "Act 4 — Installed router verification",
    "txt_suite": "Act 5 — PRTA suite run",
    "txt_state": "Act 6 — State of 0.6.0",
}


def build_viewer(run: Path, urls: dict, facts: dict) -> bytes:
    feed, suite, wiz = facts.get("feed", {}), facts.get("suite", {}), facts.get("wizard", {})
    cards = [
        ("ok", f"Feed: {feed.get('assets', '?')} assets / {feed.get('arches', '?')} arches @ {feed.get('latest_tag', '?')}"),
        ("ok", f"Suite: {suite.get('passed', '?')}/3 green"),
        ("ok", f"Wizard: {wiz.get('status', '?')}"),
        ("gap", f"{feed.get('assets_without_digest', '?')} assets without digest"),
        ("gap", f"pin {feed.get('installer_pin', '?')} vs feed {feed.get('latest_tag', '?')}"),
    ]
    card_html = "".join(f'<span class="card {c}">{html.escape(t)}</span>' for c, t in cards)
    stills = "".join(
        f'<img src="{urls[k]}" alt="{k}">'
        for k in ("still_scan", "still_form", "still_steps", "still_success", "still_portal")
        if k in urls
    )
    panels = ""
    for key, title in TEXT_TITLES.items():
        if key not in urls:
            continue
        p = run / dict((k, rel) for k, rel in ARTIFACTS)[key]
        body = html.escape(p.read_text(errors="replace")[:8000])
        panels += (f"<details><summary>{html.escape(title)}</summary>"
                   f'<pre class="footage">{body}</pre></details>')
    state_txt = ""
    p = run / "raw" / "act6-state.txt"
    if p.exists():
        state_txt = f'<pre class="footage state">{html.escape(p.read_text(errors="replace"))}</pre>'
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TollGate 0.6.0 — from stock OpenWrt to an installed router</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#1a1a2e;background:#fafafa}}
h1{{font-size:1.6rem}} video,img{{max-width:100%;border:1px solid #ddd;border-radius:6px;margin:.4rem .4rem .4rem 0}}
.footage{{background:#0d1117;color:#c9d1d9;padding:1rem;border-radius:6px;overflow:auto;max-height:24rem;font-size:.8rem}}
details{{margin:.8rem 0}} summary{{cursor:pointer;font-weight:600}}
.meta{{color:#666}} .card{{display:inline-block;padding:.6rem 1.2rem;margin:.3rem;border-radius:8px;font-weight:600}}
.ok{{background:#dcfce7}} .gap{{background:#fee2e2}} .state{{max-height:32rem}}
</style></head><body>
<h1>TollGate 0.6.0 — from stock OpenWrt to an installed router</h1>
<p class="meta">Audience: TollGate developers · filmed on a local QEMU lab ·
upstream repos read-only (no tags, no merges, no pushes) · artifacts on Blossom,
indexed via Nostr kind 30078</p>
<p>{card_html}</p>
<h2>The wizard, filmed</h2>
<video controls preload="metadata" src="{urls.get('webm', '')}"></video>
{stills}
<h2>State of the release</h2>
{state_txt}
<h2>Evidence</h2>
{panels}
<p class="meta">Run: {html.escape(run.name)} · filmed with PRTA installer/make_film.py</p>
</body></html>"""
    return page.encode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--nsec", default=str(Path.home() / ".config" / "prta" / "nsec"))
    ap.add_argument("--blossom", default="https://blossom.primal.net",
                    help="blossom server for uploads (primal serves content types)")
    args = ap.parse_args()
    run = Path(args.run).resolve()
    facts = json.loads((run / "facts.json").read_text())
    nsec_hex = Path(args.nsec).read_text().strip()

    urls: dict[str, str] = {}
    for key, rel in ARTIFACTS:
        path = run / rel
        if not path.exists():
            print(f"[skip] {rel} (missing)")
            continue
        try:
            urls[key] = nak_upload(path, args.blossom, nsec_hex)
            print(f"[ok] {key}: {urls[key]}")
        except Exception as e:  # noqa: BLE001 — one artifact failing is a finding
            print(f"[FAIL] {key}: {e}")
    if "webm" not in urls:
        print("FATAL: webm did not upload — aborting before events")
        return 1

    viewer_path = run / "film-online.html"
    viewer_path.write_bytes(build_viewer(run, urls, facts))
    urls["viewer"] = nak_upload(viewer_path, args.blossom, nsec_hex)
    print(f"[ok] viewer: {urls['viewer']}")

    webm_path = run / "raw" / "act3" / "tollgate-installer-demo.webm"
    sha = compute_sha256(str(webm_path))
    ev94 = publish_nip94_event(
        args.nsec, "tollgate-installer-demo.webm", urls["webm"], sha, "video/webm",
        metadata_tags={"project": "tollgate", "type": "demo-film", "release": "0.6.0"})
    summary = ("TollGate 0.6.0 demo film — stock OpenWrt VM → installer wizard → "
               "FreedomTechFeed package installed → state of the release "
               f"({facts.get('state', {}).get('works', '?')} works / "
               f"{facts.get('state', {}).get('gaps', '?')} gaps). Viewer page first.")
    ev78 = publish_test_run_event(
        args.nsec, run_id=f"installer-film-{run.name.split('-', 2)[-1]}",
        file_urls=[urls["viewer"], urls["webm"]] + [u for k, u in urls.items()
                                                    if k.startswith(("still", "txt"))],
        summary=summary, project_tag="tollgate",
        extra_tags=[["t", "demo-film"], ["release", "0.6.0"],
                    ["tool", "tollgate-installer"], ["feed", "FreedomTechFeed/packages"]])
    print("\nPUBLISHED")
    print(f"VIEWER={urls['viewer']}")
    print(f"VIDEO={urls['webm']}")
    print(f"NIP94={ev94}")
    print(f"RUN_EVENT={ev78}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
