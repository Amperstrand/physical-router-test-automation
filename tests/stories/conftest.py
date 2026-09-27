"""Shared fixtures for user-story tests.

Provides the device-agnostic ClientDevice abstraction backed by labgrid.
Each test parameterizes over device_place names; the corresponding
adapter is instantiated from the labgrid target.

Also provides SSID auto-resolution (queries the router for the actual
SSID matching the TollGate- prefix) and evidence recording (video +
step screenshots with vision validation).
"""
from __future__ import annotations

import logging
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Protocol

import pytest

try:
    from PIL import Image
except ImportError:  # size-heuristic fallback below
    Image = None

from lib.clients.ssid import get_router_host, resolve_ssid

log = logging.getLogger("tollgate.stories")


class ClientDevice(Protocol):
    """Any device that can act as a captive-portal client."""

    name: str

    def join_wifi(self, ssid: str, psk: str = "") -> bool: ...
    def get_ip(self) -> str: ...
    def has_internet(self, host: str = "8.8.8.8") -> bool: ...
    def screenshot(self, path: str) -> bool: ...
    def submit_token(self, token: str) -> bool: ...
    def portal_detected(self) -> bool: ...


class ADBClientDevice:
    """Android phone or emulator via ADB."""

    def __init__(self, serial: str, name: str = "android"):
        self.serial = serial
        self.name = name
        self._base = ["adb", "-s", serial] if serial else ["adb"]

    def _shell(self, cmd: str, timeout: int = 15) -> str:
        r = subprocess.run(
            self._base + ["shell", cmd],
            capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()

    def join_wifi(self, ssid: str, psk: str = "") -> bool:
        security = "wpa2" if psk else "open"
        cmd = f"cmd wifi connect-network {ssid} {security}"
        if psk:
            cmd += f" {psk}"
        self._shell(cmd, timeout=30)
        for _ in range(15):
            time.sleep(2)
            info = self._shell("dumpsys wifi | grep mWifiInfo")
            if ssid in info and "ip" in info.lower() and "/192" in info:
                return True  # connected AND has an IP
            if ssid in info:
                # Connected but no IP yet — keep waiting for DHCP
                continue
        return ssid in self._shell("dumpsys wifi | grep mWifiInfo")

    def get_ip(self) -> str:
        ip = self._shell(
            "ip addr show wlan0 | grep 'inet ' | awk '{print $2}' | cut -d/ -f1")
        if not ip:
            ip = self._shell(
                "dumpsys wifi | grep mWifiInfo | grep -o 'IP: /[0-9.]*' | cut -d/ -f2")
        return ip

    def has_internet(self, host: str = "8.8.8.8") -> bool:
        return "1 received" in self._shell(f"ping -c1 -W3 {host}")

    def screenshot(self, path: str) -> bool:
        # exec-out streaming: on-device /sdcard can be unwritable after a
        # wedged reboot (FUSE storage lock) — this path needs no device
        # storage at all.
        r = subprocess.run(
            self._base + ["exec-out", "screencap", "-p"],
            capture_output=True, timeout=15)
        if r.returncode != 0 or not r.stdout.startswith(b"\x89PNG"):
            return False
        with open(path, "wb") as f:
            f.write(r.stdout)
        return True

    def submit_token(self, token: str) -> bool:
        gateway = self._shell(
            "ip route show table all | grep 'default via' | awk '{print $3}' | head -1")
        if not gateway:
            gateway = "192.168.1.1"
        out = self._shell(
            f"curl -s -m 20 -X POST -H 'Content-Type: text/plain' "
            f"-d '{token}' http://{gateway}:2121/")
        return "1022" in out

    def portal_detected(self) -> bool:
        # Not behind a portal if we can reach the internet
        if self.has_internet():
            return False
        # On WiFi without internet = behind a captive portal
        wifi = self._shell("dumpsys wifi | grep -c 'mWifiInfo SSID: \"TollGate'")
        return wifi.strip() != "0"

    def state_text(self) -> str:
        """Non-visual device truth: association, OS validation verdict,
        and the unlock/storage state that gates screen evidence.

        The 2026-09-27 phone wall (black captures + unwritable /sdcard)
        was FallbackHome limbo: user 0 never unlocked after reboot. A
        resumed activity of com.android.settings/.FallbackHome or a
        locked user_storage line in this sidecar names that state
        instantly on every future run.
        """
        wifi = self._shell("dumpsys wifi | grep mWifiInfo | head -1")
        agent = ""
        out = self._shell("dumpsys connectivity", timeout=20)
        for line in out.splitlines():
            if "NetworkAgentInfo{" in line and "ni{WIFI" in line:
                agent = line
                break
        if "VALIDATED" in agent and "CAPTIVE_PORTAL" not in agent:
            verdict = "VALIDATED"
        elif "CAPTIVE_PORTAL" in agent:
            verdict = "CAPTIVE_PORTAL"
        else:
            verdict = "unknown"
        resumed = self._shell(
            "dumpsys activity activities 2>/dev/null "
            "| grep -m1 ResumedActivity")
        storage = "writable" if "ok" in self._shell(
            "touch /sdcard/.tg-probe && rm -f /sdcard/.tg-probe && echo ok"
        ) else "locked"
        lockscreen = self._shell("locksettings get-disabled")
        return "\n".join([
            f"validation: {verdict}",
            f"user_storage: {storage}",
            f"lockscreen_disabled: {lockscreen}",
            f"resumed: {resumed}",
            f"wifi: {wifi}",
            f"agent: {agent[:400]}",
            "",
        ])

    def os_validated(self) -> bool:
        """True when the active WIFI network agent carries the Android
        VALIDATED capability and no CAPTIVE_PORTAL verdict.

        Calibrated on the bench phone (Android 15, moto g(7)): the active
        agent is a single ``NetworkAgentInfo{... ni{WIFI CONNECTED ...
        nc{[ Transports: WIFI Capabilities: ...]}}`` dumpsys line; while a
        portal verdict is cached the caps contain CAPTIVE_PORTAL, after a
        successful re-probe they contain VALIDATED.
        """
        out = self._shell("dumpsys connectivity", timeout=20)
        for line in out.splitlines():
            if "NetworkAgentInfo{" in line and "ni{WIFI" in line:
                return "VALIDATED" in line and "CAPTIVE_PORTAL" not in line
        return False

    def open_url(self, url: str) -> bool:
        """Open a URL in the default browser (human-truth internet probe)."""
        out = self._shell(
            f"am start -W -a android.intent.action.VIEW -d '{url}'",
            timeout=20)
        return "Status: ok" in out

    def wifi_cycle(self, ssid: str, wait: int = 40) -> bool:
        """Toggle wifi off/on and verify reassociation to ``ssid``.

        Forces Android's NetworkMonitor to re-probe the fresh connection —
        the deterministic nudge when the OS holds a stale captive-portal
        verdict (probe backoff, NR7101 runbook §9.5). Falls back to an
        explicit connect-network because the selector may refuse to
        auto-rejoin a churned SSID.
        """
        self._shell("svc wifi disable")
        time.sleep(3)
        self._shell("svc wifi enable")
        for _ in range(wait // 2):
            time.sleep(2)
            info = self._shell("dumpsys wifi | grep mWifiInfo")
            if ssid in info and "/192" in info:
                return True
        self._shell(f"cmd wifi connect-network {ssid} open", timeout=30)
        for _ in range(10):
            time.sleep(2)
            info = self._shell("dumpsys wifi | grep mWifiInfo")
            if ssid in info and "/192" in info:
                return True
        return False


class SSHClientDevice:
    """Linux VM via SSH (Debian, omarchy, etc.)."""

    def __init__(self, host: str, user: str = "root",
                 password: str = "", name: str = "linux"):
        self.host = host
        self.user = user
        self._password = password
        self.name = name

    def _ssh(self, cmd: str, timeout: int = 15) -> str:
        base = ["ssh", "-o", "ConnectTimeout=5",
                "-o", "StrictHostKeyChecking=no",
                f"{self.user}@{self.host}", cmd]
        if self._password:
            base = ["sshpass", "-p", self._password] + base
        r = subprocess.run(base, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()

    def join_wifi(self, ssid: str, psk: str = "") -> bool:
        if psk:
            out = self._ssh(
                f"nmcli device wifi connect '{ssid}' password '{psk}' 2>&1 "
                f"|| iw dev wlan0 connect '{ssid}'", timeout=30)
        else:
            out = self._ssh(
                f"nmcli device wifi connect '{ssid}' 2>&1 "
                f"|| iw dev wlan0 connect '{ssid}'", timeout=30)
        return "error" not in out.lower()

    def get_ip(self) -> str:
        return self._ssh(
            "ip -4 addr show | grep 'inet ' | grep -v '127.0.0.1' "
            "| awk '{print $2}' | cut -d/ -f1 | head -1")

    def has_internet(self, host: str = "8.8.8.8") -> bool:
        return ", 0% packet loss" in self._ssh(f"ping -c1 -W3 {host}")

    def screenshot(self, path: str) -> bool:
        proof = self._ssh(
            f"curl -s -m 5 http://ifconfig.me && echo "
            f"&& ping -c1 -W3 8.8.8.8 2>&1 | tail -1")
        with open(path.replace(".png", ".txt"), "w") as f:
            f.write(proof)
        return len(proof) > 0

    def state_text(self) -> str:
        external = self._ssh("curl -s -m 5 http://ifconfig.me")
        route = self._ssh("ip route | grep default | head -1")
        return f"external_ip: {external}\nroute: {route}\n"

    def submit_token(self, token: str) -> bool:
        gateway = self._ssh(
            "ip route | grep default | awk '{print $3}' | head -1")
        out = self._ssh(
            f"curl -s -m 20 -X POST -H 'Content-Type: text/plain' "
            f"-d '{token}' http://{gateway}:2121/")
        return "1022" in out

    def portal_detected(self) -> bool:
        out = self._ssh(
            "curl -s -o /dev/null -w '%{http_code}' -m 5 "
            "http://connectivitycheck.gstatic.com/generate_204")
        return out in ("307", "302")


DEVICE_PLACES = {
    "android-phone": lambda: ADBClientDevice(
        os.environ.get("PHONE_SERIAL", ""), "android-phone"),
    "debian-vm": lambda: SSHClientDevice(
        os.environ.get("TOLLGATE_DEBIAN_HOST", "10.99.99.100"),
        "debian", os.environ.get("TOLLGATE_DEBIAN_PASSWORD", ""),
        "debian-vm"),
    "omarchy-vm": lambda: SSHClientDevice(
        os.environ.get("TOLLGATE_OMARCHY_HOST", "10.99.99.101"),
        "root", "", "omarchy-vm"),
}


def get_client_device(place_name: str) -> ClientDevice:
    factory = DEVICE_PLACES.get(place_name)
    if not factory:
        raise ValueError(f"Unknown device place: {place_name}")
    return factory()


def story_client_name() -> str:
    """Active story client place (env-driven, default android-phone).

    Set TOLLGATE_STORY_CLIENT=debian-vm (or omarchy-vm) when running the
    story suite against a VM DUT — routes the state fixtures and any
    env-parametrized test away from the physical phone.
    """
    return os.environ.get("TOLLGATE_STORY_CLIENT", "android-phone")


def pytest_collection_modifyitems(items):
    """Give every story test the phone-tier timeout (300s).

    Story tests drive the physical phone like phone-tier tests do, but
    carry only `slow` markers — the global 60s cap from pytest.ini kills
    them mid-flow (wifi-cycle nudge is 40s alone; cdk-cli V4 minting up
    to 120s). A mid-cycle kill leaves the phone disconnected and
    contaminates the next test, so the cap must not apply here.
    """
    for item in items:
        item.add_marker(pytest.mark.timeout(300))


# ═══════════════════════════════════════════════════════════════════════
# Labgrid Place Mutex (acquire/release around test runs)
# ═══════════════════════════════════════════════════════════════════════

LABGRID_COORDINATOR = os.environ.get(
    "LG_COORDINATOR", "192.168.13.208:20408")


def _resolve_labgrid_client() -> str:
    found = shutil.which("labgrid-client")
    if found:
        return found
    sibling = os.path.join(os.path.dirname(sys.executable),
                           "labgrid-client")
    return sibling if os.path.exists(sibling) else "labgrid-client"


LABGRID_CLIENT = _resolve_labgrid_client()


def _labgrid_client(*args, place: str | None = None) -> subprocess.CompletedProcess:
    cmd = [LABGRID_CLIENT, "-x", LABGRID_COORDINATOR]
    if place:
        cmd += ["-p", place]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15)


@pytest.fixture(scope="function")
def labgrid_phone_mutex(request):
    """Acquire the android-test labgrid place for the duration of a test.

    This prevents multiple agents/sessions from using the phone
    simultaneously. Yields True if acquired, skips if unavailable.
    """
    place = "android-test"
    r = _labgrid_client("lock", place=place)
    if r.returncode != 0:
        pytest.skip(f"labgrid place '{place}' not available: {r.stderr.strip()}")
    log.info("acquired labgrid place '%s'", place)
    yield True
    _labgrid_client("release", place=place)
    log.info("released labgrid place '%s'", place)


# Map device place names to labgrid place names for the mutex
DEVICE_TO_LABGRID_PLACE = {
    "android-phone": "android-test",
}


@pytest.fixture(scope="function")
def device_mutex(request):
    """Acquire the labgrid place mutex for the current device, if available.

    This is a no-op fixture for devices without labgrid places.
    """
    device_place = getattr(request, "param", None)
    if not device_place:
        # Try to get from the test's parameterization
        return None

    labgrid_place = DEVICE_TO_LABGRID_PLACE.get(device_place)
    if not labgrid_place:
        return None

    r = _labgrid_client("lock", place=labgrid_place)
    if r.returncode == 0:
        log.info("acquired labgrid place '%s' for '%s'", labgrid_place, device_place)
        yield labgrid_place
        _labgrid_client("release", place=labgrid_place)
        log.info("released labgrid place '%s'", labgrid_place)
    else:
        # Don't skip — the device may still be available directly
        log.warning("labgrid place '%s' locked by another user, "
                    "proceeding without mutex", labgrid_place)
        yield None


# ═══════════════════════════════════════════════════════════════════════
# SSID Auto-Resolution
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def tollgate_ssid():
    """Resolve the actual SSID from the router, not just the prefix.

    TOLLGATE_SSID=TollGate → queries router → returns "TollGate-0805"
    """
    prefix = os.environ.get("TOLLGATE_SSID", "TollGate")
    router = get_router_host()
    if router:
        full = resolve_ssid(router, prefix)
        if full != prefix:
            log.info("SSID auto-resolved: %s → %s", prefix, full)
            return full
    return prefix


# ═══════════════════════════════════════════════════════════════════════
# Evidence Recording (video + screenshots)
# ═══════════════════════════════════════════════════════════════════════

class StoryRecorder:
    """Step-screenshot evidence with visual truth-assessment.

    Each shot is classified: a blank or byte-identical frame cannot
    substantiate its claim and is marked degraded in the manifest with a
    loud warning (sentinel-garbage lesson — never accept empty evidence
    silently). Devices that implement ``state_text()`` get a non-visual
    sidecar per step, so proof survives display-render faults.
    """

    def __init__(self, art_dir: str):
        self.steps: list[dict] = []
        self.device = None  # set by the test via attach()
        self.art_dir = art_dir
        self._hashes: set[str] = set()

    def attach(self, device):
        self.device = device
        if os.path.basename(self.art_dir) == "unknown":
            renamed = os.path.join(
                os.path.dirname(self.art_dir), device.name)
            os.rename(self.art_dir, renamed)
            self.art_dir = renamed

    def _assess_visual(self, path: str) -> str:
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        if digest in self._hashes:
            return "degraded:duplicate"
        self._hashes.add(digest)
        if Image is not None:
            with Image.open(path) as img:
                rgb = img.convert("RGB")
                colors = rgb.getcolors(maxcolors=1 << 24) or []
                total = rgb.width * rgb.height
            dominant = max((c for c, _ in colors), default=0) / total
            blank = dominant > 0.995
        else:
            blank = os.path.getsize(path) < 12288
        return "degraded:blank" if blank else "ok"

    def _write_state(self, step: str) -> str | None:
        state = getattr(self.device, "state_text", None)
        if not callable(state):
            return None
        text = state()
        if not isinstance(text, str) or not text:
            return None
        path = os.path.join(self.art_dir, f"{step}.state.txt")
        with open(path, "w") as f:
            f.write(text)
        return path

    def shot(self, step: str, claim: str):
        path = os.path.join(self.art_dir, f"{step}.png")
        entry: dict = {
            "step": step, "file": path, "claim": claim,
            "timestamp": time.strftime("%H:%M:%S"),
        }
        state_path = self._write_state(step)
        if state_path:
            entry["state"] = state_path
        captured = bool(self.device) and self.device.screenshot(path)
        if not captured:
            log.warning(
                "EVIDENCE CAPTURE FAILED [%s]: no screenshot for '%s' "
                "(state sidecar: %s)",
                step, claim, state_path or "none")
            entry["visual"] = "capture-failed"
            entry["evidence_ok"] = False
            self.steps.append(entry)
            return None
        visual = self._assess_visual(path)
        entry["visual"] = visual
        entry["evidence_ok"] = visual == "ok"
        if visual != "ok":
            log.warning(
                "EVIDENCE DEGRADED [%s]: %s — screenshot cannot "
                "substantiate '%s' (see %s.state.txt)",
                step, visual, claim, step)
        self.steps.append(entry)
        log.info("evidence: %s (%s, visual=%s)",
                 step, claim, visual)
        return path

    def write_manifest(self):
        import json
        manifest = os.path.join(self.art_dir, "evidence-steps.json")
        with open(manifest, "w") as f:
            json.dump({"steps": self.steps}, f, indent=2)


@pytest.fixture(scope="function")
def story_evidence(request, results_dir):
    """Evidence recorder for user-story tests (video + assessed
    screenshots + state sidecars). Works with any ClientDevice that has
    a screenshot() method.
    """
    art_dir = os.path.join(results_dir, "artifacts", "unknown")
    os.makedirs(art_dir, exist_ok=True)
    rec = StoryRecorder(art_dir)
    yield rec
    rec.write_manifest()


# ═══════════════════════════════════════════════════════════════════════
# Screenrecord evidence (android) — chained segments + screen keepalive
# ═══════════════════════════════════════════════════════════════════════

class AndroidScreenRecorder:
    """Record chained ``screenrecord`` segments, pulled to artifacts on stop.

    Keeps the screen awake while recording: the unplugged bench phone dozes
    within ~30s and a sleeping screen records black — useless as evidence
    (take-3 class: dark screens produce empty page-load evidence).
    Segments are short so an unclean teardown loses at most one.
    """

    SEGMENT_SECONDS = 50
    MIN_VALID_BYTES = 4096

    def __init__(self, serial: str, art_dir: str):
        self.serial = serial
        self.art_dir = art_dir
        self.segments: list[str] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _adb(self, *args: str, timeout: int = 15):
        return subprocess.run(
            ["adb", "-s", self.serial] + list(args),
            capture_output=True, text=True, timeout=timeout)

    def _wake(self) -> None:
        self._adb("shell", "input", "keyevent", "224")  # KEYCODE_WAKEUP
        self._adb("shell", "wm", "dismiss-keyguard")

    def _keepalive_loop(self) -> None:
        while not self._stop.wait(10):
            self._wake()

    def _record_loop(self) -> None:
        n = 0
        while not self._stop.is_set():
            n += 1
            # /data/local/tmp stays writable when emulated /sdcard is
            # locked after a wedged reboot
            remote = f"/data/local/tmp/tg-story-seg{n:02d}.mp4"
            p = subprocess.Popen(
                ["adb", "-s", self.serial, "shell", "screenrecord",
                 "--time-limit", str(self.SEGMENT_SECONDS), remote])
            rc = p.wait()
            # Always offer the file to stop(): SIGINT-finalized segments (the
            # normal teardown path) exit non-zero, and screenrecord exits
            # instantly with rc 218 (INVALID_LAYER_STACK) while the display
            # dozes — stop()'s size filter separates real video from stubs.
            self.segments.append(remote)
            if rc != 0 or self._stop.is_set():
                self._wake()
                self._stop.wait(3)
                if self._stop.is_set():
                    break
                continue

    def start(self) -> None:
        os.makedirs(self.art_dir, exist_ok=True)
        self._wake()
        time.sleep(1)
        for target in (self._keepalive_loop, self._record_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        # Finalize the in-flight segment (SIGINT makes screenrecord write
        # its header); a failed kill only costs the current segment.
        self._adb("shell", "pkill", "-INT", "screenrecord")
        for t in self._threads:
            t.join(timeout=self.SEGMENT_SECONDS + 10)
        for remote in self.segments:
            local = os.path.join(self.art_dir, os.path.basename(remote))
            r = self._adb("pull", remote, local, timeout=60)
            if r.returncode == 0 and \
                    os.path.getsize(local) >= self.MIN_VALID_BYTES:
                log.info("video segment: %s", local)
            elif os.path.exists(local):
                os.unlink(local)
            self._adb("shell", "rm", "-f", remote)


@pytest.fixture(scope="function")
def story_video(results_dir):
    """Chained screen recording for the test duration (android only).

    No-op (yields None) when no PHONE_SERIAL is set — non-android devices
    keep their own evidence paths.
    """
    serial = os.environ.get("PHONE_SERIAL", "")
    if not serial:
        yield None
        return
    rec = AndroidScreenRecorder(
        serial, os.path.join(results_dir, "artifacts", "video"))
    rec.start()
    yield rec
    rec.stop()


# ═══════════════════════════════════════════════════════════════════════
# Run logs — phone logcat + router logread + labgrid topology snapshot
# ═══════════════════════════════════════════════════════════════════════

def _capture(cmd: list[str], path: str) -> None:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        with open(path, "wb") as f:
            f.write(r.stdout or b"(no output)\n")
    except Exception as exc:  # noqa: BLE001 — logging glue must not fail runs
        with open(path, "w") as f:
            f.write(f"(capture failed: {exc})\n")


@pytest.fixture(scope="function")
def story_logs(results_dir):
    """Ship phone logcat, router logread and the labgrid topology with
    every story run.

    The 2026-09-27 walls (phone FallbackHome limbo, DUT br-lan collapse)
    were diagnosed from ad-hoc probes after the fact; this fixture makes
    that evidence automatic — teardown captures the logcat ring covering
    the run window plus a filtered key slice (window manager,
    connectivity, capture errors) and the router's logread tail
    (tollgate/nds/dnsmasq/hostapd/netifd).
    """
    out_dir = os.path.join(results_dir, "artifacts", "logs")
    os.makedirs(out_dir, exist_ok=True)
    serial = os.environ.get("PHONE_SERIAL", "")
    router = os.environ.get("TOLLGATE_SSH_HOST", "")

    def collect():
        ts = time.strftime("%H%M%S")
        if serial:
            base = ["adb", "-s", serial, "shell"]
            _capture(base + ["logcat -d -t 1500 -v time"],
                     os.path.join(out_dir, f"phone-logcat-{ts}.txt"))
            _capture(base + [
                "logcat -d -t 1500 -v time WindowManager:I "
                "ConnectivityService:I NetworkMonitor:I "
                "screencap:S screenrecord:S *:S"],
                os.path.join(out_dir, f"phone-key-slice-{ts}.txt"))
        if router:
            _capture(["ssh", "-o", "ConnectTimeout=5",
                      "-o", "StrictHostKeyChecking=no",
                      f"root@{router}", "logread | tail -400"],
                     os.path.join(out_dir, f"router-logread-{ts}.txt"))
        _capture(
            [LABGRID_CLIENT, "-x", LABGRID_COORDINATOR, "places"],
            os.path.join(out_dir, f"labgrid-places-{ts}.txt"))
        _capture(
            [LABGRID_CLIENT, "-x", LABGRID_COORDINATOR, "who"],
            os.path.join(out_dir, f"labgrid-who-{ts}.txt"))
        for place in ("android-test", "nr7101-router"):
            _capture(
                [LABGRID_CLIENT, "-x", LABGRID_COORDINATOR,
                 "-p", place, "resources"],
                os.path.join(out_dir, f"labgrid-{place}-{ts}.txt"))
        log.info("story_logs: captured to %s", out_dir)

    yield collect
    collect()


# ═══════════════════════════════════════════════════════════════════════
# Session State Management (labgrid Strategy pattern)
#
# Tests declare the state they need via fixtures. Each fixture guarantees
# the device is in that state on entry and cleans up on exit.
# See docs/session-state-management.md for the full design rationale.
# ═══════════════════════════════════════════════════════════════════════

PHONE_MAC = "24:46:c8:a9:de:bb"
RATE_LIMIT_PER_MIN = 8  # margin below backend's 10/min


def _router_ssh(cmd: str) -> str:
    host = os.environ.get("TOLLGATE_SSH_HOST", "")
    if not host:
        return ""
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=no",
         f"root@{host}", cmd],
        capture_output=True, text=True, timeout=15).stdout.strip()


def _device_mac(device) -> str:
    """Resolve the client's LAN interface MAC (ADB + SSH devices).

    Prefers wireless interfaces (wl*), skips loopback. Reads sysfs so
    it works on Android toybox and Debian alike.
    """
    shell = getattr(device, "_shell", None) or getattr(device, "_ssh", None)
    if shell is None:
        return ""
    out = shell(
        "for f in /sys/class/net/*/address; do echo $f $(cat $f); done")
    best = ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        path, mac = parts
        name = path.split("/")[-2]
        if name == "lo" or mac.count(":") != 5 or set(mac) == {"0", ":"}:
            continue
        if name.startswith("wl"):
            return mac
        if not best:
            best = mac
    return best


def _deauth_device(device=None):
    """Deauth the client on the router (ndsctl).

    Resolves the client MAC from the device itself so any story client
    (phone or VM) is handled; falls back to the known phone MAC.
    """
    if device is None:
        device = get_client_device(story_client_name())
    mac = _device_mac(device)
    if not mac:
        mac = PHONE_MAC
        if getattr(device, "name", "android-phone") != "android-phone":
            log.warning(
                "could not resolve MAC for %s — deauth falls back to the "
                "phone MAC and may target the wrong client", device.name)
    _router_ssh(f"ndsctl deauth {mac} 2>/dev/null || true")
    time.sleep(2)


def _mint_and_pay(device) -> bool:
    from lib.cashu import HttpMinter
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL",
                              "http://192.168.13.221:8383")
    minter = HttpMinter(mint_url)
    token = minter.mint(4)
    return device.submit_token(token)


@pytest.fixture(scope="session")
def rate_limiter():
    """Track payment timestamps, back off when approaching the backend limit."""
    payments: list[float] = []

    def check():
        now = time.time()
        payments[:] = [t for t in payments if now - t < 60]
        if len(payments) >= RATE_LIMIT_PER_MIN:
            wait = 60 - (now - payments[0]) + 1
            log.info("rate limit approaching (%d payments), waiting %.1fs",
                     len(payments), wait)
            time.sleep(wait)
        payments.append(now)

    return check


@pytest.fixture(scope="function")
def no_session(tollgate_ssid, rate_limiter):
    """Guarantee the device is NOT authenticated (portal visible).

    Setup: deauth the device's MAC on the router, then VERIFY the gate
    stays closed through a backend session-keeper tick — the keeper
    re-authenticates MACs with live paid sessions (observed 2026-09-27,
    NR7101/tollgate-wrt v0.6.0-alpha4: deauth -> "Authenticating" 9s
    later from upstream_session_manager). A deauth alone cannot
    manufacture no_session while a session lives, so the fixture skips
    loudly instead of letting the test fail on a phantom precondition.

    Teardown: none (next fixture sets its own state).
    """
    device = get_client_device("android-phone")
    if not device.join_wifi(tollgate_ssid):
        pytest.skip(f"device could not join {tollgate_ssid}")
    _deauth_device()
    deadline = time.time() + 40
    while time.time() < deadline:
        if device.has_internet():
            pytest.skip(
                "no_session unreachable: the backend session-keeper "
                "re-authenticated the client (live paid session) — "
                "expire or clear that session before requesting "
                "no_session")
        time.sleep(2)
    yield device


@pytest.fixture(scope="function")
def fresh_session(tollgate_ssid, rate_limiter):
    """Guarantee the device IS authenticated with a working session.

    Setup: deauth (reset), then mint token and pay.
    Teardown: deauth (clean state for next test).
    """
    device = get_client_device(story_client_name())
    if not device.join_wifi(tollgate_ssid):
        pytest.skip(f"device could not join {tollgate_ssid}")

    _deauth_device(device)
    rate_limiter()

    if not _mint_and_pay(device):
        pytest.skip("could not establish fresh session (payment failed)")
    assert device.has_internet(), "fresh_session setup: no internet after payment"

    yield device

    _deauth_device(device)
