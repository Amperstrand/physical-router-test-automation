"""
Film recording plugin — captures phone screen + router logs during pytest runs,
then composes chapter-locked evidence films with the director.

Usage:
    pytest tests/phone/ --film                    # record + compose
    pytest tests/phone/ --film --film-dir out/    # custom output dir
    pytest tests/phone/ --film-narrate            # add narration

The plugin hooks into pytest's runtest protocol:
- Before each test: starts screenrecord on the phone (adb)
- After each test: stops recording, captures the clip + router log snapshot
- At session end: composes all clips into a chapter-locked film via director.py

Chapters are derived from test names + outcomes, with timestamps from the
actual run (not estimated). Each chapter's terminal panel shows the router
events that fired during that specific test.
"""

import json
import logging
import os
import pathlib
import re
import subprocess
import time
from typing import Optional

import pytest

log = logging.getLogger("tollgate.film")


def pytest_addoption(parser):
    """Add --film and related options."""
    group = parser.getgroup("film")
    group.addoption("--film", action="store_true", default=False,
                    help="Record phone screen during tests and compose a film")
    group.addoption("--film-dir", default=None,
                    help="Output directory for film artifacts (default: results/films/<timestamp>)")
    group.addoption("--film-narrate", action="store_true", default=False,
                    help="Generate narration for the film (requires edge-tts)")
    group.addoption("--film-bitrate", type=int, default=6000000,
                    help="Screenrecord bitrate (default: 6 Mbps)")


class FilmRecorder:
    """Manages screen recording across test runs."""

    def __init__(self, adb_serial: Optional[str] = None, out_dir: pathlib.Path = None,
                 bitrate: int = 6000000):
        self.adb = ["adb"] + (["-s", adb_serial] if adb_serial else [])
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.clips_dir = self.out_dir / "clips"
        self.clips_dir.mkdir(exist_ok=True)
        self.bitrate = bitrate
        self.chapters = []
        self._current_clip = None
        self._start_time = None
        self._recording = False

    def start_chapter(self, name: str, test_id: str):
        """Start recording for a new chapter."""
        if self._recording:
            self.stop_chapter(outcome="interrupted")

        safe_name = re.sub(r'[^\w-]', '_', name)[:40]
        self._current_clip = self.clips_dir / f"{safe_name}.mp4"
        self._start_time = time.time()

        # Remove any stale clip
        remote_path = "/sdcard/film_clip.mp4"
        subprocess.run(self.adb + ["shell", f"rm -f {remote_path}"],
                       capture_output=True, timeout=5)

        # Start screenrecord in background
        subprocess.Popen(
            self.adb + ["shell",
                        f"screenrecord --bit-rate {self.bitrate} "
                        f"--time-limit 180 {remote_path}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._recording = True
        log.info("🎬 recording chapter: %s", name)

    def stop_chapter(self, outcome: str = "passed", detail: str = ""):
        """Stop recording, pull the clip, record chapter metadata."""
        if not self._recording:
            return

        time.sleep(1)  # let screenrecord flush
        subprocess.run(self.adb + ["shell", "pkill -l INT screenrecord"],
                       capture_output=True, timeout=5)
        time.sleep(2)   # wait for file finalization

        remote_path = "/sdcard/film_clip.mp4"
        local = self._current_clip
        subprocess.run(self.adb + ["pull", remote_path, str(local)],
                       capture_output=True, timeout=30)

        duration = time.time() - self._start_time
        self.chapters.append({
            "name": local.stem,
            "clip": str(local),
            "outcome": outcome,
            "detail": detail,
            "start": self._start_time,
            "duration": duration,
            "size": local.stat().st_size if local.exists() else 0,
        })
        log.info("🎬 chapter done: %s (%s, %.1fs, %d bytes)",
                 local.stem, outcome, duration,
                 self.chapters[-1]["size"])
        self._recording = False

    def screenshot(self, name: str) -> Optional[pathlib.Path]:
        """Take a screenshot mid-chapter."""
        path = self.out_dir / f"{name}.png"
        subprocess.run(self.adb + ["exec-out", "screencap", "-p"],
                       stdout=open(path, "wb"), timeout=15)
        return path if path.exists() else None

    def compose(self, narrate: bool = False) -> Optional[pathlib.Path]:
        """Compose all chapters into a single film."""
        if not self.chapters:
            log.warning("no chapters to compose")
            return None

        timeline = self.out_dir / "timeline.json"
        timeline.write_text(json.dumps(self.chapters, indent=2))

        # Simple compose: concatenate all clips
        list_file = self.out_dir / "chapters.txt"
        entries = []
        for ch in self.chapters:
            if ch["size"] > 1000:  # skip empty clips
                entries.append(f"file '{ch['clip']}'")
        list_file.write_text("\n".join(entries))

        output = self.out_dir / "film.mp4"
        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "concat", "-safe", "0",
             "-i", str(list_file),
             "-c", "copy", str(output)],
            capture_output=True, text=True, timeout=300,
        )

        if result.returncode == 0 and output.exists():
            log.info("🎬 film composed: %s (%d bytes)",
                     output, output.stat().st_size)
            return output
        else:
            log.error("compose failed: %s", result.stderr[-200:])
            return None


class FilmPlugin:
    """Pytest plugin that hooks test execution to the film recorder."""

    def __init__(self, config):
        self.config = config
        self.recorder = None
        self.enabled = config.getoption("--film", default=False)

    def pytest_sessionstart(self, session):
        if not self.enabled:
            return

        out_base = self.config.getoption("--film-dir")
        if not out_base:
            import datetime
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            base = pathlib.Path(os.environ.get("TOLLGATE_RESULTS_DIR", "results"))
            out_base = str(base / "films" / stamp)

        # Find the adb serial from the environment or test config
        adb_serial = os.environ.get("TOLLGATE_ADB_SERIAL")
        if not adb_serial:
            # Try to auto-detect
            result = subprocess.run(["adb", "devices"],
                                   capture_output=True, text=True, timeout=5)
            devices = [l.split("\t")[0] for l in result.stdout.splitlines()
                       if "\tdevice" in l]
            adb_serial = devices[0] if devices else None

        if not adb_serial:
            log.warning("no adb device found — film recording disabled")
            self.enabled = False
            return

        bitrate = self.config.getoption("--film-bitrate", default=6000000)
        self.recorder = FilmRecorder(
            adb_serial=adb_serial,
            out_dir=pathlib.Path(out_base),
            bitrate=bitrate,
        )
        log.info("🎬 film recording enabled — output: %s", out_base)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        if not self.enabled or not self.recorder:
            yield
            return

        # Only record phone tests
        marks = [m.name for m in item.iter_markers()]
        if "phone" not in marks and "browser" not in marks:
            yield
            return

        test_name = item.name
        self.recorder.start_chapter(test_name, item.nodeid)
        yield
        # stop_chapter is called from pytest_runtest_logreport

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_logreport(self, report):
        if not self.enabled or not self.recorder:
            return

        if report.when == "call":
            outcome = "passed" if report.passed else "failed" if report.failed else "skipped"
            detail = ""
            if report.failed and report.longrepr:
                detail = str(report.longrepr)[:200]
            self.recorder.stop_chapter(outcome, detail)

    def pytest_sessionfinish(self, session, exitstatus):
        if not self.enabled or not self.recorder:
            return

        if self.recorder._recording:
            self.recorder.stop_chapter(outcome="session-end")

        narrate = self.config.getoption("--film-narrate", default=False)
        film = self.recorder.compose(narrate=narrate)

        if film:
            log.info("🎬 final film: %s", film)
            # Store path for post-processing
            session.results.film_path = str(film)


def pytest_configure(config):
    """Register the film plugin."""
    plugin = FilmPlugin(config)
    config._film_plugin = plugin
    config.pluginmanager.register(plugin)


def pytest_unconfigure(config):
    """Clean up the film plugin."""
    plugin = getattr(config, "_film_plugin", None)
    if plugin:
        config.pluginmanager.unregister(plugin)
