"""PRTA scenario-layer adapters — phase-2 PoC (branch scenario-layer-poc).

Thin adapters that wrap PRTA's existing lib/ clients behind tollgate-lab's
four driver Protocols (docs/SCENARIO-LAYER.md §1) and fill the
declared-but-unimplemented registry slots:

- ``debian_container`` client — the QEMU Debian container over SSH
  (the wired twin of the phone client; SSID semantics mapped to
  link/default-route, see ``active_ssid``).
- ``phone_adb`` client — lib/clients/u2phone.U2Phone (lazy uiautomator2).
- ``token_paste`` actor — the container's proven API payment: stage the
  token on the client, prime NDS, POST it to the backend from inside the
  client (identity-scoped by the container's MAC).
- ``portal_tip03`` actor — WRAPS tests/phone/test_rig_phone_payment.
  _pay_through_portal (the TIP-03 state machine); the logic is not
  duplicated, the function is called.
- ``http_module`` gateway — the backend HTTP module (:2121): kind 10021
  advertisement (ssid sourced out-of-band — this backend's event carries
  no ssid tag), identity-scoped /usage + reachability probes riding
  ``client.run_command`` (the S5 lesson), fakewallet minting via
  lib/cashu.HttpMinter.
- ``evidence_recorder`` capture — lib/clients/evidence.EvidenceRecorder
  (phone video + per-step frames).

Importing this module registers the adapters (idempotent); profiles then
reference them by name exactly like the scenario-layer schema prescribes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tollgate_lab.scenarios.contract import (
    CaptureResult,
    ClientDriver,
    GatewayDriver,
    PayReceipt,
    SessionState,
    Usage,
    is_captive_redirect,
)
from tollgate_lab.scenarios.drivers import (
    CAPTURE_DRIVERS,
    CLIENT_DRIVERS,
    GATEWAY_DRIVERS,
    PAYMENT_ACTORS,
    register_driver,
)

log = logging.getLogger("tollgate.scenario.adapters")

STAGED_TOKEN_PATH = "/tmp/tg-scenario-token"


def _ssh_run(prefix: list[str], command: str, timeout_s: int) -> str:
    proc = subprocess.run(prefix + [command], capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0:
        raise RuntimeError(f"client ssh exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout


class DebianContainerClient:
    """ClientDriver over the QEMU Debian container (wired on br-lan)."""

    name = "debian_container"

    def __init__(self, *, ssh: str) -> None:
        self._prefix = shlex.split(os.path.expanduser(ssh))
        self._ssid: str | None = None

    def run_command(self, command: str, *, timeout_s: int = 15) -> str:
        return _ssh_run(self._prefix, command, timeout_s)

    def connect_wifi(self, ssid: str, *, timeout_s: int = 60) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            addr = self.run_command("ip -4 addr show | grep -c 'inet '")
            route = self.run_command("ip route show default")
            if addr.strip() != "0" and route.strip():
                self._ssid = ssid
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("wired client has no address/default route")
            time.sleep(2)

    def active_ssid(self) -> str | None:
        if self._ssid is None:
            return None
        route = self.run_command("ip route show default")
        return self._ssid if route.strip() else None

    def status(self) -> dict[str, Any]:
        return {"balance_sats": 0, "wallet": "none"}

    def inject_token(self, token: str) -> None:
        self.run_command(f"printf '%s' '{token}' > {STAGED_TOKEN_PATH}")

    def open_wallet_ux(self) -> None:
        log.debug("debian container has no wallet UX; portal payment only")

    @property
    def staged_token(self) -> str:
        token = self.run_command(f"cat {STAGED_TOKEN_PATH}").strip()
        if not token:
            raise RuntimeError(f"no staged token at {STAGED_TOKEN_PATH}")
        return token


class PhoneAdbClient:
    """ClientDriver over lib/clients/u2phone.U2Phone (physical phone)."""

    name = "phone_adb"

    def __init__(self, serial: str = "") -> None:
        self.serial = serial or os.environ.get("PHONE_SERIAL", "")
        self._staged: str | None = None
        self._device = None

    @property
    def device(self):
        """The wrapped U2Phone — PRTA-native actors ride this."""
        if self._device is None:
            from lib.clients.u2phone import connect_u2

            self._device = connect_u2(self.serial)
        return self._device

    def run_command(self, command: str, *, timeout_s: int = 15) -> str:
        return self.device.shell(command)

    def connect_wifi(self, ssid: str, *, timeout_s: int = 60) -> None:
        d = self.device
        d.ensure_awake()
        d.ensure_unlocked()
        if not d.wifi_connect(ssid):
            raise RuntimeError(f"phone could not join {ssid!r}")
        d.wait_wifi_validated(ssid, timeout=timeout_s)

    def active_ssid(self) -> str | None:
        out = self.run_command("dumpsys wifi | grep -m1 mWifiInfo")
        match = re.search(r'SSID: "([^"]+)"', out)
        if match and "unknown" not in match.group(1).lower():
            return match.group(1)
        return None

    def status(self) -> dict[str, Any]:
        return {"balance_sats": 0, "wallet": "portal"}

    def inject_token(self, token: str) -> None:
        self._staged = token

    def open_wallet_ux(self) -> None:
        log.debug("portal flow opens the captive portal, not a wallet app")

    @property
    def staged_token(self) -> str:
        if not self._staged:
            raise RuntimeError("no staged token on the phone client")
        return self._staged


class TokenPasteActor:
    """PaymentActor: submit the staged token at the backend API.

    The container's proven payment rail (PRTA run-local/stories paths):
    prime NDS client tracking with a portal fetch, then POST the token
    from inside the client so the backend derives the right MAC.
    """

    strategy = "token_paste"

    def pay(self, client: ClientDriver, gateway: GatewayDriver, *, sats: int) -> PayReceipt:
        base = gateway.client_base_url
        host = urlparse(base).hostname
        token = client.staged_token
        if not token.startswith("cashu"):
            raise RuntimeError(f"staged token is not a cashu token: {token[:20]!r}")

        client.run_command(f"curl -s -m 5 -o /dev/null http://{host}:2050/")
        body = client.run_command(
            f"curl -s -m 30 -X POST -H 'Content-Type: text/plain' --data-binary @{STAGED_TOKEN_PATH} {base}/"
        )
        if not self._accepted(body):
            raise RuntimeError(f"payment rejected: {body[:200]}")
        repair = getattr(gateway, "open_gate_repair", None)
        if repair is not None:
            repair()
        return PayReceipt(sats=sats, strategy=self.strategy, token=token)

    @staticmethod
    def _accepted(body: str) -> bool:
        try:
            return json.loads(body).get("kind") == 1022
        except (json.JSONDecodeError, AttributeError):
            return "1022" in body


def _default_portal_machine(u2phone, token: str, timeout: int = 75) -> str:
    """PRTA's TIP-03 state machine, imported lazily (uiautomator2)."""
    from tests.phone.test_rig_phone_payment import _pay_through_portal

    return _pay_through_portal(u2phone, token, timeout=timeout)


def _default_authed_states() -> tuple[str, ...]:
    from tests.phone.test_rig_phone_payment import AUTHED_STATES

    return AUTHED_STATES


class PrtaPortalTip03Actor:
    """PaymentActor wrapping PRTA's _pay_through_portal state machine.

    portal_ready → token_typing → Purchase/Pay → authed/countdown lives
    in tests/phone/test_rig_phone_payment.py; this actor calls it, it
    does not re-implement it. ``machine``/``authed_states`` are
    injectable for unit tests with fakes (phone off at night).
    """

    strategy = "portal_tip03"

    def __init__(self, *, portal: str = "", timeout_s: int = 90) -> None:
        self._portal = portal
        self._timeout_s = timeout_s
        self.machine = _default_portal_machine

    def pay(self, client: ClientDriver, gateway: GatewayDriver, *, sats: int) -> PayReceipt:
        phone = getattr(client, "device", None)
        if phone is None:
            raise RuntimeError("portal_tip03 needs a PhoneAdbClient")
        phone.ensure_awake()
        phone.ensure_unlocked()
        if self._portal:
            phone.open_url(self._portal)
        state = self.machine(phone, client.staged_token, timeout=self._timeout_s)
        if state not in _default_authed_states():
            raise RuntimeError(f"portal did not authenticate (state={state!r})")
        repair = getattr(gateway, "open_gate_repair", None)
        if repair is not None:
            repair()
        return PayReceipt(sats=sats, strategy=self.strategy, token=client.staged_token)


class PrtaHttpGateway:
    """GatewayDriver over the tollgate backend HTTP module (:2121)."""

    def __init__(
        self,
        *,
        base: str,
        client_base: str = "",
        token_source: str = "fakewallet",
        mint_url: str = "",
    ) -> None:
        # base = host plane (advertisement, mint); client_base = what the
        # paying client probes from inside its network segment. Identical
        # on the flat virtual lab; differs on rigs whose router LAN
        # address is not host-routable (e.g. WS-AP3915i: host sees
        # 192.168.105.51, a joined phone sees 192.168.1.1).
        self.base_url = base.rstrip("/")
        self.client_base_url = (client_base or base).rstrip("/")
        self._token_source = token_source
        self._mint_url = mint_url
        self._adv_mint_url = ""

    def advertisement(self) -> dict[str, Any]:
        import requests

        adv = requests.get(f"{self.base_url}/", timeout=10).json()
        for tag in adv.get("tags", []):
            if tag and tag[0] == "price_per_step" and len(tag) > 4:
                self._adv_mint_url = tag[4]
                break
        if not adv.get("ssid"):
            from lib.clients.ssid import resolve_ssid

            adv["ssid"] = resolve_ssid(urlparse(self.base_url).hostname or "")
        return adv

    def mint_token(self, sats: int) -> str:
        from lib.cashu import HttpMinter

        url = self._mint_url or self._adv_mint_url
        if not url:
            raise RuntimeError("no mint url configured or advertised")
        token = HttpMinter(url).mint(sats)
        if not token:
            raise RuntimeError("fakewallet minted an empty token")
        return token

    def usage(self, client: ClientDriver) -> Usage | None:
        body = client.run_command(f"curl -s -m 10 {self.client_base_url}/usage").strip()
        used, _, allotment = body.partition("/")
        if not (used.lstrip("-").isdigit() and allotment.lstrip("-").isdigit()):
            return Usage()
        return Usage(used=int(used), allotment=int(allotment))

    def session_state(self, client: ClientDriver) -> SessionState:
        data = self._client_probe(client, "/balance")
        active = bool(data and data.get("session_active"))
        return SessionState("active" if active else "none")

    def external_reachable(self, client: ClientDriver) -> bool:
        out = client.run_command("curl -s -m 8 -o /dev/null -w '%{http_code} %{redirect_url}' http://1.1.1.1/").strip()
        code, _, redirect = out.partition(" ")
        if not code.isdigit() or not 200 <= int(code) < 400:
            return False
        return not is_captive_redirect(redirect, self.client_base_url)

    def open_gate_repair(self) -> None:
        """Insert the ndsNET auth-bit accept rule (NDS 5.0.2 mark bug).

        PRTA's rails run this in wait_for_auth; the scenario lifecycle has
        no equivalent hook, so actors trigger it right after an accepted
        payment — otherwise gate_open_asserted's fresh connection is
        REJECTed despite the session being live.
        """
        from lib.router import Router

        router = Router(urlparse(self.base_url).hostname or "", "", "", "scenario")
        router.fix_nodogsplash_auth_marks()

    def _client_probe(self, client: ClientDriver, path: str) -> dict | None:
        body = client.run_command(f"curl -s -m 10 {self.client_base_url}{path}")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None


class EvidenceRecorderCapture:
    """CaptureDriver over lib/clients/evidence.EvidenceRecorder (phone)."""

    def __init__(self, serial: str = "", recorder_factory=None) -> None:
        self._serial = serial or os.environ.get("PHONE_SERIAL", "")
        self._recorder_factory = recorder_factory
        self._rec = None
        self._frames: list[Path] = []

    def start(self, artifact_dir: Path) -> None:
        artifact_dir = Path(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if self._recorder_factory is None:
            self._recorder_factory = self._default_recorder
        self._rec = self._recorder_factory(artifact_dir)
        self._rec.start_video(limit_s=600)

    def step(self, name: str) -> None:
        if self._rec is None:
            return
        path = self._rec.shot(step=name, claim=name)
        if path:
            self._frames.append(Path(path))

    def stop(self) -> CaptureResult:
        if self._rec is None:
            return CaptureResult()
        video = self._rec.stop_video()
        self._rec.write_manifest()
        return CaptureResult(
            video_path=Path(video) if video else None,
            frame_paths=tuple(self._frames),
        )

    def _default_recorder(self, artifact_dir: Path):
        from lib.clients.evidence import EvidenceRecorder
        from lib.clients.u2phone import connect_u2

        return EvidenceRecorder(connect_u2(self._serial), self._serial, str(artifact_dir))


def register_prta_adapters() -> None:
    """Fill the scenario-layer registry slots with PRTA-backed adapters."""
    register_driver(CLIENT_DRIVERS, "debian_container", DebianContainerClient, required=("ssh",))
    register_driver(CLIENT_DRIVERS, "phone_adb", PhoneAdbClient)
    register_driver(PAYMENT_ACTORS, "token_paste", TokenPasteActor)
    register_driver(PAYMENT_ACTORS, "portal_tip03", PrtaPortalTip03Actor, replace=True)
    register_driver(GATEWAY_DRIVERS, "http_module", PrtaHttpGateway, required=("base",), replace=True)
    register_driver(CAPTURE_DRIVERS, "evidence_recorder", EvidenceRecorderCapture)


register_prta_adapters()
