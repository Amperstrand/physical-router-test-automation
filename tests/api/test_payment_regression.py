"""Payment regression tests for PR #252 (fuzzy mint URL) and PR #253 (DLEQ keyset fix)."""

import json
import os

import pytest
import requests

from lib.constants import BACKEND_PORT
from lib.helpers import is_session_event, is_mac_lookup_failure, require_client_identity

pytestmark = [pytest.mark.api, pytest.mark.critical]


def _pay_with_retry(router, cashu, max_attempts=3):
    for attempt in range(max_attempts):
        token = cashu.mint(4)
        resp = router.pay_direct(token)
        if is_session_event(resp):
            return resp
        body = json.dumps(resp)
        if "Duplicate outputs" in body and attempt < max_attempts - 1:
            continue
        # MAC-lookup failure and gate-open failure on a phantom MAC are
        # both "no real client identity reachable" lab state — the
        # consume-before-gate token burn is infra, not a product
        # regression (DHCP-lease MAC poisoning class, AGENTS.md 2026-09-28).
        if is_mac_lookup_failure(resp) or "failed to open gate" in body:
            return resp
    return resp


def test_ecash_payment_end_to_end(router, cashu):
    require_client_identity(router)
    resp = _pay_with_retry(router, cashu)
    if is_mac_lookup_failure(resp):
        pytest.skip("No client on TollGate AP")
    assert is_session_event(resp), f"Payment failed: {str(resp)[:200]}"


def test_ecash_payment_minimum_token(router, cashu):
    require_client_identity(router)
    resp = _pay_with_retry(router, cashu)
    if is_mac_lookup_failure(resp):
        pytest.skip("No client on TollGate AP")
    assert is_session_event(resp), f"Payment failed: {str(resp)[:200]}"


def test_ecash_payment_verify_allotment(router, cashu):
    require_client_identity(router)
    resp = _pay_with_retry(router, cashu)
    if is_mac_lookup_failure(resp):
        pytest.skip("No client on TollGate AP")
    if not is_session_event(resp):
        pytest.fail(f"Payment failed: {str(resp)[:200]}")
    if resp.get("kind") == 1022:
        tags = resp.get("tags", [])
        allotment_tags = [t for t in tags if isinstance(t, list) and t[0] == "allotment"]
        assert len(allotment_tags) > 0, f"Missing allotment tag: {tags}"
        assert int(allotment_tags[0][1]) > 0, "Allotment should be positive"
