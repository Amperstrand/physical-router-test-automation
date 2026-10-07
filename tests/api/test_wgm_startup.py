"""WGM (Wireless Gateway Manager) startup check tests.

Verifies that the WGM startup connectivity check (introduced in PR #122)
executes cleanly with and without WiFi radios present.

Feature-detected: all tests skip when WGM startup check log patterns
are absent, so they run against any firmware version that includes
the feature — no ``--expected-pr`` needed.
"""

import re

import pytest

pytestmark = [pytest.mark.story("O1"),pytest.mark.api, pytest.mark.smoke]


def _skip_if_no_startup_check(logs: str):
    """Skip test when WGM startup check is not present in firmware."""
    if not re.search(r"Startup check:", logs):
        pytest.skip("WGM startup check not present in firmware logs")


@pytest.fixture(scope="module")
def wgm_logs(router):
    """Collect recent backend logs containing WGM/startup messages."""
    return router.get_tollgate_logs(lines=500)


@pytest.mark.smoke
def test_wgm_startup_check_executes(wgm_logs):
    """WGM startup connectivity check runs during daemon startup."""
    _skip_if_no_startup_check(wgm_logs)


@pytest.mark.smoke
def test_wgm_startup_completes_cleanly(wgm_logs):
    """Startup check reaches a terminal state (no STA, or STA verified)."""
    _skip_if_no_startup_check(wgm_logs)

    terminal_states = [
        r"Startup check: no active STA, nothing to verify",
        r"Startup check: active STA has internet, all good",
        r"Startup check:.*no working upstream found",
        r"Startup check: candidate found, switching",
    ]
    assert any(re.search(p, wgm_logs) for p in terminal_states), \
        "WGM startup check did not reach a terminal state"


@pytest.mark.smoke
def test_wgm_no_panics_after_startup(wgm_logs):
    """WGM startup produces no panics or fatal errors in backend logs."""
    _skip_if_no_startup_check(wgm_logs)

    panics = re.findall(r"\bpanic\b", wgm_logs, re.IGNORECASE)
    assert not panics, f"Panics detected in backend logs: {panics}"


@pytest.mark.smoke
def test_wgm_grace_period_activates(wgm_logs):
    """WGM main loop starts with grace period after startup check."""
    _skip_if_no_startup_check(wgm_logs)

    assert re.search(r"Startup grace period active", wgm_logs), \
        "WGM startup grace period not logged"


@pytest.mark.smoke
def test_wgm_backend_responsive(router, wgm_logs):
    """Backend API remains healthy after WGM startup sequence."""
    _skip_if_no_startup_check(wgm_logs)

    assert router.api_status("/") == 200, \
        "Backend API not responding after WGM startup"
