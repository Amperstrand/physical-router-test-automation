"""Run-log collection for story runs.

A RunLogCollector snapshots a set of LogSources into a run's artifacts
directory. Sources are env-driven so the same collector serves the
physical bench and the VM lab:

  PHONE_SERIAL / TOLLGATE_PHONE_SERIAL   phone logcat ring + key slice
  TOLLGATE_SSH_HOST                      router logread tail (ssh)
  TOLLGATE_CONSOLE_LOG                   console history file — QEMU
                                         chardev ``logfile=`` on VMs today;
                                         any file a serial-bridge daemon
                                         writes for physical routers later
  TOLLGATE_CONSOLE_TCP                   host:port TCP serial bridge
                                         (conwrt-serial-bridge /
                                         labgrid NetworkSerialPort shape)
  LG_COORDINATOR                         labgrid places/who/resources
  TOLLGATE_LABGRID_PLACES                comma list — per-place resource
                                         snapshots beyond places/who

Console history is the class of evidence that survives the network stack
itself: the 2026-09-27 br-lan collapse on the physical DUT was diagnosed
from logread AFTER recovery; with serial history the boot-time netifd
errors would have been in the bundle already.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Protocol

SSH_OPTS = ["-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no"]

TAIL_CAP_BYTES = 256 * 1024


class LogSource(Protocol):
    name: str

    def available(self) -> bool: ...

    def collect(self, path: str) -> None: ...


def _write(path: str, data: bytes, note: str = "") -> None:
    with open(path, "wb") as f:
        f.write(data)
        if note:
            f.write(note.encode())


class PhoneLogcatSource:
    """Dump the phone's logcat ring (full + filtered key slice)."""

    def __init__(self, serial: str, key_slice: bool = True):
        self.serial = serial
        self.key_slice = key_slice
        self.name = "phone-logcat-key" if key_slice else "phone-logcat"

    def available(self) -> bool:
        return bool(self.serial)

    def collect(self, path: str) -> None:
        cmd = ["adb", "-s", self.serial, "shell",
               "logcat -d -t 1500 -v time"]
        if self.key_slice:
            cmd[-1] += (" WindowManager:I ConnectivityService:I"
                        " NetworkMonitor:I screencap:S screenrecord:S *:S")
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            _write(path, r.stdout or b"(no output)\n")
        except Exception as exc:  # noqa: BLE001 — log glue must not fail runs
            _write(path, b"", f"(capture failed: {exc})\n")


class RouterLogreadSource:
    """Tail the router's logread (tollgate/nds/dnsmasq/hostapd/netifd)."""

    def __init__(self, host: str, user: str = "root"):
        self.host = host
        self.user = user
        self.name = "router-logread"

    def available(self) -> bool:
        return bool(self.host)

    def collect(self, path: str) -> None:
        try:
            r = subprocess.run(
                ["ssh", *SSH_OPTS, f"{self.user}@{self.host}",
                 "logread | tail -400"],
                capture_output=True, timeout=30)
            _write(path, r.stdout or b"(no output)\n")
        except Exception as exc:  # noqa: BLE001
            _write(path, b"", f"(capture failed: {exc})\n")


class ConsoleFileSource:
    """Snapshot a persisted console-history file (tail-capped).

    On VM routers this is the QEMU chardev ``logfile=`` written since VM
    start; when physical routers gain serial bridges that log to files,
    the same source serves them unchanged.
    """

    def __init__(self, path: str, label: str = "console"):
        self.path = path
        self.name = f"console-{label}"

    def available(self) -> bool:
        return bool(self.path) and os.path.exists(self.path)

    def collect(self, path: str) -> None:
        try:
            size = os.path.getsize(self.path)
            with open(self.path, "rb") as f:
                if size > TAIL_CAP_BYTES:
                    f.seek(-TAIL_CAP_BYTES, os.SEEK_END)
                    data = f.read()
                    note = f"\n(tailed last {TAIL_CAP_BYTES} of {size} bytes)\n"
                else:
                    data = f.read()
                    note = ""
            _write(path, data, note)
        except Exception as exc:  # noqa: BLE001
            _write(path, b"", f"(capture failed: {exc})\n")


class ConsoleTcpSource:
    """Stream a TCP serial bridge (host:port) to a file while attached.

    Physical consoles are historical blind spots until a bridge exists;
    when one does (conwrt-serial-bridge :NNNN / labgrid NetworkSerialPort
    behind a TCP forward), this source attaches for the run window and
    keeps whatever the console emitted. ``start_live``/``stop_live`` bracket
    the run; ``collect`` snapshots what accumulated.
    """

    def __init__(self, endpoint: str, label: str = "tcp"):
        self.endpoint = endpoint
        self.name = f"console-{label}"
        self._live_path: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sock: socket.socket | None = None

    def available(self) -> bool:
        return bool(self.endpoint)

    def start_live(self) -> None:
        if self._thread or not self.available():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._stream, daemon=True)
        self._thread.start()

    def _stream(self) -> None:
        host, _, port = self.endpoint.partition(":")
        try:
            with socket.create_connection((host, int(port)), timeout=5) as s:
                self._sock = s
                s.settimeout(1)
                with open(self._live_path or os.devnull, "ab") as f:
                    while not self._stop.is_set():
                        try:
                            chunk = s.recv(4096)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        f.write(chunk)
        except OSError:
            pass  # bridge down — snapshot will say so
        finally:
            self._sock = None

    def stop_live(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def collect(self, path: str) -> None:
        if self._live_path and os.path.exists(self._live_path):
            try:
                with open(self._live_path, "rb") as f:
                    _write(path, f.read())
                return
            except OSError as exc:
                _write(path, b"", f"(capture failed: {exc})\n")
                return
        _write(path, b"", "(never attached: start_live was not called "
                          "or the bridge was down)\n")


class LabgridSnapshotSource:
    """Record labgrid coordinator topology (places/who/resources)."""

    def __init__(self, client: str, coordinator: str,
                 subcommand: str = "places", place: str = ""):
        self.client = client
        self.coordinator = coordinator
        self.subcommand = subcommand
        self.place = place
        self.name = f"labgrid-{subcommand}" + (f"-{place}" if place else "")

    def available(self) -> bool:
        return bool(self.client and self.coordinator)

    def collect(self, path: str) -> None:
        cmd = [self.client, "-x", self.coordinator]
        if self.place:
            cmd += ["-p", self.place]
        cmd.append(self.subcommand)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=15)
            _write(path, r.stdout or b"(no output)\n")
        except Exception as exc:  # noqa: BLE001
            _write(path, b"", f"(capture failed: {exc})\n")


def default_sources(env: dict[str, str] | None = None) -> list[LogSource]:
    """Build the standard source set from an environment mapping.

    ``env={}`` means an explicitly empty environment (no sources); only
    ``env=None`` falls back to ``os.environ``.
    """
    env = dict(os.environ) if env is None else dict(env)
    phone = env.get("PHONE_SERIAL") or env.get("TOLLGATE_PHONE_SERIAL", "")
    router = env.get("TOLLGATE_SSH_HOST", "")
    console_log = env.get("TOLLGATE_CONSOLE_LOG", "")
    console_tcp = env.get("TOLLGATE_CONSOLE_TCP", "")
    lg_client = env.get("TOLLGATE_LABGRID_CLIENT", "")
    lg_coord = env.get("LG_COORDINATOR", "")
    lg_places = [p.strip() for p in
                 env.get("TOLLGATE_LABGRID_PLACES", "").split(",") if p.strip()]

    sources: list[LogSource] = []
    if phone:
        sources.append(PhoneLogcatSource(phone, key_slice=False))
        sources.append(PhoneLogcatSource(phone, key_slice=True))
    if router:
        sources.append(RouterLogreadSource(router))
    if console_log:
        sources.append(ConsoleFileSource(console_log,
                                         label=os.path.basename(console_log)))
    if console_tcp:
        sources.append(ConsoleTcpSource(console_tcp,
                                        label=console_tcp.replace(":", "-")))
    if lg_client and lg_coord:
        sources.append(LabgridSnapshotSource(lg_client, lg_coord, "places"))
        sources.append(LabgridSnapshotSource(lg_client, lg_coord, "who"))
        for place in lg_places:
            sources.append(LabgridSnapshotSource(
                lg_client, lg_coord, "resources", place=place))
    return sources


class RunLogCollector:
    """Snapshot every available LogSource into a run artifacts directory."""

    def __init__(self, sources: list[LogSource] | None = None):
        self.sources: list[LogSource] = list(sources or [])

    def register(self, source: LogSource) -> None:
        self.sources.append(source)

    def start_live(self, staging_dir: str) -> None:
        """Begin live streaming for sources that support it (TCP console)."""
        os.makedirs(staging_dir, exist_ok=True)
        for src in self.sources:
            if isinstance(src, ConsoleTcpSource):
                src._live_path = os.path.join(
                    staging_dir, f"{src.name}.live")
                src.start_live()

    def stop_live(self) -> None:
        for src in self.sources:
            if isinstance(src, ConsoleTcpSource):
                src.stop_live()

    def collect_into(self, out_dir: str, ts: str | None = None) -> list[str]:
        ts = ts or time.strftime("%H%M%S")
        os.makedirs(out_dir, exist_ok=True)
        written: list[str] = []
        for src in self.sources:
            if not src.available():
                continue
            path = os.path.join(out_dir, f"{src.name}-{ts}.txt")
            src.collect(path)
            written.append(path)
        return written


def resolve_labgrid_client() -> str:
    """Find labgrid-client: PATH first, then beside the running python."""
    found = shutil.which("labgrid-client")
    if found:
        return found
    sibling = os.path.join(os.path.dirname(sys.executable),
                           "labgrid-client")
    if os.path.exists(sibling):
        return sibling
    return "labgrid-client"
