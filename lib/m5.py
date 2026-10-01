"""M5 Atom hardware Cashu mint client for PRTA.

The M5 Atom runs the m5-cashu-mint firmware: a fake-wallet Cashu mint
(quotes settle instantly) served over WiFi on port 3338, provisioned via a
serial CLI. This module discovers the device over USB serial, verifies it
is online and healthy, and mints V3 tokens through HttpMinter.

Serial CLI contract (firmware >= 0.1, 115200 baud):
    ssid <name> | psk <pass|-> | join | status | reset
    status -> "wifi=up|down ssid=<s> ip=<ip> st=<n> keyset=<id> url=http://<ip>:3338"

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


class M5Mint:
    """Serial-controlled M5 Atom Cashu mint."""

    def __init__(self, port: str):
        self.port = port
        self._minter: M5Minter | None = None

    # -- construction / discovery --

    @classmethod
    def from_env(cls) -> "M5Mint":
        env_port = os.environ.get("TOLLGATE_M5_PORT", "")
        if env_port:
            if not os.path.exists(env_port):
                raise M5MintUnavailable(f"TOLLGATE_M5_PORT={env_port} does not exist")
            return cls(env_port)
        for pattern in DEFAULT_ID_GLOBS:
            ports = sorted(glob.glob(pattern))
            if ports:
                return cls(ports[0])
        raise M5MintUnavailable("no M5 serial device attached")

    # -- serial CLI --

    def cli(self, line: str, wait: float = 2.0, settle: float = 0.4) -> str:
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
