"""Real TLS certificate tests (LE staging + Cloudflare DNS-01).

Ported from mint-health ``r-test-ssl-real-cert*`` targets. Requires
``TOLLGATE_CLOUDFLARE_TOKEN`` or ``SSL_CF_TOKEN`` and ``TOLLGATE_SSL_REAL_CERT_DOMAIN``.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.story("O8"),
    pytest.mark.api,
    pytest.mark.extended,
    pytest.mark.go_only,
    pytest.mark.hardware,
    pytest.mark.config,
    pytest.mark.destructive,
    pytest.mark.timeout(1800),
    pytest.mark.complete,
]

SSL_CERT = "/etc/tollgate/ssl/server.crt"
SSL_BACKUP = "/etc/tollgate/ssl/backup"


def _cf_token() -> str:
    return os.environ.get("TOLLGATE_CLOUDFLARE_TOKEN", "") or os.environ.get("SSL_CF_TOKEN", "")


def _domain() -> str:
    return os.environ.get(
        "TOLLGATE_SSL_REAL_CERT_DOMAIN",
        os.environ.get("SSL_REAL_CERT_DOMAIN", ""),
    )


def _zone_id() -> str:
    return os.environ.get("TOLLGATE_CLOUDFLARE_ZONE_ID", "") or os.environ.get("SSL_CF_ZONE", "")


def _skip_without_cloudflare():
    if not _cf_token():
        pytest.skip("TOLLGATE_CLOUDFLARE_TOKEN or SSL_CF_TOKEN not set")
    if not _domain():
        pytest.skip("TOLLGATE_SSL_REAL_CERT_DOMAIN or SSL_REAL_CERT_DOMAIN not set")


def _skip_if_no_pr123_ssl(router):
    out = router.ssh("tollgate ssl status 2>&1 || true")
    if "Unknown" in out or "not found" in out or ("Usage" not in out and "SSL" not in out):
        pytest.skip("PR #123 Go SSL CLI not installed")


def _acme_home() -> Path:
    base = os.environ.get("TOLLGATE_ACME_HOME", "")
    if base:
        return Path(base).expanduser()
    return Path(tempfile.gettempdir()) / "tollgate-acme"


def _issue_staging_cert(domain: str) -> tuple[Path, Path]:
    """Issue cert via acme.sh LE staging + dns_cf; return (fullchain, key) paths."""
    home = _acme_home()
    home.mkdir(parents=True, exist_ok=True)
    acme_sh = home / "acme.sh"
    if not acme_sh.is_file():
        subprocess.run(
            [
                "bash",
                "-c",
                f"curl -sL https://github.com/acmesh-official/acme.sh/archive/master.tar.gz | "
                f"tar xz -C {home} --strip-components=1",
            ],
            check=True,
            timeout=120,
        )
        acme_sh.chmod(0o755)

    env = os.environ.copy()
    env["CF_Token"] = _cf_token()
    if _zone_id():
        env["CF_Zone_ID"] = _zone_id()

    subprocess.run(
        [
            "bash",
            str(acme_sh),
            "--issue",
            "--dns",
            "dns_cf",
            "-d",
            domain,
            "--server",
            "letsencrypt_test",
            "--home",
            str(home),
            "--force",
        ],
        env=env,
        check=True,
        timeout=600,
        capture_output=True,
        text=True,
    )

    ecc_dir = home / f"{domain}_ecc"
    chain = ecc_dir / "fullchain.cer"
    key = ecc_dir / f"{domain}.key"
    if not chain.is_file() or not key.is_file():
        pytest.fail(f"acme.sh did not produce cert files under {ecc_dir}")
    return chain, key


@pytest.fixture
def real_cert_paths():
    _skip_without_cloudflare()
    domain = _domain()
    return _issue_staging_cert(domain)


@pytest.fixture(autouse=True)
def ssl_clean_state(router):
    _skip_if_no_pr123_ssl(router)
    router.ssh("tollgate ssl remove --yes >/tmp/tollgate-real-cert-remove.log 2>&1 || true", timeout=90)
    yield
    router.ssh("tollgate ssl remove --yes >/tmp/tollgate-real-cert-remove.log 2>&1 || true", timeout=90)
    router.ssh("rm -f /tmp/test-cert.pem /tmp/test-key.pem 2>/dev/null || true")


def _apply_real_cert(router, real_cert_paths: tuple[Path, Path], domain: str) -> None:
    chain, key = real_cert_paths
    remote_cert = "/tmp/test-cert.pem"
    remote_key = "/tmp/test-key.pem"
    router.scp_to(str(chain), remote_cert)
    router.scp_to(str(key), remote_key)

    out = router.ssh(f"tollgate ssl apply --yes {remote_cert} {remote_key} 2>&1", timeout=90)
    assert "error" not in out.lower(), out

    status = router.ssh("tollgate ssl status 2>&1")
    assert "real-cert" in status.lower(), status
    assert domain in status, status

    assert router.uci_get("nodogsplash.@nodogsplash[0].gatewaydomainname") == domain

    dhcp = router.ssh("uci show dhcp 2>/dev/null | grep -E '\\.name=|\\.ip=' || true")
    assert domain in dhcp, dhcp


@pytest.mark.extended
def test_ssl_real_cert_apply_via_acme(router, real_cert_paths):
    _skip_without_cloudflare()
    domain = _domain()
    _apply_real_cert(router, real_cert_paths, domain)

    cert_text = router.ssh(f"cat {SSL_CERT}")
    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = Path(tmpdir) / "server.crt"
        cert_path.write_text(cert_text)
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

    assert domain in subject or domain in san, f"subject={subject}\nsan={san}"


@pytest.mark.extended
def test_ssl_real_cert_remove_cleans_state(router, real_cert_paths):
    _skip_without_cloudflare()
    domain = _domain()
    _apply_real_cert(router, real_cert_paths, domain)

    out = router.ssh("tollgate ssl remove --yes 2>&1", timeout=60)
    assert "error" not in out.lower(), out

    assert not router.ssh_bool(f"test -f {SSL_CERT}"), "cert still exists after remove"
    assert not router.ssh_bool(f"test -d {SSL_BACKUP}"), "backup still exists after remove"

    assert router.uci_get("nodogsplash.@nodogsplash[0].gatewaydomainname") != domain

    dhcp = router.ssh("uci show dhcp 2>/dev/null | grep '=domain' || true")
    assert domain not in dhcp, dhcp

    status = router.ssh("tollgate ssl status 2>&1")
    assert "not configured" in status.lower(), status


# Run the whole module for full lifecycle (test-ssl-real-cert-full / test-ssl-all).
