"""Unit tests for the PRTA scenario adapters — composition with fakes.

The phone is off at night and the portal state machine module pulls in
uiautomator2, so the phone-path adapters are exercised through injectable
fakes (machine/recorder/device); the gateway parses run against canned
client.run_command output matching the live backend shapes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.scenario.adapters import (
    DebianContainerClient,
    PhoneAdbClient,
    PrtaHttpGateway,
    PrtaPortalTip03Actor,
    TokenPasteActor,
    register_prta_adapters,
)
from tollgate_lab.scenarios.contract import PayReceipt, SessionState, Usage
from tollgate_lab.scenarios.drivers import (
    CAPTURE_DRIVERS,
    CLIENT_DRIVERS,
    GATEWAY_DRIVERS,
    PAYMENT_ACTORS,
)
from tollgate_lab.scenarios.profile import load_profile

HERE = Path(__file__).parent

BALANCE_LIVE = json.dumps(
    {"status": 1, "session_active": True, "metric": "bytes", "usage": 0, "allotment": 176160768, "remaining": 176160768}
)
BALANCE_VOID = json.dumps({"status": 0, "session_active": False, "usage": 0, "allotment": 0})


class FakeClient:
    name = "fake"

    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.commands: list[str] = []

    def run_command(self, command: str, *, timeout_s: int = 15) -> str:
        self.commands.append(command)
        for prefix, out in self.responses.items():
            if prefix in command:
                return out
        return ""

    @property
    def staged_token(self) -> str:
        return self.run_command("cat /tmp/tg-scenario-token").strip()


class FakeGateway:
    base_url = "http://10.99.99.1:2121"
    last_sats = 4

    def open_gate_repair(self) -> None:
        self.repaired = True


def test_registry_slots_filled():
    register_prta_adapters()
    assert CLIENT_DRIVERS["debian_container"].factory is DebianContainerClient
    assert CLIENT_DRIVERS["phone_adb"].factory is PhoneAdbClient
    assert PAYMENT_ACTORS["token_paste"].factory is TokenPasteActor
    assert PAYMENT_ACTORS["portal_tip03"].factory is PrtaPortalTip03Actor
    assert GATEWAY_DRIVERS["http_module"].factory is PrtaHttpGateway
    assert CAPTURE_DRIVERS["evidence_recorder"].factory is not None


def test_profiles_validate_after_registration():
    register_prta_adapters()
    for name in ("debian-container-real.yaml", "phone-adb-mt3000.yaml"):
        profile = load_profile(HERE / "profiles" / name)
        assert profile.name == Path(name).stem


def test_gateway_usage_parses_live_and_void_shapes():
    gw = PrtaHttpGateway(base="http://10.99.99.1:2121")
    live = FakeClient({"/usage": "0/176160768"})
    void = FakeClient({"/usage": "-1/-1"})
    garbage = FakeClient({"/usage": "<html>502 from proxy</html>"})

    assert gw.usage(live) == Usage(used=0, allotment=176160768)
    assert gw.usage(void) == Usage()
    assert gw.usage(void).is_void
    assert gw.usage(garbage) == Usage()

    active = FakeClient({"/balance": BALANCE_LIVE})
    inactive = FakeClient({"/balance": BALANCE_VOID})
    assert gw.session_state(active) == SessionState("active")
    assert gw.session_state(inactive) == SessionState("none")


def test_gateway_external_reachable_requires_http_success():
    gw = PrtaHttpGateway(base="http://10.99.99.1:2121")
    probe = "-w '%{http_code} %{redirect_url}' http://1.1.1.1/"
    internet = FakeClient({probe: "301 https://one.one.one.one/"})
    portal = FakeClient({probe: "307 http://10.99.99.1:2050/splash.html?redir=http%3a%2f%2f1.1.1.1%2f"})
    dead = FakeClient({probe: "000 "})

    assert gw.external_reachable(internet) is True
    assert gw.external_reachable(portal) is False
    assert gw.external_reachable(dead) is False


def test_gateway_advertisement_sources_ssid_out_of_band(monkeypatch):
    gw = PrtaHttpGateway(base="http://10.99.99.1:2121")

    class FakeResponse:
        def json(self):
            return {
                "kind": 10021,
                "tags": [
                    ["metric", "bytes"],
                    ["price_per_step", "cashu", "1", "sats", "http://10.99.99.2:8383", "0"],
                ],
            }

    monkeypatch.setattr("requests.get", lambda url, timeout: FakeResponse(), raising=False)
    monkeypatch.setattr("lib.clients.ssid.resolve_ssid", lambda host: "TollGate-ALPHA", raising=False)

    adv = gw.advertisement()

    assert adv["ssid"] == "TollGate-ALPHA"
    assert adv["kind"] == 10021
    assert gw._adv_mint_url == "http://10.99.99.2:8383"


def test_token_paste_actor_posts_staged_token():
    client = FakeClient(
        {
            "cat /tmp/tg-scenario-token": "cashuAUnit",
            "-X POST": '{"kind": 1022, "allotment": 44040192}',
        }
    )
    gw = FakeGateway()

    receipt = TokenPasteActor().pay(client, gw)

    assert receipt == PayReceipt(sats=4, strategy="token_paste", token="cashuAUnit")
    assert gw.repaired is True
    assert any(":2050/" in c for c in client.commands), "must prime NDS portal first"
    assert any("--data-binary @/tmp/tg-scenario-token" in c for c in client.commands)


def test_token_paste_actor_raises_on_rejection():
    client = FakeClient(
        {
            "cat /tmp/tg-scenario-token": "cashuAUnit",
            "-X POST": '{"kind": 21005, "error": "token already spent"}',
        }
    )
    with pytest.raises(RuntimeError, match="rejected"):
        TokenPasteActor().pay(client, FakeGateway())


class FakeU2:
    def __init__(self, state: str = "authed"):
        self.state = state
        self.calls: list[str] = []

    def ensure_awake(self):
        self.calls.append("awake")

    def ensure_unlocked(self):
        self.calls.append("unlocked")

    def open_url(self, url):
        self.calls.append(f"url:{url}")


class FakePhoneClient(PhoneAdbClient):
    def __init__(self, fake_u2: FakeU2):
        super().__init__(serial="fake")
        self._device = fake_u2

    @property
    def staged_token(self) -> str:
        return "cashuBUnit"


def test_portal_actor_wraps_the_prta_state_machine():
    fake_u2 = FakeU2("countdown")
    client = FakePhoneClient(fake_u2)
    seen = {}

    def fake_machine(u2phone, token, timeout=75):
        seen["token"] = token
        seen["timeout"] = timeout
        return "countdown"

    actor = PrtaPortalTip03Actor(portal="http://192.168.1.1:2050/", timeout_s=45)
    actor.machine = fake_machine

    receipt = actor.pay(client, FakeGateway())

    assert receipt.strategy == "portal_tip03"
    assert receipt.token == "cashuBUnit"
    assert seen == {"token": "cashuBUnit", "timeout": 45}
    assert "url:http://192.168.1.1:2050/" in fake_u2.calls


def test_portal_actor_raises_when_portal_stalls():
    client = FakePhoneClient(FakeU2("token_typing"))
    actor = PrtaPortalTip03Actor()
    actor.machine = lambda u2phone, token, timeout=75: "token_typing"

    with pytest.raises(RuntimeError, match="did not authenticate"):
        actor.pay(client, FakeGateway())


def test_container_client_ssid_maps_to_default_route():
    responses = {
        "ip -4 addr show": "2",
        "ip route show default": "default via 10.99.99.1 dev ens3",
    }
    client = DebianContainerClient(ssh="ssh debian@10.99.99.100")
    client.run_command = FakeClient(responses).run_command

    client.connect_wifi("TollGate-ALPHA", timeout_s=1)

    assert client.active_ssid() == "TollGate-ALPHA"

    responses["ip route show default"] = ""
    assert client.active_ssid() is None
