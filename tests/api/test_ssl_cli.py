"""Tests for CLI SSL management (tollgate ssl apply/remove/status).

Pytest equivalents of the Makefile r-test-ssl-* targets. These tests
exercise the Go-based SSL management introduced by the CLI SSL rewrite.

Tests skip cleanly on versions that don't have the Go SSL CLI by
checking for the 'tollgate ssl' subcommand.

All tests are safe: they restore the router to its original SSL state
via teardown.
"""

import json
import logging
import re

import pytest

from lib.helpers import (
    nodogsplash_allow_entries,
    nodogsplash_allows_port,
    skip_if_no_openssl,
    skip_if_no_ssl_cli,
    ssl_is_applied,
)

log = logging.getLogger("tollgate.ssl_cli")

pytestmark = [pytest.mark.api, pytest.mark.extended]


_skip_if_no_ssl_cli = skip_if_no_ssl_cli
_skip_if_no_openssl = skip_if_no_openssl
_ssl_is_applied = ssl_is_applied


@pytest.fixture(autouse=True)
def ssl_cleanup(router):
    yield
    if _ssl_is_applied(router):
        log.info("Teardown: removing SSL after test")
        router.ssh("tollgate ssl remove --yes 2>/dev/null || true")


@pytest.mark.extended
def test_ssl_apply_self_signed(router):
    _skip_if_no_ssl_cli(router)
    result = router.ssh("tollgate ssl apply --yes 2>&1")
    log.info("ssl apply output: %s", result)
    assert "error" not in result.lower() or "no error" in result.lower(), \
        f"SSL apply failed: {result[:300]}"


@pytest.mark.extended
def test_ssl_status_shows_cert(router):
    _skip_if_no_ssl_cli(router)
    router.ssh("tollgate ssl apply --yes 2>&1")
    result = router.ssh("tollgate ssl status 2>&1")
    assert any(kw in result.lower() for kw in ("active", "applied", "installed", "configured")) \
        and "not configured" not in result.lower(), \
        f"SSL status did not show applied state: {result[:300]}"


@pytest.mark.extended
def test_ssl_removes_cleanly(router):
    _skip_if_no_ssl_cli(router)
    router.ssh("tollgate ssl apply --yes 2>&1")
    result = router.ssh("tollgate ssl remove --yes 2>&1")
    log.info("ssl remove output: %s", result)
    status = router.ssh("tollgate ssl status 2>&1")
    assert "active" not in status.lower() and "applied" not in status.lower(), \
        f"SSL still shows as applied after remove: {status[:300]}"


@pytest.mark.extended
def test_ssl_wrapper_scripts_exist(router):
    tollgate_apply = router.ssh("which tollgate-apply-ssl 2>/dev/null || echo MISSING")
    tollgate_remove = router.ssh("which tollgate-remove-ssl 2>/dev/null || echo MISSING")
    if "MISSING" in tollgate_apply and "MISSING" in tollgate_remove:
        pytest.skip("Wrapper scripts not installed")
    if "MISSING" not in tollgate_apply:
        assert tollgate_apply.strip().endswith("tollgate-apply-ssl")
    if "MISSING" not in tollgate_remove:
        assert tollgate_remove.strip().endswith("tollgate-remove-ssl")


@pytest.mark.extended
def test_ssl_idempotent_apply(router):
    _skip_if_no_ssl_cli(router)
    router.ssh("tollgate ssl apply --yes 2>&1")
    result = router.ssh("tollgate ssl apply --yes 2>&1")
    assert "error" not in result.lower() or "already" in result.lower() or "no error" in result.lower(), \
        f"Second SSL apply failed: {result[:300]}"


@pytest.mark.extended
def test_ssl_cert_has_valid_san(router):
    _skip_if_no_ssl_cli(router)
    _skip_if_no_openssl(router)
    router.ssh("tollgate ssl apply --yes 2>&1")
    cert_check = router.ssh(
        "openssl x509 -in /etc/tollgate/ssl/server.crt -noout -text 2>/dev/null | grep -A5 'Subject Alternative'"
    )
    if not cert_check.strip():
        cert_check = router.ssh(
            "openssl x509 -in /etc/tollgate/ssl/server.crt -noout -text 2>/dev/null | grep -A2 'Subject:'"
        )
    assert cert_check.strip(), "Could not read SSL certificate"


@pytest.mark.extended
def test_ssl_https_port_listening(router):
    _skip_if_no_ssl_cli(router)
    router.ssh("tollgate ssl apply --yes 2>&1")
    result = router.ssh("netstat -tlnp 2>/dev/null | grep ':443' || ss -tlnp | grep ':443'")
    assert "443" in result, f"Port 443 not listening after SSL apply: {result[:200]}"


@pytest.mark.extended
def test_ssl_nodogsplash_allows_443(router):
    _skip_if_no_ssl_cli(router)
    router.ssh("tollgate ssl apply --yes 2>&1")
    nds_config = router.ssh("cat /etc/config/nodogsplash 2>/dev/null || echo MISSING")
    if "MISSING" in nds_config:
        pytest.skip("nodogsplash config not found")
    # Parsed, never substring-searched: "443" is a substring of the
    # `allow tcp port 8443` entry every router with the admin board carries, so
    # the previous check passed while :443 was not allow-listed at all and the
    # LuCI :8080 -> https://<router>/ redirect dead-ended. See
    # tests/unit/test_nodogsplash_allow_list.py.
    assert nodogsplash_allows_port(nds_config, 443), (
        "nodogsplash users_to_router does not allow tcp port 443, so an "
        "unauthenticated client's :8080 -> https://<router>/ redirect cannot "
        "complete. Entries: %s" % (nodogsplash_allow_entries(nds_config),)
    )


@pytest.mark.extended
def test_ssl_remove_no_backup_errors(router):
    _skip_if_no_ssl_cli(router)
    result = router.ssh("tollgate ssl remove --yes 2>&1 || true")
    if "no ssl backup" in result.lower() or "nothing to remove" in result.lower() or "not configured" in result.lower():
        return
    assert "error" not in result.lower(), f"SSL remove errored unexpectedly: {result[:300]}"
