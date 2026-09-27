"""Unit tests for lib/helpers.py — pure-function helpers that need no router.

Tests the data-parsing and decision functions (is_session_event, is_mac_lookup_failure,
parse_json_or_fail, gate_bug_fix, is_full_merchant, is_degraded) using mock data.
Router-dependent functions (pay_and_wait, skip_if_no_*) are excluded — they're
integration-test territory.
"""
from __future__ import annotations

import json
import pytest

from lib.helpers import (
    is_session_event,
    is_mac_lookup_failure,
    is_payment_swap_succeeded,
    parse_json_or_fail,
    gate_bug_fix,
    is_full_merchant,
    is_degraded,
    assert_internet,
)


# --------------------------------------------------------------------------- #
# is_session_event
# --------------------------------------------------------------------------- #


class TestIsSessionEvent:
    def test_kind_1022(self):
        assert is_session_event({"kind": 1022, "tags": []}) is True

    def test_allotment_tag(self):
        assert is_session_event({"kind": 21000, "tags": [["allotment", "1000"]]}) is True

    def test_kind_21000_no_allotment(self):
        assert is_session_event({"kind": 21000, "tags": [["p", "abc"]]}) is False

    def test_non_dict(self):
        assert is_session_event("not a dict") is False
        assert is_session_event(None) is False
        assert is_session_event(42) is False

    def test_empty_dict(self):
        assert is_session_event({}) is False

    def test_tags_not_list(self):
        assert is_session_event({"kind": 1022, "tags": "not_a_list"}) is True

    def test_allotment_tag_wrong_type(self):
        assert is_session_event({"kind": 10021, "tags": ["not_a_list"]}) is False


# --------------------------------------------------------------------------- #
# is_mac_lookup_failure
# --------------------------------------------------------------------------- #


class TestIsMacLookupFailure:
    def test_correct_kind_and_code(self):
        resp = {"kind": 21023, "tags": [["code", "mac-address-lookup-failed"]]}
        assert is_mac_lookup_failure(resp) is True

    def test_wrong_kind(self):
        resp = {"kind": 1022, "tags": [["code", "mac-address-lookup-failed"]]}
        assert is_mac_lookup_failure(resp) is False

    def test_wrong_code(self):
        resp = {"kind": 21023, "tags": [["code", "payment-failed"]]}
        assert is_mac_lookup_failure(resp) is False

    def test_no_tags(self):
        assert is_mac_lookup_failure({"kind": 21023}) is False

    def test_empty_tags(self):
        assert is_mac_lookup_failure({"kind": 21023, "tags": []}) is False

    def test_non_list_tags(self):
        assert is_mac_lookup_failure({"kind": 21023, "tags": "string"}) is False


# --------------------------------------------------------------------------- #
# parse_json_or_fail
# --------------------------------------------------------------------------- #


class TestParseJsonOrFail:
    def test_valid_json(self):
        assert parse_json_or_fail('{"key": "value"}') == {"key": "value"}

    def test_valid_json_array(self):
        assert parse_json_or_fail('[1, 2, 3]') == [1, 2, 3]

    def test_invalid_json_fails(self):
        try:
            parse_json_or_fail("not json")
            assert False, "should have raised"
        except BaseException as e:
            assert "Non-JSON" in str(e)

    def test_invalid_json_skip(self):
        try:
            parse_json_or_fail("not json", skip=True)
            assert False, "should have raised"
        except BaseException as e:
            assert "Non-JSON" in str(e)

    def test_empty_string_fails(self):
        try:
            parse_json_or_fail("")
            assert False, "should have raised"
        except BaseException as e:
            assert "Non-JSON" in str(e)

    def test_with_label(self):
        try:
            parse_json_or_fail("not json", label="balance")
            assert False, "should have raised"
        except BaseException as e:
            assert "balance" in str(e)


# --------------------------------------------------------------------------- #
# gate_bug_fix
# --------------------------------------------------------------------------- #


class TestGateBugFix:
    def test_fix_present_does_nothing(self):
        gate_bug_fix(True, bug_id="test-bug", fix_pr="PR #999")

    def test_fix_absent_xfails(self):
        with pytest.raises(Exception, match="test-bug"):
            gate_bug_fix(False, bug_id="test-bug", fix_pr="PR #999")

    def test_fix_absent_no_pr(self):
        with pytest.raises(Exception, match="test-bug"):
            gate_bug_fix(False, bug_id="test-bug")

    def test_fix_absent_message_contains_pr(self):
        with pytest.raises(Exception, match="PR #999"):
            gate_bug_fix(False, bug_id="bug", fix_pr="PR #999")


# --------------------------------------------------------------------------- #
# is_full_merchant / is_degraded (with mock router)
# --------------------------------------------------------------------------- #


class MockRouter:
    """Minimal router for testing API-parsing helpers."""
    def __init__(self, status_code=200, body=""):
        self._status = status_code
        self._body = body

    def api_status(self, path="/"):
        return self._status

    def api_body(self, path="/"):
        return self._body


class TestIsFullMerchant:
    def test_valid_merchant(self):
        body = json.dumps({"kind": 10021, "tags": [["price_per_step", "cashu", "1", "sat"]]})
        assert is_full_merchant(MockRouter(200, body)) is True

    def test_kind_wrong(self):
        body = json.dumps({"kind": 21023, "tags": [["price_per_step", "cashu", "1", "sat"]]})
        assert is_full_merchant(MockRouter(200, body)) is False

    def test_no_price_per_step_tag(self):
        body = json.dumps({"kind": 10021, "tags": [["metric", "bytes"]]})
        assert is_full_merchant(MockRouter(200, body)) is False

    def test_non_200(self):
        assert is_full_merchant(MockRouter(500, "")) is False

    def test_invalid_json(self):
        assert is_full_merchant(MockRouter(200, "not json")) is False


class TestIsDegraded:
    def test_degraded(self):
        body = json.dumps({"kind": 21023, "tags": []})
        assert is_degraded(MockRouter(200, body)) is True

    def test_not_degraded(self):
        body = json.dumps({"kind": 10021, "tags": []})
        assert is_degraded(MockRouter(200, body)) is False

    def test_invalid_json(self):
        assert is_degraded(MockRouter(200, "not json")) is False


# --------------------------------------------------------------------------- #
# is_payment_swap_succeeded
# --------------------------------------------------------------------------- #


class TestIsPaymentSwapSucceeded:
    def test_session_event_kind_1022(self):
        assert is_payment_swap_succeeded(
            {"kind": 1022, "tags": [["allotment", "66060288"]]}
        ) is True

    def test_session_event_allotment_only(self):
        assert is_payment_swap_succeeded(
            {"kind": 10021, "tags": [["allotment", "100"]]}
        ) is True

    def test_mac_lookup_failure_structural(self):
        assert is_payment_swap_succeeded(
            {"kind": 21023, "tags": [["code", "mac-address-lookup-failed"]]}
        ) is True

    def test_nut02_id_length_invalid(self):
        assert is_payment_swap_succeeded(
            {"kind": 21023, "tags": [["code", "NUT02: ID length invalid"]]}
        ) is False

    def test_invalid_v3_token(self):
        assert is_payment_swap_succeeded(
            {"kind": 21023, "content": "invalid V3 token"}
        ) is False

    def test_token_already_spent(self):
        assert is_payment_swap_succeeded(
            {"kind": 21023, "content": "token_already_spent"}
        ) is False

    def test_failed_to_open_gate(self):
        assert is_payment_swap_succeeded(
            {"kind": 21023, "content": "failed to open gate: exit status 1"}
        ) is True

    def test_gateway_ip_no_false_positive(self):
        assert is_payment_swap_succeeded(
            {"kind": 21023, "tags": [["gateway_ip", "10.99.99.1"]]}
        ) is False

    def test_empty_dict(self):
        assert is_payment_swap_succeeded({}) is False

    def test_non_dict(self):
        assert is_payment_swap_succeeded(None) is False
        assert is_payment_swap_succeeded("not a dict") is False

    def test_unknown_error_returns_false(self):
        assert is_payment_swap_succeeded(
            {"kind": 21023, "tags": [["code", "some-unknown-error"]]}
        ) is False

    def test_token_failure_takes_precedence_over_gate(self):
        body = "NUT02: ID length invalid and failed to open gate"
        assert is_payment_swap_succeeded(
            {"kind": 21023, "content": body}
        ) is False


class TestMintUrlPort:
    """block_mints must block the port a mint actually listens on —
    blocking only 443 was a no-op for plain-http lab mints, so degraded
    mode never triggered locally (2026-09-03 finding)."""

    def test_https_default_port(self):
        from lib.helpers import _mint_url_port
        assert _mint_url_port("https://testnut.cashu.exchange") == 443

    def test_http_default_port(self):
        from lib.helpers import _mint_url_port
        assert _mint_url_port("http://mint.example") == 80

    def test_explicit_port_wins(self):
        from lib.helpers import _mint_url_port
        assert _mint_url_port("http://10.99.99.2:8383") == 8383

    def test_https_explicit_port(self):
        from lib.helpers import _mint_url_port
        assert _mint_url_port("https://mint.example:7443") == 7443

    def test_bare_ip_with_port(self):
        from lib.helpers import _mint_url_port
        assert _mint_url_port("http://10.99.99.2:8385") == 8385
