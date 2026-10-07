"""Go-native SSL management CLI tests.

Verifies `tollgate ssl apply/remove/status` — the Go CLI that replaced
the old `tollgate-setup-ssl` shell script (PR #117). Feature-detected
so tests run against any firmware with the Go SSL CLI.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest


pytestmark = [
    pytest.mark.story("O8"),
    pytest.mark.api,
    pytest.mark.extended,
    pytest.mark.go_only,
    pytest.mark.config,
    pytest.mark.destructive,
    pytest.mark.complete,
]

SSL_DIR = "/etc/tollgate/ssl"
SSL_CERT = f"{SSL_DIR}/server.crt"
SSL_KEY = f"{SSL_DIR}/server.key"
SSL_BACKUP = f"{SSL_DIR}/backup"


def _skip_if_no_pr123_ssl(router):
    out = router.ssh("tollgate ssl status 2>&1 || true")
    if "Unknown" in out or "not found" in out or ("Usage" not in out and "SSL" not in out):
        pytest.skip("PR #123 Go SSL CLI not installed")


def _remove_ssl_force(router):
    router.ssh("tollgate ssl remove --yes >/tmp/tollgate-ssl-remove.log 2>&1 || true", timeout=60)
    router.ssh(
        f"rm -rf {SSL_CERT} {SSL_KEY} {SSL_BACKUP} "
        "/tmp/tollgate-test-cert.pem /tmp/tollgate-test-key.pem 2>/dev/null || true"
    )


def _apply_self_signed(router):
    out = router.ssh("tollgate ssl apply --yes 2>&1", timeout=90)
    assert "error" not in out.lower(), out
    assert router.ssh_bool(f"test -f {SSL_CERT} && test -f {SSL_KEY}"), out
    return out


def _remote_file_mode(router, path: str) -> str:
    return router.file_mode(path)


@pytest.fixture(autouse=True)
def ssl_clean_state(router):
    _skip_if_no_pr123_ssl(router)
    _remove_ssl_force(router)
    yield
    _remove_ssl_force(router)


@pytest.mark.extended
def test_ssl_go_cli_initial_clean_state(router):
    assert not router.ssh_bool(f"test -f {SSL_CERT}"), "TollGate SSL cert should not exist initially"
    assert not router.ssh_bool(f"test -d {SSL_BACKUP}"), "SSL backup should not exist initially"
    cert_uci = router.uci_get("uhttpd.main.cert")
    assert cert_uci != SSL_CERT, f"uhttpd should not point to TollGate SSL cert initially: {cert_uci}"
    nds = router.uci_get("nodogsplash.@nodogsplash[0].users_to_router")
    assert "allow tcp port 443" not in nds, f"NDS should not allow 443 initially: {nds}"


@pytest.mark.extended
def test_ssl_go_cli_status_reports_unconfigured(router):
    status = router.ssh("tollgate ssl status 2>&1")
    assert "SSL" in status
    assert "not configured" in status.lower(), status
    assert "tollgate ssl apply" in status, status


@pytest.mark.extended
def test_ssl_go_cli_apply_self_signed_configures_uhttpd(router):
    _apply_self_signed(router)
    assert router.uci_get("uhttpd.main.cert") == SSL_CERT
    assert router.uci_get("uhttpd.main.key") == SSL_KEY
    listen_https = router.uci_get("uhttpd.main.listen_https")
    assert "443" in listen_https, listen_https
    assert router.ssh_bool("ss -tlnp 2>/dev/null | grep ':443' || netstat -tlnp 2>/dev/null | grep ':443'"), \
        "uhttpd should listen on 443"


@pytest.mark.extended
def test_ssl_go_cli_apply_self_signed_interactive_prompt_accepts_yes(router):
    out = router.ssh("printf 'y\\n' | tollgate ssl apply 2>&1", timeout=90)
    assert "Apply all?" in out, out
    assert "Done. Self-signed HTTPS enabled" in out, out
    assert router.ssh_bool(f"test -f {SSL_CERT} && test -f {SSL_KEY}"), out
    assert router.uci_get("uhttpd.main.cert") == SSL_CERT


@pytest.mark.extended
def test_ssl_go_cli_apply_self_signed_creates_backup_and_mode(router):
    _apply_self_signed(router)
    assert router.ssh_bool(f"test -d {SSL_BACKUP}"), "SSL backup directory missing"
    mode = router.ssh(f"cat {SSL_BACKUP}/ssl.mode 2>/dev/null || true").strip()
    assert mode == "self-signed", f"Expected self-signed mode backup, got: {mode}"


@pytest.mark.extended
def test_ssl_go_cli_generated_cert_has_expected_cn_san_and_key_permissions(router):
    _apply_self_signed(router)
    hostname = router.uci_get("system.@system[0].hostname")
    expected_domain = f"{hostname}.lan"

    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = Path(tmpdir) / "server.crt"
        cert_path.write_text(router.ssh(f"cat {SSL_CERT}"))
        subject = subprocess.run(
            ["openssl", "x509", "-noout", "-subject", "-in", str(cert_path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        san = subprocess.run(
            ["openssl", "x509", "-noout", "-ext", "subjectAltName", "-in", str(cert_path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        subprocess.run(
            ["openssl", "x509", "-checkend", "0", "-in", str(cert_path)],
            check=True,
            capture_output=True,
            text=True,
        )

    assert f"CN = {expected_domain}" in subject or f"CN={expected_domain}" in subject, subject
    assert f"DNS:{expected_domain}" in san, san
    assert f"DNS:{hostname}" in san, san
    key_mode = _remote_file_mode(router, SSL_KEY)
    assert key_mode == "-rw-------", f"Expected SSL key mode 600, got {key_mode}"


@pytest.mark.extended
def test_ssl_go_cli_allows_https_through_nodogsplash_without_self_signed_dns(router):
    _apply_self_signed(router)
    nds = router.uci_get("nodogsplash.@nodogsplash[0].users_to_router")
    assert "allow tcp port 443" in nds, nds
    hostname = router.uci_get("system.@system[0].hostname")
    domains = router.ssh("uci show dhcp 2>/dev/null | grep '=domain' || true")
    assert f"{hostname}.lan" not in domains, domains


@pytest.mark.extended
def test_ssl_go_cli_status_reports_self_signed_after_apply(router):
    _apply_self_signed(router)
    status = router.ssh("tollgate ssl status 2>&1")
    assert "SSL" in status
    assert "self-signed" in status.lower(), status


@pytest.mark.extended
def test_ssl_go_cli_reapply_keeps_valid_state(router):
    _apply_self_signed(router)
    out = router.ssh("tollgate ssl apply --yes 2>&1", timeout=90)
    assert "error" not in out.lower(), out
    assert "backup already exists" in out.lower(), out
    assert router.ssh_bool(f"test -f {SSL_CERT} && test -f {SSL_KEY}"), out
    assert router.uci_get("uhttpd.main.cert") == SSL_CERT
    assert router.ssh_bool("ss -tlnp 2>/dev/null | grep ':443' || netstat -tlnp 2>/dev/null | grep ':443'"), \
        "uhttpd should still listen on 443"


@pytest.mark.extended
def test_ssl_go_cli_remove_reverts_self_signed_state(router):
    _apply_self_signed(router)
    out = router.ssh("tollgate ssl remove --yes 2>&1", timeout=90)
    assert "error" not in out.lower(), out
    assert not router.ssh_bool(f"test -f {SSL_CERT}"), "cert still exists after remove"
    assert not router.ssh_bool(f"test -d {SSL_BACKUP}"), "backup still exists after remove"
    cert_uci = router.uci_get("uhttpd.main.cert")
    assert cert_uci != SSL_CERT, f"uhttpd still points to TollGate cert: {cert_uci}"


@pytest.mark.extended
def test_ssl_go_cli_remove_without_backup_fails_cleanly(router):
    _remove_ssl_force(router)
    out = router.ssh("tollgate ssl remove 2>&1; echo RC=$?", timeout=60)
    match = re.search(r"RC=(\d+)", out)
    assert match, out
    assert match.group(1) != "0", out
    assert "backup" in out.lower(), out


@pytest.mark.extended
def test_ssl_go_cli_apply_real_certificate_configures_domain_mode(router):
    domain = os.environ.get("TOLLGATE_SSL_TEST_DOMAIN", "tollgate-python-test.example.com")
    key_path = "/tmp/tollgate-test-key.pem"
    cert_path = "/tmp/tollgate-test-cert.pem"
    router.ssh(
        "openssl req -x509 -newkey rsa:2048 -nodes "
        f"-keyout {key_path} -out {cert_path} -days 2 "
        f"-subj '/CN={domain}' -addext 'subjectAltName=DNS:{domain}' >/dev/null 2>&1",
        timeout=90,
    )
    if not router.ssh_bool(f"test -s {cert_path} && test -s {key_path}"):
        pytest.skip("router openssl cannot generate local test certificate")

    out = router.ssh(f"tollgate ssl apply --yes {cert_path} {key_path} 2>&1", timeout=90)
    assert "error" not in out.lower(), out
    status = router.ssh("tollgate ssl status 2>&1")
    assert "real-cert" in status.lower(), status
    assert domain in status, status
    gateway_domain = router.uci_get("nodogsplash.@nodogsplash[0].gatewaydomainname")
    assert gateway_domain == domain
    dhcp_domains = router.ssh("uci show dhcp 2>/dev/null | grep -E '\\.name=|\\.ip=' || true")
    assert domain in dhcp_domains, dhcp_domains


@pytest.mark.extended
def test_ssl_go_cli_remove_reverts_real_certificate_domain_state(router):
    domain = os.environ.get("TOLLGATE_SSL_TEST_DOMAIN", "tollgate-python-test.example.com")
    key_path = "/tmp/tollgate-test-key.pem"
    cert_path = "/tmp/tollgate-test-cert.pem"
    router.ssh(
        "openssl req -x509 -newkey rsa:2048 -nodes "
        f"-keyout {key_path} -out {cert_path} -days 2 "
        f"-subj '/CN={domain}' -addext 'subjectAltName=DNS:{domain}' >/dev/null 2>&1",
        timeout=90,
    )
    if not router.ssh_bool(f"test -s {cert_path} && test -s {key_path}"):
        pytest.skip("router openssl cannot generate local test certificate")

    apply_out = router.ssh(f"tollgate ssl apply --yes {cert_path} {key_path} 2>&1", timeout=90)
    assert "error" not in apply_out.lower(), apply_out

    remove_out = router.ssh("tollgate ssl remove --yes 2>&1", timeout=90)
    assert "error" not in remove_out.lower(), remove_out
    assert not router.ssh_bool(f"test -f {SSL_CERT}"), "cert still exists after real-cert remove"
    assert not router.ssh_bool(f"test -d {SSL_BACKUP}"), "backup still exists after real-cert remove"
    assert router.uci_get("nodogsplash.@nodogsplash[0].gatewaydomainname") != domain
    dhcp_domains = router.ssh("uci show dhcp 2>/dev/null | grep -E '\\.name=|\\.ip=' || true")
    assert domain not in dhcp_domains, dhcp_domains
