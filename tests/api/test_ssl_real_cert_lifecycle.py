"""End-to-end tests for real certificate SSL lifecycle (dnsmasq + nodogsplash).

Replaces the following mocked Go unit tests that were removed from ssl_test.go:
- TestConfigureDnsmasq (mocked fnRunCommandChecked)
- TestConfigureNodogsplash (mocked fnRunCommandChecked)

These tests exercise real cert installation with a PEM file generated on the router,
verifying dnsmasq DNS entries and nodogsplash gatewaydomainname configuration.

All tests skip cleanly when 'tollgate ssl' subcommand is not available.
"""

import logging
import re

import pytest

from lib.helpers import (
    get_hostname,
    get_lan_ip,
    skip_if_no_openssl,
    skip_if_no_ssl_cli,
    ssl_is_applied,
)

log = logging.getLogger("tollgate.ssl_real_cert")

pytestmark = [pytest.mark.story("O8"),pytest.mark.api, pytest.mark.extended]


_skip_if_no_ssl_cli = skip_if_no_ssl_cli
_skip_if_no_openssl = skip_if_no_openssl
_ssl_is_applied = ssl_is_applied
_get_lan_ip = get_lan_ip
_get_hostname = get_hostname


def _generate_self_signed_on_router(router, domain, ip):
    """Generate a cert+key PEM on the router and return the file paths."""
    cert_dir = "/tmp/tollgate-test-cert"
    router.ssh(f"mkdir -p {cert_dir}")

    router.ssh(
        f"openssl req -x509 -newkey rsa:2048 -keyout {cert_dir}/key.pem "
        f"-out {cert_dir}/cert.pem -days 365 -nodes "
        f'-subj "/CN={domain}" '
        f'-addext "subjectAltName=DNS:{domain},IP:{ip}" 2>&1'
    )

    combined = f"{cert_dir}/combined.pem"
    router.ssh(f"cat {cert_dir}/cert.pem {cert_dir}/key.pem > {combined}")

    return combined, f"{cert_dir}/cert.pem", f"{cert_dir}/key.pem"


def _get_dnsmasq_domain(router, domain):
    """Check if dnsmasq has a domain entry for the given domain."""
    result = router.ssh("uci show dhcp 2>/dev/null | grep address || echo NOT_FOUND")
    return domain in result


def _get_nodogsplash_gatewaydomainname(router):
    return router.ssh(
        "uci get nodogsplash.@nodogsplash[0].gatewaydomainname 2>/dev/null || echo NOT_SET"
    ).strip()


def _get_nodogsplash_gatewayport(router):
    return router.ssh(
        "uci get nodogsplash.@nodogsplash[0].gatewayport 2>/dev/null || echo NOT_SET"
    ).strip()


@pytest.fixture(autouse=True)
def ssl_cleanup(router):
    yield
    if _ssl_is_applied(router):
        log.info("Teardown: removing SSL after real-cert test")
        router.ssh("tollgate ssl remove --yes 2>/dev/null || true")
    router.ssh("rm -rf /tmp/tollgate-test-cert 2>/dev/null || true")


@pytest.mark.extended
def test_ssl_real_cert_sets_dnsmasq_entry(router):
    """After applying real cert, dnsmasq must have a DNS entry for the domain.

    This replaces TestConfigureDnsmasq which mocked fnRunCommandChecked to verify
    'uci set dhcp.@dnsmasq[0].address=...' was called.
    """
    _skip_if_no_ssl_cli(router)
    _skip_if_no_openssl(router)

    hostname = _get_hostname(router)
    domain = f"{hostname}.lan"
    lan_ip = _get_lan_ip(router)

    combined, _, _ = _generate_self_signed_on_router(router, domain, lan_ip)

    router.ssh(f"tollgate ssl apply {combined} --yes 2>&1")

    assert _get_dnsmasq_domain(router, domain), \
        f"dnsmasq does not have domain entry for {domain}"


@pytest.mark.extended
def test_ssl_real_cert_sets_nodogsplash_gatewaydomainname(router):
    """After applying real cert, nodogsplash gatewaydomainname must be set.

    This replaces TestConfigureNodogsplash which mocked fnRunCommandChecked to verify
    'uci set nodogsplash.@nodogsplash[0].gatewaydomainname=...' was called.
    """
    _skip_if_no_ssl_cli(router)
    _skip_if_no_openssl(router)

    hostname = _get_hostname(router)
    domain = f"{hostname}.lan"
    lan_ip = _get_lan_ip(router)

    combined, _, _ = _generate_self_signed_on_router(router, domain, lan_ip)

    original_domain = _get_nodogsplash_gatewaydomainname(router)
    original_port = _get_nodogsplash_gatewayport(router)

    router.ssh(f"tollgate ssl apply {combined} --yes 2>&1")

    nds_domain = _get_nodogsplash_gatewaydomainname(router)
    assert nds_domain == domain, \
        f"nodogsplash gatewaydomainname: expected {domain}, got {nds_domain}"


@pytest.mark.extended
def test_ssl_real_cert_remove_restores_dnsmasq(router):
    """After removing real cert, dnsmasq domain entry must be cleaned up."""
    _skip_if_no_ssl_cli(router)
    _skip_if_no_openssl(router)

    hostname = _get_hostname(router)
    domain = f"{hostname}.lan"
    lan_ip = _get_lan_ip(router)

    combined, _, _ = _generate_self_signed_on_router(router, domain, lan_ip)

    router.ssh(f"tollgate ssl apply {combined} --yes 2>&1")
    assert _get_dnsmasq_domain(router, domain), "Precondition: domain should be set"

    router.ssh("tollgate ssl remove --yes 2>&1")

    assert not _get_dnsmasq_domain(router, domain), \
        f"dnsmasq still has entry for {domain} after remove"


@pytest.mark.extended
def test_ssl_real_cert_remove_restores_nodogsplash(router):
    """After removing real cert, nodogsplash gatewaydomainname must be restored."""
    _skip_if_no_ssl_cli(router)
    _skip_if_no_openssl(router)

    hostname = _get_hostname(router)
    domain = f"{hostname}.lan"
    lan_ip = _get_lan_ip(router)

    combined, _, _ = _generate_self_signed_on_router(router, domain, lan_ip)

    original_domain = _get_nodogsplash_gatewaydomainname(router)
    original_port = _get_nodogsplash_gatewayport(router)

    router.ssh(f"tollgate ssl apply {combined} --yes 2>&1")

    router.ssh("tollgate ssl remove --yes 2>&1")

    restored_domain = _get_nodogsplash_gatewaydomainname(router)
    restored_port = _get_nodogsplash_gatewayport(router)

    assert restored_domain == original_domain, \
        f"nodogsplash domain not restored: before={original_domain}, after={restored_domain}"
    assert restored_port == original_port, \
        f"nodogsplash port not restored: before={original_port}, after={restored_port}"


@pytest.mark.extended
def test_ssl_real_cert_separate_cert_and_key(router):
    """Test applying with separate cert and key files (not combined PEM)."""
    _skip_if_no_ssl_cli(router)
    _skip_if_no_openssl(router)

    hostname = _get_hostname(router)
    domain = f"{hostname}.lan"
    lan_ip = _get_lan_ip(router)

    _, cert_path, key_path = _generate_self_signed_on_router(router, domain, lan_ip)

    result = router.ssh(f"tollgate ssl apply {cert_path} {key_path} --yes 2>&1")
    log.info("Separate cert+key apply output: %s", result)

    assert _ssl_is_applied(router), "SSL not applied with separate cert+key files"
    assert _get_dnsmasq_domain(router, domain), "dnsmasq domain not set with separate files"
