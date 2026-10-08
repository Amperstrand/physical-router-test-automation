"""Unit tests for lib/session_verify.py — read-only session verification probes.

These run without a router: they exercise the parsing logic, regex matching,
and probe orchestration with mock router objects. The live path is exercised
by tests/scenarios/ and tests/api/ on real hardware.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from lib.session_verify import (
    SessionVerification,
    _SESSION_LOG_RE,
    _extract_allotment,
    _has_allotment_tag,
    _parse_ndsctl_json,
    check_backend_logs,
    check_balance_api,
    check_ndsctl,
    check_usage_api,
    snapshot_session,
    verify_session,
)

MAC = "AA:BB:CC:DD:EE:FF"
MAC_LOWER = "aa:bb:cc:dd:ee:ff"
IP = "192.168.1.100"


# --------------------------------------------------------------------------- #
# Mock router
# --------------------------------------------------------------------------- #


pytestmark = [pytest.mark.story("P1")]

class MockRouter:
    """Minimal duck-typed Router for unit-testing the probes."""

    def __init__(
        self,
        logs: str = "",
        ndsctl_json: str = "",
        nds_state: str = "",
        balance_resp: str = "",
        usage_resp: str = "",
        ssh_responses: dict | None = None,
        phone_mac: str = MAC,
        phone_ip: str = IP,
    ):
        self._logs = logs
        self._ndsctl_json = ndsctl_json
        self._nds_state = nds_state
        self._balance_resp = balance_resp
        self._usage_resp = usage_resp
        self._ssh_responses = ssh_responses or {}
        self.phone_mac = phone_mac
        self.phone_ip = phone_ip

    def get_tollgate_logs(self, filter_expr="tollgate", lines=200):
        return self._logs

    def backend_url(self, path="/"):
        return f"http://[::1]:2121{path}"

    def backend_curl_xff(self, path, ip=None, method=None, headers=None, data=None):
        if "/balance" in path:
            return self._balance_resp
        if "/usage" in path:
            return self._usage_resp
        return ""

    def ssh(self, cmd, timeout=30):
        # Return canned responses for ndsctl commands
        if "ndsctl json" in cmd:
            return self._ndsctl_json
        return self._ssh_responses.get(cmd, "")

    def get_nds_state(self, mac=None):
        return self._nds_state


# --------------------------------------------------------------------------- #
# _parse_ndsctl_json
# --------------------------------------------------------------------------- #


class TestParseNdsctlJson:
    def test_dict_with_clients_key(self):
        raw = json.dumps({"clients": {MAC_LOWER: {"mac": MAC_LOWER, "state": "Authenticated"}}})
        state, evidence = _parse_ndsctl_json(raw, MAC)
        assert state == "Authenticated"
        assert MAC_LOWER in evidence

    def test_list_top_level(self):
        raw = json.dumps([{"mac": MAC_LOWER, "state": "Authenticated"}])
        state, _ = _parse_ndsctl_json(raw, MAC)
        assert state == "Authenticated"

    def test_client_list_key(self):
        raw = json.dumps({"client_list": [{"mac": MAC_LOWER, "state": "Preauthenticated"}]})
        state, _ = _parse_ndsctl_json(raw, MAC)
        assert state == "Preauthenticated"

    def test_clients_as_dict_values(self):
        raw = json.dumps({"clients": {"cl1": {"mac": MAC_LOWER, "state": "Authenticated"}}})
        state, _ = _parse_ndsctl_json(raw, MAC)
        assert state == "Authenticated"

    def test_mac_case_insensitive(self):
        raw = json.dumps({"clients": {"x": {"mac": "AA:BB:CC:DD:EE:FF", "state": "Authenticated"}}})
        state, _ = _parse_ndsctl_json(raw, "aa:bb:cc:dd:ee:ff")
        assert state == "Authenticated"

    def test_mac_nocolon_match(self):
        raw = json.dumps({"clients": {"x": {"mac": "aabbccddeeff", "state": "Authenticated"}}})
        state, _ = _parse_ndsctl_json(raw, "AA:BB:CC:DD:EE:FF")
        assert state == "Authenticated"

    def test_alternative_mac_keys(self):
        """Client dict may use 'hw' or 'macaddr' instead of 'mac'."""
        for key in ("hw", "macaddr"):
            raw = json.dumps({"clients": {"x": {key: MAC_LOWER, "state": "Authenticated"}}})
            state, _ = _parse_ndsctl_json(raw, MAC)
            assert state == "Authenticated", f"failed for key={key}"

    def test_alternative_state_keys(self):
        """Client dict may use 'status' instead of 'state'."""
        raw = json.dumps({"clients": {"x": {"mac": MAC_LOWER, "status": "Authenticated"}}})
        state, _ = _parse_ndsctl_json(raw, MAC)
        assert state == "Authenticated"

    def test_mac_not_found(self):
        raw = json.dumps({"clients": {"x": {"mac": "11:22:33:44:55:66", "state": "Authenticated"}}})
        state, evidence = _parse_ndsctl_json(raw, MAC)
        assert state == ""
        assert "not found" in evidence

    def test_no_clients(self):
        raw = json.dumps({"clients": {}})
        state, evidence = _parse_ndsctl_json(raw, MAC)
        assert state == ""
        assert "no clients" in evidence

    def test_invalid_json(self):
        state, evidence = _parse_ndsctl_json("not json at all", MAC)
        assert state == ""
        assert "invalid json" in evidence

    def test_unexpected_clients_shape(self):
        raw = json.dumps({"clients": "not a dict or list"})
        state, evidence = _parse_ndsctl_json(raw, MAC)
        assert state == ""
        assert "unexpected" in evidence

    def test_no_mac_filter_returns_first_with_state(self):
        """When mac=None, return the first client that has a state."""
        raw = json.dumps([{"mac": "11:22:33:44:55:66", "state": "Authenticated"}])
        state, _ = _parse_ndsctl_json(raw, None)
        assert state == "Authenticated"

    def test_non_dict_client_skipped(self):
        raw = json.dumps({"clients": ["string_item", {"mac": MAC_LOWER, "state": "Authenticated"}]})
        state, _ = _parse_ndsctl_json(raw, MAC)
        assert state == "Authenticated"

    def test_empty_state_skipped(self):
        """Client with empty state string is skipped, not returned."""
        raw = json.dumps({"clients": {"a": {"mac": MAC_LOWER, "state": ""}}})
        state, evidence = _parse_ndsctl_json(raw, MAC)
        assert state == ""

    def test_evidence_truncated(self):
        """Evidence string is truncated to 200 chars."""
        long_value = "x" * 500
        raw = json.dumps({"clients": {"x": {"mac": MAC_LOWER, "state": "Authenticated", "data": long_value}}})
        _, evidence = _parse_ndsctl_json(raw, MAC)
        assert len(evidence) <= 200


# --------------------------------------------------------------------------- #
# _extract_allotment
# --------------------------------------------------------------------------- #


class TestExtractAllotment:
    def test_allotment_key(self):
        assert _extract_allotment({"allotment": 1000}) == 1000

    def test_remaining_key(self):
        assert _extract_allotment({"remaining": 500}) == 500

    def test_download_limit_key(self):
        assert _extract_allotment({"download_limit": 200}) == 200

    def test_upload_limit_key(self):
        assert _extract_allotment({"upload_limit": 100}) == 100

    def test_zero_allotment_returns_zero(self):
        assert _extract_allotment({"allotment": 0}) == 0

    def test_negative_returns_zero(self):
        assert _extract_allotment({"allotment": -5}) == 0

    def test_first_positive_key_wins(self):
        """When multiple keys present, first in priority order wins."""
        result = _extract_allotment({"allotment": 10, "remaining": 20})
        assert result == 10

    def test_tag_allotment(self):
        assert _extract_allotment({"tags": [["allotment", "2000"]]}) == 2000

    def test_tag_allotment_invalid_value(self):
        assert _extract_allotment({"tags": [["allotment", "not_a_num"]]}) == 0

    def test_tag_allotment_empty(self):
        assert _extract_allotment({"tags": [["allotment"]]}) == 0

    def test_empty_dict(self):
        assert _extract_allotment({}) == 0

    def test_non_dict(self):
        assert _extract_allotment("string") == 0
        assert _extract_allotment(None) == 0
        assert _extract_allotment([]) == 0

    def test_float_value_converted_to_int(self):
        assert _extract_allotment({"allotment": 100.5}) == 100

    def test_string_value_ignored(self):
        assert _extract_allotment({"allotment": "1000"}) == 0


# --------------------------------------------------------------------------- #
# _has_allotment_tag
# --------------------------------------------------------------------------- #


class TestHasAllotmentTag:
    def test_present(self):
        assert _has_allotment_tag({"tags": [["allotment", "100"]]}) is True

    def test_absent(self):
        assert _has_allotment_tag({"tags": [["other", "val"]]}) is False

    def test_empty_tags(self):
        assert _has_allotment_tag({"tags": []}) is False

    def test_no_tags_key(self):
        assert _has_allotment_tag({}) is False

    def test_non_dict(self):
        assert _has_allotment_tag("string") is False


# --------------------------------------------------------------------------- #
# SessionVerification
# --------------------------------------------------------------------------- #


class TestSessionVerification:
    def test_default_all_false(self):
        sv = SessionVerification()
        assert sv.any_success is False

    def test_backend_logs(self):
        sv = SessionVerification(backend_logs=True)
        assert sv.any_success is True

    def test_ndsctl(self):
        sv = SessionVerification(ndsctl_authenticated=True)
        assert sv.any_success is True

    def test_balance(self):
        sv = SessionVerification(balance_session_active=True)
        assert sv.any_success is True

    def test_usage(self):
        sv = SessionVerification(usage_allotment=500)
        assert sv.any_success is True

    def test_usage_zero_is_false(self):
        sv = SessionVerification(usage_allotment=0)
        assert sv.any_success is False

    def test_summary_contains_all_fields(self):
        sv = SessionVerification(
            backend_logs=True,
            ndsctl_state="Authenticated",
            balance_session_active=True,
            usage_allotment=42,
        )
        s = sv.summary()
        assert "backend_logs=True" in s
        assert "ndsctl=Authenticated" in s
        assert "balance_active=True" in s
        assert "usage_allotment=42" in s

    def test_summary_empty_ndsctl_shows_na(self):
        sv = SessionVerification()
        assert "ndsctl=n/a" in sv.summary()


# --------------------------------------------------------------------------- #
# _SESSION_LOG_RE
# --------------------------------------------------------------------------- #


class TestSessionLogRegex:
    @pytest.mark.parametrize("line", [
        "PurchaseSession for client aa:bb:cc:dd:ee:ff",
        "session created for 192.168.1.100",
        "session started",
        "SESSION ACTIVE",
        "session established",
        "authorized client 192.168.1.100",
        "client aa:bb:cc:dd:ee:ff authenticated",
        "created session for client",
        "payment accepted",
        "payment received from client",
        "payment processed successfully",
        "allotment granted: 1000 bytes",
    ])
    def test_matches(self, line):
        assert _SESSION_LOG_RE.search(line) is not None

    @pytest.mark.parametrize("line", [
        "unrelated log line",
        "error connecting to mint",
        "client disconnected",
        "configuration loaded",
        "",
    ])
    def test_no_match(self, line):
        assert _SESSION_LOG_RE.search(line) is None

    def test_case_insensitive(self):
        assert _SESSION_LOG_RE.search("purchasesession") is not None
        assert _SESSION_LOG_RE.search("AUTHORIZED CLIENT") is not None


# --------------------------------------------------------------------------- #
# check_backend_logs
# --------------------------------------------------------------------------- #


class TestCheckBackendLogs:
    def test_mac_matching_line(self):
        logs = f"some log\nPurchaseSession for client {MAC_LOWER}\nother line"
        router = MockRouter(logs=logs)
        ok, evidence = check_backend_logs(router, mac=MAC)
        assert ok is True
        assert MAC_LOWER in evidence

    def test_mac_nocolon_matching(self):
        """MAC without colons in log line should still match."""
        logs = "session created for aabbccddeeff"
        router = MockRouter(logs=logs)
        ok, evidence = check_backend_logs(router, mac=MAC)
        assert ok is True

    def test_fallback_any_matching_line(self):
        """When no MAC-specific line, falls back to any matching line."""
        logs = "session created for other_client"
        router = MockRouter(logs=logs)
        ok, evidence = check_backend_logs(router, mac=MAC)
        assert ok is True
        assert "session created" in evidence

    def test_no_match(self):
        logs = "some unrelated log\nanother line"
        router = MockRouter(logs=logs)
        ok, evidence = check_backend_logs(router, mac=MAC)
        assert ok is False

    def test_empty_logs(self):
        router = MockRouter(logs="")
        ok, evidence = check_backend_logs(router)
        assert ok is False
        assert "empty" in evidence

    def test_get_tollgate_logs_exception(self):
        router = MockRouter()
        router.get_tollgate_logs = MagicMock(side_effect=RuntimeError("SSH failed"))
        ok, evidence = check_backend_logs(router)
        assert ok is False
        assert "logread error" in evidence

    def test_evidence_truncated(self):
        long_line = "session created " + "x" * 500
        router = MockRouter(logs=long_line)
        ok, evidence = check_backend_logs(router)
        assert ok is True
        assert len(evidence) <= 200

    def test_no_mac_finds_any_match(self):
        logs = "payment accepted"
        router = MockRouter(logs=logs)
        ok, evidence = check_backend_logs(router, mac=None)
        assert ok is True


# --------------------------------------------------------------------------- #
# check_balance_api
# --------------------------------------------------------------------------- #


class TestCheckBalanceApi:
    def test_session_active_true(self):
        router = MockRouter(balance_resp='{"session_active": true}')
        ok, _ = check_balance_api(router)
        assert ok is True

    def test_remaining_positive(self):
        router = MockRouter(balance_resp='{"remaining": 500}')
        ok, _ = check_balance_api(router)
        assert ok is True

    def test_allotment_positive(self):
        router = MockRouter(balance_resp='{"allotment": 1000}')
        ok, _ = check_balance_api(router)
        assert ok is True

    def test_kind_1022(self):
        router = MockRouter(balance_resp='{"kind": 1022}')
        ok, _ = check_balance_api(router)
        assert ok is True

    def test_allotment_tag(self):
        router = MockRouter(balance_resp='{"tags": [["allotment", "500"]]}')
        ok, _ = check_balance_api(router)
        assert ok is True

    def test_no_active_session(self):
        router = MockRouter(balance_resp='{"session_active": false, "remaining": 0}')
        ok, _ = check_balance_api(router)
        assert ok is False

    def test_empty_response(self):
        router = MockRouter(balance_resp="")
        ok, evidence = check_balance_api(router)
        assert ok is False
        assert "empty" in evidence

    def test_non_json_response(self):
        router = MockRouter(balance_resp="not json")
        ok, evidence = check_balance_api(router)
        assert ok is False
        assert "non-json" in evidence

    def test_exception(self):
        router = MockRouter()
        router.backend_curl_xff = MagicMock(side_effect=RuntimeError("fail"))
        ok, evidence = check_balance_api(router)
        assert ok is False
        assert "balance error" in evidence

    def test_uses_phone_ip_default(self):
        router = MockRouter(balance_resp='{"session_active": true}')
        check_balance_api(router)
        # Should not crash — phone_ip is the default

    def test_custom_ip(self):
        router = MockRouter(balance_resp='{"session_active": true}')
        check_balance_api(router, ip="10.0.0.5")
        # Should not crash


# --------------------------------------------------------------------------- #
# check_usage_api
# --------------------------------------------------------------------------- #


class TestCheckUsageApi:
    def test_allotment_positive(self):
        router = MockRouter(usage_resp='{"allotment": 1000}')
        ok, _ = check_usage_api(router)
        assert ok is True

    def test_allotment_zero(self):
        router = MockRouter(usage_resp='{"allotment": 0}')
        ok, _ = check_usage_api(router)
        assert ok is False

    def test_tag_allotment(self):
        router = MockRouter(usage_resp='{"tags": [["allotment", "500"]]}')
        ok, _ = check_usage_api(router)
        assert ok is True

    def test_empty_response(self):
        router = MockRouter(usage_resp="")
        ok, evidence = check_usage_api(router)
        assert ok is False
        assert "empty" in evidence

    def test_non_json(self):
        router = MockRouter(usage_resp="<<<html>>>")
        ok, evidence = check_usage_api(router)
        assert ok is False
        assert "non-json" in evidence

    def test_exception(self):
        router = MockRouter()
        router.backend_curl_xff = MagicMock(side_effect=RuntimeError("fail"))
        ok, evidence = check_usage_api(router)
        assert ok is False
        assert "usage error" in evidence


# --------------------------------------------------------------------------- #
# check_ndsctl
# --------------------------------------------------------------------------- #


class TestCheckNdsctl:
    def test_json_authenticated(self):
        ndsctl = json.dumps({"clients": {MAC_LOWER: {"mac": MAC_LOWER, "state": "Authenticated"}}})
        router = MockRouter(ndsctl_json=ndsctl)
        ok, evidence = check_ndsctl(router, mac=MAC)
        assert ok is True

    def test_json_preauthenticated(self):
        ndsctl = json.dumps({"clients": {MAC_LOWER: {"mac": MAC_LOWER, "state": "Preauthenticated"}}})
        router = MockRouter(ndsctl_json=ndsctl)
        ok, _ = check_ndsctl(router, mac=MAC)
        assert ok is False

    def test_fallback_to_get_nds_state(self):
        """When ndsctl json returns nothing, falls back to get_nds_state."""
        router = MockRouter(ndsctl_json="", nds_state="Authenticated")
        ok, _ = check_ndsctl(router, mac=MAC)
        assert ok is True

    def test_fallback_preauthenticated(self):
        router = MockRouter(ndsctl_json="", nds_state="Preauthenticated")
        ok, _ = check_ndsctl(router, mac=MAC)
        assert ok is False

    def test_no_client_found(self):
        router = MockRouter(ndsctl_json="", nds_state="")
        ok, evidence = check_ndsctl(router, mac=MAC)
        assert ok is False

    def test_uses_phone_mac_default(self):
        ndsctl = json.dumps({"clients": {MAC_LOWER: {"mac": MAC_LOWER, "state": "Authenticated"}}})
        router = MockRouter(ndsctl_json=ndsctl, phone_mac=MAC)
        ok, _ = check_ndsctl(router)
        assert ok is True


# --------------------------------------------------------------------------- #
# snapshot_session
# --------------------------------------------------------------------------- #


class TestSnapshotSession:
    def test_all_probes_succeed(self):
        ndsctl = json.dumps({"clients": {MAC_LOWER: {"mac": MAC_LOWER, "state": "Authenticated"}}})
        router = MockRouter(
            logs=f"PurchaseSession for {MAC_LOWER}",
            ndsctl_json=ndsctl,
            nds_state="Authenticated",
            balance_resp='{"session_active": true}',
            usage_resp='{"allotment": 1000}',
        )
        result = snapshot_session(router, mac=MAC, ip=IP)
        assert result.any_success is True
        assert result.backend_logs is True
        assert result.ndsctl_authenticated is True
        assert result.balance_session_active is True
        assert result.usage_allotment == 1  # snapshot stores 1/0, not raw value

    def test_no_probes_succeed(self):
        router = MockRouter(
            logs="unrelated log",
            ndsctl_json="",
            nds_state="",
            balance_resp='{"session_active": false}',
            usage_resp='{"allotment": 0}',
        )
        result = snapshot_session(router, mac=MAC, ip=IP)
        assert result.any_success is False

    def test_one_probe_sufficient(self):
        """Only backend_logs matches — any_success should be True."""
        router = MockRouter(logs="session created", ndsctl_json="", balance_resp="{}", usage_resp="{}")
        result = snapshot_session(router, mac=MAC, ip=IP)
        assert result.any_success is True
        assert result.backend_logs is True

    def test_probe_exception_does_not_crash(self):
        """If a probe raises unexpectedly, snapshot should still return."""
        router = MockRouter()
        router.get_tollgate_logs = MagicMock(side_effect=Exception("boom"))
        result = snapshot_session(router, mac=MAC, ip=IP)
        assert isinstance(result, SessionVerification)


# --------------------------------------------------------------------------- #
# verify_session
# --------------------------------------------------------------------------- #


class TestVerifySession:
    def test_immediate_success(self):
        router = MockRouter(
            logs="session created",
            balance_resp='{"session_active": true}',
        )
        result = verify_session(router, mac=MAC, ip=IP, timeout=2, poll_interval=0.1)
        assert result.any_success is True

    def test_timeout_no_session(self):
        router = MockRouter(logs="nothing relevant", balance_resp="{}", usage_resp="{}")
        result = verify_session(router, mac=MAC, ip=IP, timeout=1, poll_interval=0.1)
        assert result.any_success is False

    def test_returns_session_verification(self):
        router = MockRouter()
        result = verify_session(router, mac=MAC, ip=IP, timeout=0, poll_interval=0.1)
        assert isinstance(result, SessionVerification)
