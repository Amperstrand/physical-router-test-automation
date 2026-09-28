"""Unit tests for lib/clients/wifi.py SSID resolution (mock router SSH)."""
from __future__ import annotations

import re

from lib.clients.wifi import WiFi


class FakeRouter:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.commands: list[str] = []

    def ssh(self, cmd: str, timeout: int = 30) -> str:
        self.commands.append(cmd)
        return self._responses.pop(0) if self._responses else ""


class FakeAdb:
    is_desktop = False


def _wifi(responses, fallback="TollGate", prefixes=None):
    router = FakeRouter(responses)
    wifi = WiFi(adb=FakeAdb(), router=router, ssid=fallback, captive_prefixes=prefixes)
    return wifi, router


class TestPrefixListResolution:
    def test_default_prefix_picks_tollgate_ignoring_private(self):
        wifi, _ = _wifi(['ESSID: "c08r4d0r-C830"\nESSID: "TollGate-0805"\n'])
        assert wifi.ssid == "TollGate-0805"

    def test_net4sats_brand_router(self):
        wifi, _ = _wifi(['ESSID: "Net4sats-C830"\n'], prefixes=["TollGate-", "Net4sats-"])
        assert wifi.ssid == "Net4sats-C830"

    def test_prefixes_normalized_without_trailing_dash(self):
        wifi, _ = _wifi(['ESSID: "Net4sats-C830"\n'], prefixes=["Net4sats-"])
        assert wifi.captive_prefixes == ["Net4sats"]
        assert wifi.ssid == "Net4sats-C830"

    def test_private_ssid_not_matched_by_default_prefixes(self):
        # The old dead 'grep -v private' heuristic is gone; exclusion now
        # happens by prefix classification, so a private-only radio must
        # NOT satisfy a TollGate prefix match.
        wifi, _ = _wifi(['ESSID: "c08r4d0r-C830"\n'])
        assert wifi.ssid == "TollGate"  # fallback

    def test_uci_fallback_when_iwinfo_empty(self):
        wifi, router = _wifi(["", "wireless.radio0.ssid='TollGate-A1B2'\n"])
        assert wifi.ssid == "TollGate-A1B2"
        assert any(cmd.startswith("uci show wireless") for cmd in router.commands)

    def test_fallback_when_nothing_matches(self):
        wifi, _ = _wifi(['ESSID: "SomethingElse"\n', ""])
        assert wifi.ssid == "TollGate"

    def test_ssh_failure_falls_back(self):
        class ExplodingRouter(FakeRouter):
            def ssh(self, cmd, timeout=30):
                raise RuntimeError("unreachable")
        wifi = WiFi(adb=FakeAdb(), router=ExplodingRouter([]), ssid="TollGate")
        assert wifi.ssid == "TollGate"


class TestNoDeadPrivateGrep:
    def test_probe_commands_have_no_grep_v_private(self):
        _, router = _wifi(['ESSID: "TollGate-0805"\n'])
        assert router.commands, "expected SSH probes to be issued"
        for cmd in router.commands:
            assert "grep -v private" not in cmd, f"dead heuristic resurrected: {cmd}"


class TestScanPrefixPattern:
    def test_alternation_matches_any_prefix(self):
        wifi, _ = _wifi([""], prefixes=["TollGate-", "Net4sats-"])
        pattern = wifi._scan_prefix_pattern()
        assert re.search(f'text="({pattern})"', '<node text="Net4sats-C830" />')
        assert re.search(f'text="({pattern})"', '<node text="TollGate-A1B2" />')
        assert not re.search(f'text="({pattern})"', '<node text="c08r4d0r-C830" />')
        assert not re.search(f'text="({pattern})"', '<node text="TollGateX-C830" />')

    def test_prefixes_are_escaped(self):
        wifi, _ = _wifi([""], prefixes=["Br and.X-"])
        pattern = wifi._scan_prefix_pattern()
        assert re.search(f'text="({pattern})"', '<node text="Br and.X-C830" />')
        assert not re.search(f'text="({pattern})"', '<node text="BrXandCX-C830" />')


class TestBackwardCompatAttrs:
    def test_ssid_prefix_is_first_configured_prefix(self):
        wifi, _ = _wifi([""], prefixes=["Net4sats-", "TollGate-"])
        assert wifi.ssid_prefix == "Net4sats"

    def test_default_prefixes_from_env(self, monkeypatch):
        monkeypatch.delenv("TOLLGATE_CAPTIVE_SSID_PREFIXES", raising=False)
        monkeypatch.delenv("TOLLGATE_SSID_PREFIX", raising=False)
        wifi, _ = _wifi([""])
        assert wifi.captive_prefixes == ["TollGate"]
