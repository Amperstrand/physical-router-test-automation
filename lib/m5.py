"""M5 Atom hardware Cashu mint client for PRTA.

The M5 Atom runs the m5-cashu-mint firmware: a fake-wallet Cashu mint
(quotes settle instantly) served over WiFi on port 3338, provisioned via a
serial CLI. This module discovers the device over USB serial, verifies it
is online and healthy, and mints V3 tokens through HttpMinter.

Serial CLI contract (firmware >= 0.1, 115200 baud):
    ssid <name> | psk <pass|-> | join | status | reset
    status -> "wifi=up|down ssid=<s> ip=<ip> st=<n> keyset=<id> url=http://<ip>:3338"

Labgrid mode (the supported shared-bench path): set
TOLLGATE_M5_LABGRID_PLACE=<place> (or TOLLGATE_M5_PORT=labgrid://<place>)
and the client acquires the place for exclusive use, drives the serial
console through `ssh $TOLLGATE_LABGRID_HOST labgrid-client -p <place>
console`, and releases the place on teardown. The coordinator is not
reachable from test hosts — all labgrid traffic goes through the exporter
host over SSH. Any labgrid place exporting a SerialPort backed by an
ESP32-class CLI works; acquire failure (place held) raises
M5MintUnavailable naming the holder, so tests skip visibly instead of
fighting over hardware.

Firmware quirks handled here:
    <0.2: parametrized GET routes (/v1/mint/quote/bolt11/{id}, /v1/keys/{id})
          never matched on the installed Arduino core (404). Quote-create
          responses are authoritative and always state=PAID, so the mint
          flow skips quote polling when create says PAID.
    any:  keyset is stable per boot only before v0.2 (NVS-seeded DRBG lands
          in 0.2). Do not reboot the device between minting and paying.
    any:  pyserial must open with RTS/DTR deasserted — the auto-reset
          circuit holds the ESP32 in reset when RTS is asserted.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import select
import subprocess
import time
from urllib import request as urlrequest
from urllib.error import URLError

from lib.cashu import HttpMinter

log = logging.getLogger("tollgate.m5")

MINT_PORT = 3338
BAUD = 115200
DEFAULT_ID_GLOBS = (
    "/dev/serial/by-id/*Hades2001*",
    "/dev/serial/by-id/*M5STACK*",
)
LABGRID_PREFIX = "labgrid://"
DEFAULT_LABGRID_HOST = "ai-legion"
_STATUS_RE = re.compile(
    r"wifi=(up|down) ssid=(\S*) ip=(\S+) st=(\d+) keyset=(\S+) url=(\S+)"
)


class M5MintUnavailable(RuntimeError):
    """Raised when no M5 mint device is attached or it cannot come online."""


class M5Minter(HttpMinter):
    """HttpMinter that trusts a born-PAID quote-create response.

    Firmware <0.2 has no working quote-status GET (parametrized routes 404)
    but creates every quote with state=PAID, so polling is unnecessary.
    On firmware >=0.2 the poll still runs whenever create did not report
    PAID, keeping this minter correct for real pending-quote mints.
    """

    def __init__(self, mint_url: str):
        super().__init__(mint_url)
        self._quote_state = "UNPAID"

    def _http_post(self, url: str, body: dict, timeout: int = 15) -> dict:
        resp = super()._http_post(url, body, timeout)
        if url.endswith("/v1/mint/quote/bolt11"):
            self._quote_state = resp.get("state", "UNPAID")
        return resp

    def _http_get(self, url: str, timeout: int = 15) -> dict:
        if "/v1/mint/quote/bolt11/" in url and self._quote_state == "PAID":
            return {"state": "PAID"}
        return super()._http_get(url, timeout)


class _LabgridConsole:
    """Serial CLI over `ssh <host> labgrid-client -p <place> console`.

    One persistent process for the whole session: labgrid allows a single
    console attachment per place, and we hold the place anyway.
    """

    def __init__(self, place: str, host: str):
        self.place = place
        self.host = host
        self._proc: subprocess.Popen | None = None

    def _ensure_proc(self) -> subprocess.Popen:
        if self._proc and self._proc.poll() is None:
            return self._proc
        cmd = ["ssh", self.host, "labgrid-client", "-p", self.place, "console"]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            raise M5MintUnavailable(f"cannot spawn ssh for labgrid console: {exc}") from exc
        time.sleep(2.0)  # console attach + initial firmware noise
        self._drain(0.5)
        return self._proc

    def _drain(self, settle: float) -> bytes:
        proc = self._ensure_proc()
        assert proc.stdout is not None
        out = b""
        fd = proc.stdout.fileno()
        while True:
            ready, _, _ = select.select([fd], [], [], settle)
            if not ready:
                break
            chunk = os.read(fd, 4096)
            if not chunk:
                raise M5MintUnavailable(
                    f"labgrid console for {self.place} exited "
                    f"(rc={proc.poll()}); last output: {out[-200:]!r}"
                )
            out += chunk
        return out

    def cli(self, line: str, wait: float = 2.0, settle: float = 0.4) -> str:
        proc = self._ensure_proc()
        assert proc.stdin is not None
        proc.stdin.write((line + "\r\n").encode())
        proc.stdin.flush()
        out = b""
        deadline = time.monotonic() + wait
        last_data = time.monotonic()
        while time.monotonic() < deadline:
            chunk = self._drain(settle)
            if chunk:
                out += chunk
                last_data = time.monotonic()
            elif out and time.monotonic() - last_data > settle:
                break
        return out.decode(errors="replace")

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


def labgrid_acquire(place: str, host: str) -> None:
    """Acquire the place exclusively; raise naming the holder on conflict."""
    proc = subprocess.run(
        ["ssh", host, "labgrid-client", "-p", place, "acquire"],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise M5MintUnavailable(
            f"labgrid place '{place}' not acquired (held or unreachable): {detail[:200]}"
        )
    log.info("labgrid place '%s' acquired for exclusive M5 use", place)


def labgrid_release(place: str, host: str) -> None:
    proc = subprocess.run(
        ["ssh", host, "labgrid-client", "-p", place, "release"],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        log.warning(
            "labgrid release of '%s' failed: %s",
            place, (proc.stderr or proc.stdout).strip()[:200],
        )
    else:
        log.info("labgrid place '%s' released", place)


def _labgrid_host() -> str:
    return os.environ.get("TOLLGATE_LABGRID_HOST", DEFAULT_LABGRID_HOST)


class M5Mint:
    """Serial-controlled M5 Atom Cashu mint (local USB or labgrid place)."""

    def __init__(self, port: str):
        self.port = port
        self._minter: M5Minter | None = None
        self.labgrid_place: str | None = None
        self._lg: _LabgridConsole | None = None
        if port.startswith(LABGRID_PREFIX):
            self.labgrid_place = port[len(LABGRID_PREFIX):]
            self._lg = _LabgridConsole(self.labgrid_place, _labgrid_host())
            labgrid_acquire(self.labgrid_place, _labgrid_host())
        elif not os.path.exists(port):
            raise M5MintUnavailable(f"serial port {port} does not exist")

    # -- construction / discovery --

    @classmethod
    def from_env(cls) -> "M5Mint":
        env_place = os.environ.get("TOLLGATE_M5_LABGRID_PLACE", "")
        if env_place:
            return cls(f"{LABGRID_PREFIX}{env_place}")
        env_port = os.environ.get("TOLLGATE_M5_PORT", "")
        if env_port:
            if not env_port.startswith(LABGRID_PREFIX) and not os.path.exists(env_port):
                raise M5MintUnavailable(f"TOLLGATE_M5_PORT={env_port} does not exist")
            return cls(env_port)
        for pattern in DEFAULT_ID_GLOBS:
            ports = sorted(glob.glob(pattern))
            if ports:
                return cls(ports[0])
        raise M5MintUnavailable("no M5 serial device attached")

    # -- serial CLI --

    def cli(self, line: str, wait: float = 2.0, settle: float = 0.4) -> str:
        if self._lg is not None:
            return self._lg.cli(line, wait=wait, settle=settle)

        import serial

        with serial.Serial(self.port, BAUD, timeout=0.5) as ser:
            ser.rts = False
            ser.dtr = False
            time.sleep(0.2)
            ser.reset_input_buffer()
            ser.write((line + "\r\n").encode())
            ser.flush()
            out = ""
            deadline = time.monotonic() + wait
            last_data = time.monotonic()
            while time.monotonic() < deadline:
                chunk = ser.read(4096).decode(errors="replace")
                if chunk:
                    out += chunk
                    last_data = time.monotonic()
                elif time.monotonic() - last_data > settle and out:
                    break
            return out

    def release(self) -> None:
        """Release labgrid exclusivity (no-op for a locally attached device)."""
        if self._lg is not None:
            self._lg.close()
            assert self.labgrid_place is not None
            labgrid_release(self.labgrid_place, _labgrid_host())
            self.labgrid_place = None
            self._lg = None

    def status(self) -> dict:
        out = self.cli("status")
        match = _STATUS_RE.search(out)
        if not match:
            raise M5MintUnavailable(f"no status line from {self.port}: {out[-200:]!r}")
        return {
            "wifi": match.group(1),
            "ssid": match.group(2),
            "ip": None if match.group(3) == "-" else match.group(3),
            "station_state": int(match.group(4)),
            "keyset": match.group(5),
            "url": match.group(6),
        }

    # -- lifecycle --

    def ensure_online(self, timeout: float = 90.0) -> str:
        """Return the mint URL once WiFi is up; join (reboot) if needed."""
        st = self.status()
        if st["wifi"] == "up" and st["ip"]:
            url = f"http://{st['ip']}:{MINT_PORT}"
            if self.is_healthy(url):
                return url
            raise M5MintUnavailable(f"wifi up ({st['ip']}) but mint unhealthy on :{MINT_PORT}")
        log.info("M5 wifi down, sending join (reboot to connect)")
        self.cli("join", wait=1.0)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(5)
            try:
                st = self.status()
            except M5MintUnavailable:
                continue
            if st["wifi"] == "up" and st["ip"]:
                url = f"http://{st['ip']}:{MINT_PORT}"
                if self.is_healthy(url):
                    return url
        raise M5MintUnavailable(f"M5 did not come online within {timeout:.0f}s")

    def is_healthy(self, url: str | None = None, timeout: float = 5.0) -> bool:
        url = url or self.mint_url
        try:
            with urlrequest.urlopen(f"{url}/v1/keys", timeout=timeout) as resp:
                return resp.status == 200
        except (URLError, TimeoutError, OSError):
            return False

    @property
    def mint_url(self) -> str:
        st = self.status()
        if st["wifi"] != "up" or not st["ip"]:
            raise M5MintUnavailable("M5 not online — call ensure_online() first")
        return f"http://{st['ip']}:{MINT_PORT}"

    # -- minting --

    def minter(self) -> M5Minter:
        if self._minter is None:
            self._minter = M5Minter(self.mint_url)
        return self._minter

    def keyset_id(self) -> str:
        return self.status()["keyset"]

    def mint(self, amount: int = 4, timeout: int = 30) -> str:
        return self.minter().mint(amount, timeout=timeout)
