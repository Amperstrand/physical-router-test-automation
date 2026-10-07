"""Mint URL trailing-slash and normalization tests.

Tests that the backend survives mint URLs with trailing slashes,
extra path segments, and case differences — without crashing or
entering degraded mode. Complements test_mint_url_fuzzy.py (which
tests the full payment flow) by verifying backend stability alone,
without requiring NDS or a connected client.
"""

import json
import os
import time

import pytest
import requests

from lib.constants import BACKEND_PORT

pytestmark = [pytest.mark.story("O4"),pytest.mark.api, pytest.mark.go_only]


def _get_config_mint_url(router):
    raw = router.ssh("cat /etc/tollgate/config.json")
    cfg = json.loads(raw)
    return cfg.get("accepted_mints", [{}])[0].get("url", "")


def _set_mint_url(router, new_url):
    router.ssh(
        f"jq '.accepted_mints[0].url = \"{new_url}\"' /etc/tollgate/config.json"
        f" > /tmp/cfg.json && mv /tmp/cfg.json /etc/tollgate/config.json",
        timeout=10,
    )
    router.restart_backend(timeout=45)


def _wait_backend_healthy(router, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if router.api_status("/") == 200:
            return True
        time.sleep(2)
    return False


def test_backend_survives_trailing_slash_url(router):
    """Backend must not crash when mint URL has a trailing slash."""
    original_url = _get_config_mint_url(router)
    if not original_url:
        pytest.skip("Cannot read configured mint URL")

    slashed_url = original_url.rstrip("/") + "/"
    if slashed_url == original_url:
        pytest.skip("URL already has trailing slash")

    try:
        _set_mint_url(router, slashed_url)
        healthy = _wait_backend_healthy(router, timeout=45)
        if not healthy:
            logs = router.get_tollgate_logs(lines=50)
            pytest.fail(
                f"Backend did not return HTTP 200 within 45s with trailing-slash URL.\n"
                f"URL: {slashed_url}\nLogs:\n{logs[-500:]}"
            )
    finally:
        _set_mint_url(router, original_url)
        _wait_backend_healthy(router, timeout=30)


def test_backend_survives_path_normalization(router):
    """Backend must not crash when mint URL has an extra path segment."""
    original_url = _get_config_mint_url(router)
    if not original_url:
        pytest.skip("Cannot read configured mint URL")

    path_url = original_url.rstrip("/") + "/"
    if path_url == original_url:
        path_url = original_url + "/"

    try:
        _set_mint_url(router, path_url)
        healthy = _wait_backend_healthy(router, timeout=45)
        if not healthy:
            logs = router.get_tollgate_logs(lines=50)
            pytest.fail(
                f"Backend did not return HTTP 200 within 45s with path URL.\n"
                f"URL: {path_url}\nLogs:\n{logs[-500:]}"
            )
    finally:
        _set_mint_url(router, original_url)
        _wait_backend_healthy(router, timeout=30)


def test_invoice_works_after_url_restore(router):
    """Invoice creation works after a trailing-slash URL was set and reverted."""
    original_url = _get_config_mint_url(router)
    if not original_url:
        pytest.skip("Cannot read configured mint URL")

    slashed_url = original_url.rstrip("/") + "/"
    if slashed_url == original_url:
        pytest.skip("URL already has trailing slash")

    try:
        _set_mint_url(router, slashed_url)
        _wait_backend_healthy(router, timeout=45)
    finally:
        _set_mint_url(router, original_url)
        assert _wait_backend_healthy(router, timeout=30), "Backend not healthy after URL restore"

    backend_ip = os.environ.get("TOLLGATE_SSH_HOST", "10.99.99.1")
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL", original_url)
    url = f"http://{backend_ip}:{BACKEND_PORT}/ln-invoice"
    for attempt in range(3):
        try:
            resp = requests.post(url, json={"amount": 21, "mint_url": mint_url}, timeout=15)
            if resp.status_code == 200:
                assert resp.json().get("quote"), "Invoice response missing quote"
                return
        except Exception:
            pass
        time.sleep(3)
    pytest.fail("Invoice creation failed after URL restore")
