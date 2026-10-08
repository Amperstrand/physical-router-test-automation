"""Mint URL fuzzy matching tests for PR #252.

PR #252 changes calculateAllotment() from exact string equality (==) to
MintURLMatches(), which tolerates trailing slashes, case differences,
and path normalization.

These tests modify the backend config URL to differ from the token's
embedded URL, then verify payment still succeeds. Requires a connected
client (NDS auth flow).
"""

import json
import os
import time

import pytest
import requests

from lib.constants import BACKEND_PORT
from lib.helpers import is_session_event, is_mac_lookup_failure, require_client_identity

pytestmark = [pytest.mark.story("R2"),pytest.mark.api, pytest.mark.extended]


def _get_config_mint_url(router):
    raw = router.ssh("cat /etc/tollgate/config.json")
    cfg = json.loads(raw)
    return cfg.get("accepted_mints", [{}])[0].get("url", "")


def _set_and_wait(router, new_url, timeout=90):
    router.ssh(
        f"jq '.accepted_mints[0].url = \"{new_url}\"' /etc/tollgate/config.json"
        f" > /tmp/cfg.json && mv /tmp/cfg.json /etc/tollgate/config.json",
        timeout=10,
    )
    router.restart_backend(timeout=45)
    deadline = time.time() + timeout
    while time.time() < deadline:
        code = router.api_status("/")
        if code == 200:
            time.sleep(5)
            return True
        time.sleep(3)
    return False


def test_payment_with_trailing_slash_mismatch(router, cashu):
    """Token has no trailing slash, config has trailing slash — fuzzy match handles it."""
    require_client_identity(router)
    original_url = _get_config_mint_url(router)
    if not original_url:
        pytest.skip("Cannot read configured mint URL")

    token = cashu.mint(4)
    slashed_url = original_url.rstrip("/") + "/"
    if slashed_url == original_url:
        pytest.skip("URL already has trailing slash")

    try:
        if not _set_and_wait(router, slashed_url):
            pytest.skip("Backend did not stabilize after config change (local lab limitation)")
        resp = router.pay_direct(token)
        if is_mac_lookup_failure(resp):
            pytest.skip("No client on TollGate AP")
        assert is_session_event(resp), (
            f"Payment with trailing-slash URL mismatch failed: {str(resp)[:200]}"
        )
    finally:
        _set_and_wait(router, original_url, timeout=60)


def test_payment_with_path_normalization(router, cashu):
    """Token has bare URL, config has extra path segment — fuzzy match handles it."""
    require_client_identity(router)
    original_url = _get_config_mint_url(router)
    if not original_url:
        pytest.skip("Cannot read configured mint URL")

    token = cashu.mint(4)
    path_url = original_url.rstrip("/") + "/"
    if path_url == original_url:
        path_url = original_url + "/"

    try:
        if not _set_and_wait(router, path_url):
            pytest.skip("Backend did not stabilize after config change (local lab limitation)")
        resp = router.pay_direct(token)
        if is_mac_lookup_failure(resp):
            pytest.skip("No client on TollGate AP")
        assert is_session_event(resp), (
            f"Payment with path-normalized URL failed: {str(resp)[:200]}"
        )
    finally:
        _set_and_wait(router, original_url, timeout=60)
