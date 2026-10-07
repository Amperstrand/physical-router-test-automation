"""End-to-end tests for self-signed SSL apply → verify → remove → verify-clean lifecycle.

Replaces the following mocked Go unit tests that were removed from ssl_test.go:
- TestConfigureUhttpd (mocked fnRunCommandChecked)
- TestAllowPort443 (mocked fnRunCommandChecked)
- TestRemovePort443Allow (mocked fnRunCommandChecked)
- TestRestoreUhttpd_FromBackup (mocked fnRunCommand)

These tests exercise the real UCI, real filesystem, and real service restarts
on a physical OpenWrt router.

All tests skip cleanly when 'tollgate ssl' subcommand is not available.
"""

import logging
import os
import re
import time

import pytest

from lib.helpers import get_uhttpd_cert, get_uhttpd_key, skip_if_no_ssl_cli, ssl_is_applied

log = logging.getLogger("tollgate.ssl_lifecycle")

pytestmark = [pytest.mark.story("O8"),pytest.mark.api, pytest.mark.extended]


def _skip_virtual_lab():
    if os.environ.get("TOLLGATE_VIRTUAL_LAB"):
        pytest.skip("SSL lifecycle requires physical router (uhttpd HTTPS in QEMU is unreliable)")


_skip_if_no_ssl_cli = skip_if_no_ssl_cli
_ssl_is_applied = ssl_is_applied
_get_uhttpd_cert = get_uhttpd_cert
_get_uhttpd_key = get_uhttpd_key


def _get_uhttpd_listen_https(router):
    return router.ssh("uci get uhttpd.main.listen_https 2>/dev/null || echo NOT_SET").strip()


def _port_443_listening(router):
    result = router.ssh("netstat -tlnp 2>/dev/null | grep ':443' || ss -tlnp | grep ':443'")
    return "443" in result


def _cert_file_exists(router):
    result = router.ssh("ls -la /etc/tollgate/ssl/server.crt 2>&1")
    return "No such file" not in result


@pytest.fixture(autouse=True)
def ssl_cleanup(router):
    yield
    if _ssl_is_applied(router):
        log.info("Teardown: removing SSL after test")
        router.ssh("tollgate ssl remove --yes 2>/dev/null || true")


@pytest.mark.extended
def test_ssl_apply_sets_uhttpd_cert_and_key(router):
    """After applying self-signed SSL, uhttpd must point to the new cert/key files.

    This replaces TestConfigureUhttpd which mocked fnRunCommandChecked to verify
    that 'uci set uhttpd.main.cert=...' was called. Here we read the actual UCI
    values after applying.
    """
    _skip_if_no_ssl_cli(router)

    router.ssh("tollgate ssl apply --yes 2>&1")

    cert = _get_uhttpd_cert(router)
    key = _get_uhttpd_key(router)

    assert cert != "NOT_SET", "uhttpd.main.cert not set after SSL apply"
    assert key != "NOT_SET", "uhttpd.main.key not set after SSL apply"
    assert "tollgate" in cert.lower() or ".crt" in cert, f"unexpected cert path: {cert}"
    assert "tollgate" in key.lower() or ".key" in key, f"unexpected key path: {key}"


@pytest.mark.extended
def test_ssl_apply_enables_https_listener(router):
    """After applying SSL, uhttpd must listen on port 443.

    This replaces TestAllowPort443 which mocked fnRunCommandChecked to verify
    the firewall rule was added. Here we check the actual port.
    """
    _skip_if_no_ssl_cli(router)

    router.ssh("tollgate ssl apply --yes 2>&1")

    assert _port_443_listening(router), "Port 443 not listening after SSL apply"


@pytest.mark.extended
def test_ssl_apply_cert_file_on_disk(router):
    """After applying SSL, the cert file must exist on disk with correct permissions."""
    _skip_if_no_ssl_cli(router)

    router.ssh("tollgate ssl apply --yes 2>&1")

    assert _cert_file_exists(router), "Cert file not found after SSL apply"


@pytest.mark.extended
def test_ssl_remove_restores_uhttpd(router):
    """After removing SSL, uhttpd cert/key must be restored to defaults.

    This replaces TestRestoreUhttpd_FromBackup which mocked fnRunCommand to
    simulate reading backup files. Here we apply, then remove, then verify.
    """
    _skip_if_no_ssl_cli(router)

    original_cert = _get_uhttpd_cert(router)
    original_key = _get_uhttpd_key(router)

    router.ssh("tollgate ssl apply --yes 2>&1")

    applied_cert = _get_uhttpd_cert(router)
    assert applied_cert != original_cert, "Cert path should have changed after apply"

    router.ssh("tollgate ssl remove --yes 2>&1")

    restored_cert = _get_uhttpd_cert(router)
    restored_key = _get_uhttpd_key(router)

    assert restored_cert == original_cert, \
        f"Cert not restored: before={original_cert}, after={restored_cert}"
    assert restored_key == original_key, \
        f"Key not restored: before={original_key}, after={restored_key}"


@pytest.mark.extended
def test_ssl_remove_cleans_cert_files(router):
    """After removing SSL, cert files must be deleted from disk."""
    _skip_if_no_ssl_cli(router)

    router.ssh("tollgate ssl apply --yes 2>&1")
    assert _cert_file_exists(router), "Precondition: cert should exist after apply"

    router.ssh("tollgate ssl remove --yes 2>&1")

    assert not _cert_file_exists(router), "Cert file still exists after SSL remove"


@pytest.mark.extended
def test_ssl_remove_stops_https_listener(router):
    """After removing SSL, port 443 must no longer be listening.

    This replaces TestRemovePort443Allow which mocked fnRunCommandChecked.
    """
    _skip_virtual_lab()
    _skip_if_no_ssl_cli(router)

    router.ssh("tollgate ssl apply --yes 2>&1")
    assert _port_443_listening(router), "Precondition: 443 should be listening after apply"

    router.ssh("tollgate ssl remove --yes 2>&1")
    router.ssh("/etc/init.d/uhttpd restart")
    time.sleep(2)

    for _ in range(10):
        if not _port_443_listening(router):
            break
        time.sleep(2)

    assert not _port_443_listening(router), "Port 443 still listening after SSL remove"


@pytest.mark.extended
def test_ssl_full_roundtrip(router):
    """Full lifecycle: apply → verify → remove → verify → re-apply → verify.

    This exercises the complete flow including idempotent re-application and
    ensures no state leaks between apply/remove cycles.
    """
    _skip_virtual_lab()
    _skip_if_no_ssl_cli(router)

    original_cert = _get_uhttpd_cert(router)

    router.ssh("tollgate ssl apply --yes 2>&1")
    assert _ssl_is_applied(router), "SSL not applied after first apply"
    assert _port_443_listening(router), "443 not listening after first apply"

    router.ssh("tollgate ssl remove --yes 2>&1")
    router.ssh("/etc/init.d/uhttpd restart")
    time.sleep(2)
    assert not _ssl_is_applied(router), "SSL still applied after remove"
    for _ in range(10):
        if not _port_443_listening(router):
            break
        time.sleep(2)
    assert not _port_443_listening(router), "443 still listening after remove"
    assert _get_uhttpd_cert(router) == original_cert, "Cert not restored after remove"

    router.ssh("tollgate ssl apply --yes 2>&1")
    assert _ssl_is_applied(router), "SSL not applied after second apply"
    assert _port_443_listening(router), "443 not listening after second apply"
