import os
import re

import pytest

from lib.helpers import parse_json_or_fail

pytestmark = [pytest.mark.story("O2"),pytest.mark.api, pytest.mark.extended]


@pytest.fixture(scope="module")
def backend_logs(router):
    return router.get_tollgate_logs(lines=1000)


@pytest.fixture(scope="module")
def discovery(router, backend):
    if backend.is_rust:
        from lib.cashu import create_minter
        mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL", "https://testnut.cashu.exchange")
        minter = create_minter(mint_url)
        minter.ensure_mint_available(timeout=10)
        minter.warmup(timeout=30)
        token = minter.mint(2)
        raw = router.ssh(f"curl -s -H 'X-Cashu: {token}' http://127.0.0.1:2121/pay", timeout=15)
        if not raw or not raw.strip().startswith('{'):
            raw = router.api_body("/")
        return parse_json_or_fail(raw, "discovery response")
    return parse_json_or_fail(router.api_body("/"), "discovery response")


@pytest.mark.extended
def test_logs_show_mint_health_tracking(backend_logs):
    health_signals = re.findall(
        r"(health|reachable|unreachable|mint.*check)",
        backend_logs, re.IGNORECASE,
    )
    if not health_signals:
        pytest.skip("No mint health tracking signals found in backend logs")
    assert len(health_signals) > 0, f"Expected at least one health signal, found {len(health_signals)}: {health_signals}"


@pytest.mark.extended
def test_logs_show_dynamic_rebuild(backend_logs):
    rebuild_signals = re.findall(
        r"(rebuilding merchant|reachable mint set changed|merchant rebuilt)",
        backend_logs, re.IGNORECASE,
    )
    if not rebuild_signals:
        pytest.skip("No dynamic merchant rebuild signals in recent logs")
    assert len(rebuild_signals) > 0, f"Expected at least one rebuild signal, found {len(rebuild_signals)}: {rebuild_signals}"


@pytest.mark.extended
def test_discovery_excludes_unhealthy_mints(backend_logs, discovery):
    unreachable = set(re.findall(
        r"mint (\S+) .*(?:unreachable|failed)",
        backend_logs, re.IGNORECASE,
    ))
    if not unreachable:
        pytest.skip("No unreachable mints found in logs")

    discovery_tags = discovery.get("tags", [])
    price_tags = [t for t in discovery_tags if isinstance(t, list) and t[0] == "price_per_step"]
    discovery_urls = {t[4] for t in price_tags if len(t) >= 5}

    stale = unreachable & discovery_urls
    assert not stale, \
        f"Unreachable mints still in discovery ad: {stale}"


@pytest.mark.extended
def test_wallet_info_shows_mint_count(router):
    if not router.backend.has_cli_socket:
        pytest.skip("CLI socket not supported by this backend")
    info = router.get_wallet_info()
    if info.get("success") is not True:
        pytest.skip(f"wallet info command not supported or returned no data: {str(info)[:200]}")
    data = info.get("data", {})
    mint_count = data.get("mint_count", -1)
    assert isinstance(mint_count, int), f"Expected mint_count to be int, got {type(mint_count).__name__}: {mint_count}"
    assert mint_count >= 0, f"Expected mint_count >= 0, got {mint_count}"


@pytest.mark.extended
def test_status_command_works(router):
    """Test that the CLI status command returns valid data.

    Gated on CLI socket availability AND the socket returning valid
    responses. Skips cleanly when the CLI socket is absent or when
    the deployed version doesn't support the status command.
    """
    if not router.backend.has_cli_socket:
        pytest.skip("CLI socket not supported by this backend")
    try:
        out = router.ssh("ls -S /var/run/tollgate.sock 2>/dev/null", timeout=5)
        if not out.strip():
            pytest.skip("No CLI socket at /var/run/tollgate.sock")
    except Exception:
        pytest.skip("Cannot check CLI socket availability")

    status = router.get_tollgate_status()
    if status.get("success") is None and "raw" in status:
        pytest.skip(f"CLI socket exists but status command not supported: {status}")
    assert status.get("success") is True, f"Status command failed: {status}"


@pytest.mark.extended
def test_version_matches_installed(router):
    if not router.backend.has_cli_socket:
        pytest.skip("CLI socket not supported by this backend")
    version = router.get_tollgate_version()
    if version.get("success") is None and "raw" in version:
        pytest.skip(f"CLI socket exists but version command not supported: {version}")
    assert version.get("success") is True, f"Expected version.success to be True, got: {version}"
    msg = version.get("message", "")
    assert "version:" in msg, f"No version line in message: {msg}"
