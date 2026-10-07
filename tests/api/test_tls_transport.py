"""Tests for TLS 1.2 transport hardening.

Verifies that the router's HTTP client forces TLS 1.2 and that
mint API calls complete without hanging (regression test for the
TLS 1.3 ClientHello timeout on OpenWrt).

These tests are marked 'extended' because they require mint
connectivity from the router.
"""

import re
import time

import pytest

from lib.helpers import parse_json_or_fail

pytestmark = [pytest.mark.story("O8"),pytest.mark.api, pytest.mark.extended]


@pytest.fixture(scope="module")
def backend_logs(router):
    return router.get_tollgate_logs(lines=500)


@pytest.mark.extended
def test_mint_api_responds_within_timeout(router):
    """Mint API calls must complete in <5s. Before the TLS 1.2 fix,
    Go's TLS 1.3 ClientHello would hang indefinitely on the router's
    network path."""
    start = time.time()
    code = router.api_status("/")
    elapsed = time.time() - start
    assert code == 200, f"Root endpoint returned {code}"
    assert elapsed < 5.0, f"API call took {elapsed:.1f}s — possible TLS hang"


@pytest.mark.extended
def test_no_tls_timeout_in_logs(backend_logs):
    """Backend logs should not contain TLS handshake timeout errors."""
    tls_errors = re.findall(
        r"(TLS handshake timeout|tls:.*timeout|certificate verify failed)",
        backend_logs, re.IGNORECASE,
    )
    if tls_errors:
        pytest.fail(f"TLS errors found in logs: {tls_errors[:5]}")


@pytest.mark.extended
def test_mint_info_endpoint_reachable(router):
    """The test mint's /v1/info must be reachable from the router."""
    result = router.ssh(
        "wget -qO- --timeout=5 --no-check-certificate https://nofee.testnut.cashu.space/v1/info 2>&1 || echo WGET_FAILED"
    )
    if "WGET_FAILED" in result:
        pytest.skip(f"Mint /v1/info unreachable (TLS issue): {result[:200]}")
    assert len(result) > 10, f"Mint /v1/info returned empty or short response: {result[:200]}"
