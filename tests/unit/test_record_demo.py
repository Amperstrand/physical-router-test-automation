"""Unit tests for scripts/record-demo.py — the synchronized demo recorder.

These run with no TollGate, no docker, no wallet: they pin the parsers
(docker timestamps, status lines, payment/failure events), the event
de-duper, manifest building, and player rendering.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "record-demo.py"
spec = importlib.util.spec_from_file_location("record_demo", SCRIPT)
rd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rd)

TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "demo-player.html"


class TestDockerTimestamps:
    def test_parses_rfc3339_nano(self):
        a = rd.parse_docker_line_ts("2026-09-19T16:20:01.123456789Z x")
        b = rd.parse_docker_line_ts("2026-09-19T16:20:02.124456789Z y")
        assert a is not None and b is not None
        assert abs((b - a) - 1.001) < 0.002
        assert a > 1.7e9  # epoch scale, not a naive datetime

    def test_none_without_prefix(self):
        assert rd.parse_docker_line_ts("plain log line") is None

    def test_strip_prefix(self):
        line = "2026-09-19T16:20:01.123456789Z hello"
        assert rd.strip_docker_ts(line) == "hello"


class TestStatusParsing:
    def test_no_session(self):
        snap = rd.parse_status_line(
            "[TollGate upstream] no session — needs payment  [topping up...]")
        assert snap == {"text": "TG no session", "cls": "critical", "pct": 0}

    def test_time_remaining(self):
        snap = rd.parse_status_line("[TollGate upstream] 0:45 left (15000/60000)")
        assert snap["text"] == "TG 0:45"
        assert snap["pct"] == 75
        assert snap["cls"] == "good"

    def test_topping_up_is_warning(self):
        snap = rd.parse_status_line(
            "[TollGate upstream] 0:45 left (15000/60000)  [topping up...]")
        assert snap["cls"] == "warning"

    def test_dry_run_marker_is_warning(self):
        snap = rd.parse_status_line(
            "[TollGate upstream] no session — needs payment  [dry-run: would top up]")
        assert snap["cls"] == "critical"  # still no session

    def test_bytes_remaining(self):
        snap = rd.parse_status_line("[TollGate 192.168.8.1] 87.3 MB left (1/2)")
        assert snap["text"] == "TG 87.3 MB"
        assert snap["cls"] == "good"  # 50% > 40

    def test_non_status_line(self):
        assert rd.parse_status_line("  -> paid 1 sats (allotment now 60000)") is None


class TestEventParsing:
    def test_paid(self):
        assert rd.parse_event_line("  -> paid 1 sats (allotment now 60000)") == (
            "payment", "paid 1 sats (allotment now 60000)")

    def test_failed(self):
        kind, text = rd.parse_event_line(
            "  !! top-up failed: cdk-cli send failed: insufficient")
        assert kind == "failure"
        assert "insufficient" in text

    def test_needs_payment(self):
        assert rd.parse_event_line(
            "[TollGate upstream] no session — needs payment") == (
            "notice", "no session — payment needed")

    def test_plain_line(self):
        assert rd.parse_event_line("[TollGate upstream] 0:59 left (1000/60000)") is None


class TestHumanRemaining:
    def test_clock_formats(self):
        assert rd.human_remaining_to_value("0:45") == 45
        assert rd.human_remaining_to_value("1:02:03") == 3723

    def test_bytes(self):
        assert rd.human_remaining_to_value("87.3 MB") == 87.3 * 1024**2
        assert rd.human_remaining_to_value("512 KB") == 512 * 1024

    def test_garbage(self):
        assert rd.human_remaining_to_value("no session") is None


class TestDedupe:
    def test_collapses_same_kind_within_gap(self):
        events = [{"t": 0.0, "kind": "notice", "text": "a"},
                  {"t": 1.0, "kind": "notice", "text": "a"},
                  {"t": 9.0, "kind": "notice", "text": "a"}]
        out = rd.dedupe_events(events, gap=5.0)
        assert [e["t"] for e in out] == [0.0, 9.0]

    def test_keeps_renewal_payment_just_past_throttle(self):
        # payments land ~5s apart (client throttle == dedupe gap); distinct text must survive
        events = [{"t": 1.4, "kind": "payment", "text": "paid 1 sats (allotment now 22020096)"},
                  {"t": 6.3, "kind": "payment", "text": "paid 1 sats (allotment now 44040192)"}]
        assert len(rd.dedupe_events(events)) == 2

    def test_keeps_alternating_kinds(self):
        events = [{"t": 0.0, "kind": "notice", "text": "a"},
                  {"t": 0.5, "kind": "payment", "text": "b"},
                  {"t": 1.0, "kind": "notice", "text": "c"}]
        assert len(rd.dedupe_events(events)) == 3


class TestManifestAndPlayer:
    def _manifest(self):
        terminal = [(1000.0, "TollGate upstream | milliseconds |"),
                    (1001.0, "[TollGate upstream] no session — needs payment"),
                    (1002.0, "  -> paid 1 sats (allotment now 60000)"),
                    (1003.0, "[TollGate upstream] 0:59 left (1000/60000)")]
        router = [(1000.2, "INFO payment: swap ok"),
                  (1002.1, "INFO session: opened mac=02:00:00:00:00:aa")]
        status = [{"t": 3.0, "text": "TG 0:59", "cls": "good", "pct": 98}]
        events = [{"t": 2.0, "kind": "payment", "text": "paid 1 sats"}]
        return rd.build_manifest("demo", 1000.0, terminal, router, status, events)

    def test_manifest_shape_and_relative_times(self):
        m = self._manifest()
        assert m["start_epoch"] == 1000.0
        assert m["streams"]["terminal"][0] == {"t": 0.0,
                                               "line": "TollGate upstream | milliseconds |"}
        assert m["duration"] >= 3.0
        assert m["events"][0]["t"] == 2.0

    def test_render_player_is_self_contained(self, tmp_path):
        m = self._manifest()
        out = rd.render_player(TEMPLATE, m, tmp_path / "player.html")
        html = out.read_text()
        assert "__DATA__" not in html and "__TITLE__" not in html
        assert "<title>demo</title>" in html
        payload = html.split("const DATA = ", 1)[1].split(";\n", 1)[0]
        parsed = json.loads(payload)
        assert parsed["events"][0]["kind"] == "payment"
        assert parsed["events"][0]["emoji"]  # injected during render


class TestLogSourceCommand:
    def test_docker(self):
        assert rd.log_source_command("docker:tg-upstream") == [
            "docker", "logs", "-f", "--timestamps", "tg-upstream"]

    def test_cmd_and_file(self):
        assert rd.log_source_command("cmd:ssh r logread -f")[0:3] == [
            "sh", "-c", "ssh r logread -f"]
        assert rd.log_source_command("file:/tmp/x.log")[0] == "tail"

    def test_none(self):
        assert rd.log_source_command("none") is None

    def test_unknown(self):
        try:
            rd.log_source_command("bogus:x")
            raised = False
        except SystemExit:
            raised = True
        assert raised


class TestTerminalNoiseFilter:
    def test_compose_status_lines_dropped(self):
        entries = [(1.0, " Container prta-mint Running "),
                   (1.1, " Container cloud-lab-client-run-abc Created "),
                   (1.2, "[TollGate upstream] no session — needs payment"),
                   (1.3, "  -> paid 1 sats (allotment now 60000)")]
        kept = rd.filter_terminal(entries)
        assert kept == entries[2:]


def test_module_importable_via_dash_name():
    # sanity for the importlib load used by other lanes
    assert hasattr(rd, "run_recording")
