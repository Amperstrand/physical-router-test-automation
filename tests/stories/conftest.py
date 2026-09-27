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
import os
import subprocess
import time
from typing import Protocol

import pytest

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
        subprocess.run(
            self._base + ["shell", "screencap", "-p", "/sdcard/tg-ss.png"],
            capture_output=True, timeout=15)
        r = subprocess.run(
            self._base + ["pull", "/sdcard/tg-ss.png", path],
            capture_output=True, timeout=15)
        return r.returncode == 0

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


# ═══════════════════════════════════════════════════════════════════════
# Labgrid Place Mutex (acquire/release around test runs)
# ═══════════════════════════════════════════════════════════════════════

LABGRID_COORDINATOR = os.environ.get(
    "LG_COORDINATOR", "192.168.13.208:20408")
LABGRID_CLIENT = os.path.join(
    os.path.dirname(subprocess.run(["which", "python3"],
                                    capture_output=True, text=True).stdout.strip()),
    "labgrid-client")


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

@pytest.fixture(scope="function")
def story_evidence(request, results_dir):
    """Evidence recorder for user-story tests.

    Wraps lib/clients/evidence.py to produce video + step screenshots.
    Works with any ClientDevice that has a screenshot() method.
    """
    device_name = getattr(request, "param", "unknown")
    art_dir = os.path.join(results_dir, "artifacts", device_name)
    os.makedirs(art_dir, exist_ok=True)

    class StoryRecorder:
        def __init__(self):
            self.steps: list[dict] = []
            self.device = None  # set by the test

        def attach(self, device):
            self.device = device

        def shot(self, step: str, claim: str):
            path = os.path.join(art_dir, f"{step}.png")
            if self.device and self.device.screenshot(path):
                self.steps.append({
                    "step": step, "file": path, "claim": claim,
                    "timestamp": time.strftime("%H:%M:%S"),
                })
                log.info("evidence: %s (%s)", step, claim)
                return path
            return None

        def write_manifest(self):
            import json
            manifest = os.path.join(art_dir, "evidence-steps.json")
            with open(manifest, "w") as f:
                json.dump({"steps": self.steps}, f, indent=2)

    rec = StoryRecorder()
    yield rec
    rec.write_manifest()
