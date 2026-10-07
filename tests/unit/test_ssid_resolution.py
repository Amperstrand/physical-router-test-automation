"""Unit tests for lib/ssid.py — runtime SSID resolution (mock SSH only)."""
from __future__ import annotations

import pytest

from lib.ssid import (
    DEFAULT_CAPTIVE_PREFIX,
    DEFAULT_PRIVATE_PREFIX,
    LEGACY_RESELLER_SSID,
    captive_prefixes_from_env,
    classify_ssid,
    expected_portal_banner,
    extract_ssids,
    matches_prefix,
    normalize_prefix,
    prefixes_from_inventory,
    private_prefixes_from_env,
    probe_router_identity,
    resolve_reseller_ssid,
)


pytestmark = [pytest.mark.story("P11")]

class TestNormalizePrefix:
    def test_strips_trailing_dash(self):
        assert normalize_prefix("TollGate-") == "TollGate"

    def test_strips_surrounding_whitespace(self):
        assert normalize_prefix("  Net4sats ") == "Net4sats"

    def test_empty(self):
        assert normalize_prefix("") == ""
        assert normalize_prefix("-") == ""


class TestCaptivePrefixesFromEnv:
    def test_default(self):
        assert captive_prefixes_from_env({}) == [DEFAULT_CAPTIVE_PREFIX]

    def test_legacy_ssid_prefix_env(self):
        assert captive_prefixes_from_env({"TOLLGATE_SSID_PREFIX": "TollGate-"}) == ["TollGate"]

    def test_comma_separated_list(self):
        env = {"TOLLGATE_CAPTIVE_SSID_PREFIXES": "TollGate-, Net4sats"}
        assert captive_prefixes_from_env(env) == ["TollGate", "Net4sats"]

    def test_list_env_wins_over_legacy(self):
        env = {
            "TOLLGATE_CAPTIVE_SSID_PREFIXES": "Net4sats",
            "TOLLGATE_SSID_PREFIX": "TollGate-",
        }
        assert captive_prefixes_from_env(env) == ["Net4sats"]


class TestPrivatePrefixesFromEnv:
    def test_default_nym(self):
        assert private_prefixes_from_env({}) == [DEFAULT_PRIVATE_PREFIX]

    def test_override(self):
        assert private_prefixes_from_env({"TOLLGATE_PRIVATE_SSID_PREFIX": "acme"}) == ["acme"]


class TestPrefixesFromInventory:
    def test_inventory_absent_falls_back_to_env(self):
        captive, private = prefixes_from_inventory({}, {"TOLLGATE_SSID_PREFIX": "TollGate-"})
        assert captive == ["TollGate"]
        assert private == [DEFAULT_PRIVATE_PREFIX]

    def test_captive_list_field(self):
        entry = {"captiveSsidPrefixes": ["TollGate-", "Net4sats-"]}
        captive, private = prefixes_from_inventory(entry, {})
        assert captive == ["TollGate", "Net4sats"]
        assert private == [DEFAULT_PRIVATE_PREFIX]

    def test_captive_string_field(self):
        captive, _ = prefixes_from_inventory({"captiveSsidPrefixes": "TollGate-,Net4sats"}, {})
        assert captive == ["TollGate", "Net4sats"]

    def test_legacy_tollgate_ssid_prefix_still_honored(self):
        captive, _ = prefixes_from_inventory({"tollgateSsidPrefix": "Net4sats-"}, {})
        assert captive == ["Net4sats"]

    def test_private_prefix_field(self):
        _, private = prefixes_from_inventory({"privateSsidPrefix": "acme"}, {})
        assert private == ["acme"]


class TestMatchesPrefix:
    def test_exact(self):
        assert matches_prefix("TollGate", "TollGate")

    def test_dash_suffix(self):
        assert matches_prefix("TollGate-A1B2", "TollGate")

    def test_no_partial_prefix(self):
        assert not matches_prefix("TollGateX-A1B2", "TollGate")

    def test_no_middle_match(self):
        assert not matches_prefix("xTollGate-A1B2", "TollGate")


class TestClassifySsid:
    def test_default_captive(self):
        assert classify_ssid("TollGate-0805") == "captive"

    def test_branded_captive_in_list(self):
        assert classify_ssid("Net4sats-C830", ["TollGate", "Net4sats"]) == "captive"

    def test_branded_captive_not_in_list(self):
        assert classify_ssid("Net4sats-C830", ["TollGate"]) == "other"

    def test_private(self):
        assert classify_ssid("c08r4d0r-C830") == "private"

    def test_private_checked_before_captive(self):
        # Overlapping prefixes: the nym is more specific than the brand.
        assert classify_ssid("c08r4d0r-1A2B", ["c08r4d0r"], ["c08r4d0r"]) == "private"

    def test_other(self):
        assert classify_ssid("HomeNetwork") == "other"


class TestExtractSsids:
    def test_iwinfo_output(self):
        out = (
            'phy0-ap0  ESSID: "TollGate-A1B2"\n'
            'phy1-ap0  ESSID: "c08r4d0r-A1B2"\n'
        )
        assert extract_ssids(out) == ["TollGate-A1B2", "c08r4d0r-A1B2"]

    def test_uci_output(self):
        out = (
            "wireless.default_radio0.ssid='TollGate-A1B2'\n"
            'wireless.default_radio1.ssid="c08r4d0r-A1B2"\n'
        )
        assert extract_ssids("", out) == ["TollGate-A1B2", "c08r4d0r-A1B2"]

    def test_dedup_and_skip_empty(self):
        out = 'ESSID: ""\nESSID: "TollGate-A1B2"\nESSID: "TollGate-A1B2"\n'
        assert extract_ssids(out) == ["TollGate-A1B2"]

    def test_no_private_filtering_in_parser(self):
        # The old dead heuristic (grep -v private) must not live here either.
        out = 'ESSID: "private-net"\nESSID: "TollGate-A1B2"\n'
        assert "private-net" in extract_ssids(out)


class TestProbeRouterIdentity:
    @staticmethod
    def _run_ssh_factory(responses: dict):
        def run_ssh(cmd: str) -> str:
            return responses.get(cmd, "")
        return run_ssh

    def test_code_derived_ssids_preferred(self):
        run_ssh = self._run_ssh_factory({
            "uci -q get tollgate.device.code": "A1B2\n",
            "iwinfo 2>/dev/null | grep ESSID": 'ESSID: "TollGate-A1B2"\nESSID: "c08r4d0r-A1B2"\n',
            "uci show wireless 2>/dev/null | grep '\\.ssid='": "",
        })
        ident = probe_router_identity(run_ssh)
        assert ident.device_code == "A1B2"
        assert ident.code_valid
        assert ident.captive_ssid == "TollGate-A1B2"
        assert ident.private_ssid == "c08r4d0r-A1B2"

    def test_classification_fallback_without_code(self):
        run_ssh = self._run_ssh_factory({
            "iwinfo 2>/dev/null | grep ESSID": 'ESSID: "c08r4d0r-C830"\nESSID: "Net4sats-C830"\n',
        })
        ident = probe_router_identity(run_ssh, ["TollGate", "Net4sats"])
        assert ident.device_code == ""
        assert not ident.code_valid
        assert ident.captive_ssid == "Net4sats-C830"
        assert ident.private_ssid == "c08r4d0r-C830"

    def test_no_match_leaves_empty(self):
        run_ssh = self._run_ssh_factory({
            "iwinfo 2>/dev/null | grep ESSID": 'ESSID: "SomeOtherNetwork"\n',
        })
        ident = probe_router_identity(run_ssh)
        assert ident.ssids == ["SomeOtherNetwork"]
        assert ident.captive_ssid == ""
        assert ident.private_ssid == ""

    def test_invalid_code_still_reported(self):
        run_ssh = self._run_ssh_factory({
            "uci -q get tollgate.device.code": "toolongcode\n",
        })
        ident = probe_router_identity(run_ssh)
        assert ident.device_code == "toolongcode"
        assert not ident.code_valid


class TestPortalBanner:
    def test_decided_writer_shape(self):
        assert expected_portal_banner("TollGate-1A2B") == "TollGate-1A2B Portal"

    def test_branded(self):
        assert expected_portal_banner("Net4sats-C830") == "Net4sats-C830 Portal"


class TestResolveResellerSsid:
    def test_env_override_wins(self):
        def exploding(cmd):
            raise RuntimeError("should not be called")
        assert resolve_reseller_ssid(exploding, env={"RESELLER_SSID": "manual-ssid"}) == "manual-ssid"

    def test_router_resolution(self):
        def run_ssh(cmd):
            if cmd.startswith("uci -q get"):
                return "C830\n"
            if cmd.startswith("iwinfo"):
                return 'ESSID: "c08r4d0r-C830"\n'
            return ""
        assert resolve_reseller_ssid(run_ssh, env={}) == "c08r4d0r-C830"

    def test_ssh_failure_falls_back_to_legacy(self):
        def run_ssh(cmd):
            raise RuntimeError("connection refused")
        assert resolve_reseller_ssid(run_ssh, env={}) == LEGACY_RESELLER_SSID

    def test_no_private_ssid_falls_back(self):
        def run_ssh(cmd):
            if cmd.startswith("iwinfo"):
                return 'ESSID: "TollGate-C830"\n'
            return ""
        assert resolve_reseller_ssid(run_ssh, env={}) == LEGACY_RESELLER_SSID
