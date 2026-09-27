#!/usr/bin/env python3
"""record-demo.py — synchronized TollGate demo recorder for PRTA.

Runs a tollgate-clientd session while collecting the router's service log
and status-bar snapshots, all stamped on one wall clock, then renders:

  events.json  — the raw synchronized timeline (streams + events + status)
  player.html  — self-contained scrubbable player: laptop terminal pane,
                 router log pane, waybar-style status pill, event markers
  demo.webm    — optional video export of the player (--export-video)

The laptop command is backend-agnostic (--clientd-cmd), so the same
recorder works natively, inside a container, or over SSH lanes. Log
sources: docker:<container> (timestamps parsed from `docker logs -t`),
cmd:<shell command>, file:<path> (both tailed), or none.

Typical, against the module repo's cloud lab from the host (add the host
bridge IP to configs/dhcp.leases first so /usage resolves):

    python3 scripts/record-demo.py \\
        --gateway 127.0.0.1 --gateway-port 2121 \\
        --mac 02:00:00:00:00:aa \\
        --wallet-dir ~/.demo-wallet --steps 1 --renew-below 45s \\
        --duration 40 --log-source docker:tg-upstream \\
        --out evidence/$(date +%F)-clientd-demo --export-video
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "demo-player.html"

EVENT_EMOJI = {"payment": "\N{BLACK DIAMOND}", "failure": "\N{MULTIPLICATION X}",
               "notice": "\N{MIDDLE DOT}"}

DOCKER_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{1,9}Z) ")
ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
COMPOSE_NOISE = re.compile(
    r"^\s*Container \S+ (Running|Healthy|Waiting|Starting|Started|Creating|"
    r"Created|Removed)\s*$")
STATUS_SESSION = re.compile(
    r"^\[TollGate ([^\]]+)\] (no session|\d+:\d{2}(?::\d{2})? left|[\d.]+ [KMG]?B left)"
    r"(?:\s+\((\d+)/(\d+)\))?")
PAID = re.compile(r"^\s*-> (paid .*)$")
FAILED = re.compile(r"^\s*!! (top-up failed:.*)$")
NEEDS_PAYMENT = re.compile(r"no session — needs payment")


def parse_docker_line_ts(line: str) -> float | None:
    """Epoch seconds from a `docker logs --timestamps` line prefix, else None."""
    m = DOCKER_TS.match(line)
    if not m:
        return None
    ts = m.group(1)
    # truncate nanoseconds to microseconds for fromisoformat
    date_part, frac_z = ts.split(".", 1)
    frac = frac_z.rstrip("Z")[:6].ljust(6, "0")
    try:
        dt = datetime.fromisoformat(f"{date_part}.{frac}").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.timestamp()


def strip_docker_ts(line: str) -> str:
    return DOCKER_TS.sub("", line, count=1)


def human_remaining_to_value(text: str) -> float | None:
    """'0:45' -> 45 (s); '1:02:03' -> 3723; '87.3 MB' -> bytes; else None."""
    if "left" in text:
        text = text.replace(" left", "").strip()
    if ":" in text:
        parts = [int(p) for p in text.split(":")]
        if len(parts) > 3 or any(p < 0 for p in parts):
            return None
        while len(parts) < 3:
            parts.insert(0, 0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    m = re.fullmatch(r"([\d.]+) *([KMG]?B)", text)
    if m:
        return float(m.group(1)) * {"B": 1, "KB": 1024, "MB": 1024**2,
                                    "GB": 1024**3}[m.group(2)]
    return None


def parse_status_line(line: str) -> dict | None:
    """Turn a clientd status line into a status-bar snapshot.

    Class follows the daemon's own state: critical = no session, warning =
    actively topping up (or dry-run would), good = healthy allotment."""
    m = STATUS_SESSION.match(line.strip())
    if not m:
        return None
    if m.group(2) == "no session":
        return {"text": "TG no session", "cls": "critical", "pct": 0}
    used, allotment = m.group(3), m.group(4)
    pct = int(100 * (int(allotment) - int(used)) / int(allotment)) \
        if allotment and int(allotment) else 0
    cls = "warning" if ("[topping up" in line or "[dry-run" in line) else "good"
    return {"text": "TG " + m.group(2).replace(" left", ""), "cls": cls, "pct": pct}


def parse_event_line(line: str) -> tuple[str, str] | None:
    """(kind, text) for payment/failure/notice lines, else None."""
    if m := PAID.match(line):
        return "payment", m.group(1)
    if m := FAILED.match(line):
        return "failure", m.group(1)
    if NEEDS_PAYMENT.search(line):
        return "notice", "no session — payment needed"
    return None


def dedupe_events(events: list[dict], gap: float = 5.0) -> list[dict]:
    """Collapse runs of the same event (kind and text) within `gap` seconds.

    Payments/failures carry distinct text (amounts, allotments), so
    threshold renewals landing just past the client's payment throttle
    survive; repeated identical notices ("no session — payment needed"
    every poll) still collapse."""
    out: list[dict] = []
    for e in sorted(events, key=lambda e: e["t"]):
        if (out and out[-1]["kind"] == e["kind"] and out[-1]["text"] == e["text"]
                and e["t"] - out[-1]["t"] < gap):
            continue
        out.append(e)
    return out


class LineCollector(threading.Thread):
    """Stamps every line read from `stream` with its arrival epoch."""

    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream = stream
        self.entries: list[tuple[float, str]] = []
        self._stop = threading.Event()

    def run(self):
        for line in self.stream:
            if self._stop.is_set():
                break
            self.entries.append((time.time(), line.rstrip("\n")))

    def stop(self):
        self._stop.set()


def log_source_command(source: str) -> list[str] | None:
    if source == "none":
        return None
    if source.startswith("docker:"):
        return ["docker", "logs", "-f", "--timestamps", source.split(":", 1)[1]]
    if source.startswith("cmd:"):
        return ["sh", "-c", source.split(":", 1)[1]]
    if source.startswith("file:"):
        return ["tail", "-n", "0", "-f", source.split(":", 1)[1]]
    raise SystemExit(f"unknown --log-source: {source!r}")


def default_clientd_cmd(args) -> str:
    cmd = [sys.executable, str(args.clientd)]
    if args.gateway:
        gw = args.gateway
        if args.gateway_port:
            gw = f"{gw}:{args.gateway_port}"
        cmd += ["--gateway", gw]
    cmd += ["--mac", args.mac]
    if args.wallet_dir:
        cmd += ["--wallet-dir", args.wallet_dir]
    cmd += ["--steps", str(args.steps), "--renew-below", args.renew_below,
            "--interval", str(args.interval)]
    return " ".join(shlex.quote(c) for c in cmd)


def filter_terminal(entries):
    """Drop docker-compose status noise from the terminal stream."""
    return [e for e in entries if not COMPOSE_NOISE.match(e[1])]


def build_manifest(title: str, start_epoch: float, terminal, router, status,
                   events) -> dict:
    def rel(entries):
        return sorted(({"t": round(ts - start_epoch, 3), "line": line}
                       for ts, line in entries), key=lambda e: e["t"])

    duration = max(
        [e["t"] for e in rel(terminal)] + [e["t"] for e in rel(router)]
        + [e["t"] for e in events] + [0.0]) + 0.5
    return {
        "title": title,
        "start_epoch": start_epoch,
        "duration": round(duration, 3),
        "streams": {"terminal": rel(terminal), "router": rel(router)},
        "status": sorted(status, key=lambda s: s["t"]),
        "events": sorted(events, key=lambda e: e["t"]),
    }


def render_player(template: Path, manifest: dict, out: Path) -> Path:
    html = template.read_text()
    for e in manifest["events"]:
        e.setdefault("emoji", EVENT_EMOJI.get(e["kind"], "\N{bullet}"))
    html = html.replace("/*__DATA__*/", json.dumps(manifest))
    html = html.replace("__TITLE__", manifest["title"])
    html = html.replace("__START__",
                        datetime.fromtimestamp(manifest["start_epoch"]).strftime(
                            "%Y-%m-%d %H:%M:%S"))
    out.write_text(html)
    return out


def export_video(player: Path, out_dir: Path, duration: float,
                 speed: float) -> Path | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("python playwright not installed — skipping video export",
              file=sys.stderr)
        return None
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            record_video_dir=str(out_dir), record_video_size={"width": 1280,
                                                              "height": 800})
        page = context.new_page()
        page.goto(f"file://{player.resolve()}?autoplay=1")
        page.wait_for_timeout(int(1000 * (duration / speed + 3)))
        context.close()
        browser.close()
    videos = sorted(out_dir.glob("*.webm"), key=lambda p: p.stat().st_mtime)
    if not videos:
        return None
    final = out_dir / "demo.webm"
    videos[-1].replace(final)
    return final


def run_recording(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    log_cmd = log_source_command(args.log_source)
    log_proc = None
    router_collector = None
    if log_cmd:
        log_proc = subprocess.Popen(log_cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
        router_collector = LineCollector(log_proc.stdout)
        router_collector.start()

    start_epoch = time.time()
    clientd = subprocess.Popen(shlex.split(args.clientd_cmd),
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    term_collector = LineCollector(clientd.stdout)
    term_collector.start()

    status: list[dict] = []
    events: list[dict] = []
    consumed = 0

    def consume_pending():
        nonlocal consumed
        while consumed < len(term_collector.entries):
            ts, line = term_collector.entries[consumed]
            consumed += 1
            if COMPOSE_NOISE.match(line):
                continue
            if (snap := parse_status_line(line)) is not None:
                snap["t"] = round(ts - start_epoch, 3)
                status.append(snap)
            if (ev := parse_event_line(line)) is not None:
                kind, text = ev
                events.append({"t": round(ts - start_epoch, 3),
                               "kind": kind, "text": text})

    def stop():
        if clientd.poll() is None:
            clientd.terminate()
            try:
                clientd.wait(timeout=5)
            except subprocess.TimeoutExpired:
                clientd.kill()
        term_collector.stop()
        if log_proc and log_proc.poll() is None:
            log_proc.terminate()
        if router_collector:
            router_collector.stop()

    try:
        deadline = time.time() + args.duration
        while time.time() < deadline:
            consume_pending()
            time.sleep(0.25)
            if clientd.poll() is not None and consumed == len(term_collector.entries):
                break
    except KeyboardInterrupt:
        pass
    finally:
        time.sleep(0.5)  # let final lines land
        stop()
        consume_pending()

    status = dedupe_status(status)
    events = dedupe_events(events)

    router_entries = []
    if router_collector:
        for ts, line in router_collector.entries:
            parsed = parse_docker_line_ts(line)
            line = ANSI.sub("", strip_docker_ts(line))
            router_entries.append((parsed if parsed is not None else ts, line))
        router_entries = [(ts, line) for ts, line in router_entries
                          if ts >= start_epoch - 1]

    manifest = build_manifest(args.title, start_epoch,
                              filter_terminal(term_collector.entries),
                              router_entries, status, events)
    (out / "events.json").write_text(json.dumps(manifest, indent=1))

    player = render_player(TEMPLATE, manifest, out / "player.html")

    video = None
    if args.export_video:
        video = export_video(player, out, manifest["duration"], args.export_speed)

    payments = sum(1 for e in events if e["kind"] == "payment")
    print(f"recorded {len(manifest['streams']['terminal'])} client lines, "
          f"{len(manifest['streams']['router'])} router lines, "
          f"{payments} payment(s) over {manifest['duration']:.1f}s")
    print(f"player: {player}")
    if video:
        print(f"video:  {video}")
    return 0


def dedupe_status(status: list[dict]) -> list[dict]:
    """Collapse consecutive identical snapshots."""
    out: list[dict] = []
    for s in status:
        if out and out[-1]["text"] == s["text"] and out[-1]["cls"] == s["cls"]:
            continue
        out.append(s)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="record-demo",
        description="Record a synchronized TollGate demo: clientd terminal + "
                    "router log + status bar, rendered as a scrubbable player.")
    ap.add_argument("--clientd", default=str(ROOT / "scripts" / "tollgate-clientd.py"))
    ap.add_argument("--clientd-cmd",
                    help="full command to run clientd (overrides --clientd/gateway/...)")
    ap.add_argument("--gateway")
    ap.add_argument("--gateway-port", type=int)
    ap.add_argument("--mac", default="02:00:00:00:00:20")
    ap.add_argument("--wallet-dir")
    ap.add_argument("--steps", type=int, default=1)
    ap.add_argument("--renew-below", default="45s")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--log-source", default="docker:tg-upstream",
                    help="docker:NAME | cmd:SH | file:PATH | none")
    ap.add_argument("--duration", type=float, default=40.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="TollGate demo — laptop lane")
    ap.add_argument("--export-video", action="store_true")
    ap.add_argument("--export-speed", type=float, default=2.0)
    args = ap.parse_args()

    if not args.clientd_cmd:
        if not args.gateway:
            ap.error("--gateway or --clientd-cmd required")
        args.clientd_cmd = default_clientd_cmd(args)

    return run_recording(args)


if __name__ == "__main__":
    sys.exit(main())
