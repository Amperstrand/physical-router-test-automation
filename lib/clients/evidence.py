"""Evidence capture for phone E2E runs: screenrecord video + step
screenshots + machine-readable manifest for AI-vision validation.

Owner evidence plan: during the payment run, record video
(``screenrecord --time-limit 120``) from just before the wifi join until
internet is verified, take a screenshot at each key step (wifi connected,
portal detected, portal page, payment submitted, internet unlocked), and
afterwards validate every capture against its claim with the zai-vision
MCP, writing verdicts next to the artifacts.

Device notes (pinned on ZY326DPC7R):
- ``screenrecord`` finalizes the mp4 cleanly on SIGINT; blind kill leaves
  an unplayable file, so stop_video() always uses ``pkill -INT``.
- screenrecord caps at 180 s per file; the plan's 120 s fits.
- The recorder is spawned as a host-side ``adb shell`` subprocess (not via
  the u2 RPC) so its lifecycle is independent of UI automation calls.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time

log = logging.getLogger("tollgate.evidence")

VIDEO_DEVICE_PATH = "/sdcard/tollgate-e2e.mp4"


class EvidenceRecorder:
    def __init__(self, d, serial: str, artifacts_dir: str):
        self.d = d
        self.serial = serial
        self.artifacts_dir = artifacts_dir
        os.makedirs(artifacts_dir, exist_ok=True)
        self.steps: list[dict] = []
        self._video_proc = None
        self._video_limit = 0

    # -- video -----------------------------------------------------------

    def start_video(self, limit_s: int = 120) -> bool:
        self.stop_video(pull=False)
        self.d.shell(f"rm -f {VIDEO_DEVICE_PATH}")
        cmd = ["adb", "-s", self.serial, "shell",
               "screenrecord", "--time-limit", str(limit_s), VIDEO_DEVICE_PATH]
        self._video_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._video_limit = limit_s
        log.info("screenrecord started (pid %d, %ds cap)",
                 self._video_proc.pid, limit_s)
        return True

    def stop_video(self, pull: bool = True) -> str | None:
        if self._video_proc is not None:
            # SIGINT makes screenrecord finalize the container properly.
            self.d.shell("pkill -INT screenrecord")
            try:
                self._video_proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._video_proc.kill()
            self._video_proc = None
            time.sleep(1)
        if not pull:
            return None
        local = os.path.join(self.artifacts_dir, "tollgate-e2e.mp4")
        r = subprocess.run(
            ["adb", "-s", self.serial, "pull", VIDEO_DEVICE_PATH, local],
            capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and os.path.isfile(local):
            size = os.path.getsize(local)
            if size > 1024:
                log.info("video pulled: %s (%d bytes)", local, size)
                return local
            os.remove(local)  # unplayable stub from a blind kill
            log.warning("video file too small (%d bytes) — discarded", size)
        return None

    # -- step screenshots --------------------------------------------------

    def shot(self, step: str, claim: str) -> str | None:
        path = os.path.join(self.artifacts_dir, f"{step}.png")
        try:
            self.d.screenshot(path)
        except Exception as exc:
            log.warning("screenshot %s failed: %s", step, exc)
            return None
        self.steps.append({
            "step": step,
            "file": path,
            "claim": claim,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        log.info("evidence shot: %s (%s)", step, claim)
        return path

    # -- artifacts ---------------------------------------------------------

    def write_manifest(self) -> str:
        path = os.path.join(self.artifacts_dir, "evidence-steps.json")
        with open(path, "w") as f:
            json.dump({
                "video": os.path.join(self.artifacts_dir, "tollgate-e2e.mp4"),
                "video_limit_s": self._video_limit,
                "steps": self.steps,
            }, f, indent=2)
        return path

    def write_verdicts(self, verdicts: list[dict]) -> str:
        path = os.path.join(self.artifacts_dir, "evidence-verdicts.json")
        with open(path, "w") as f:
            json.dump(verdicts, f, indent=2)
        return path
