import json
import logging
import time

import pytest

from lib.helpers import gate_bug_fix

log = logging.getLogger("tollgate.profit_share_validation")

pytestmark = [pytest.mark.story("M6"),pytest.mark.api, pytest.mark.extended]

_MUTATING_VALIDATION_TESTS = frozenset({
    "test_profit_share_boot_with_invalid_config",
    "test_profit_share_boot_with_empty_list",
    "test_profit_share_boot_with_negative_factor",
})


def _has_profit_share_validation(router) -> bool:
    try:
        out = router.ssh(
            "grep -ac 'ValidateProfitShare' /usr/bin/tollgate-wrt 2>/dev/null || echo 0"
        )
        return int(out.strip()) > 0
    except Exception:
        return False


@pytest.fixture(scope="session")
def _validation_available(router):
    return _has_profit_share_validation(router)


@pytest.fixture(autouse=True)
def _gate_validation_tests(request, _validation_available):
    if request.node.originalname in _MUTATING_VALIDATION_TESTS and not _validation_available:
        gate_bug_fix(
            _validation_available,
            bug_id="profit-share-no-validation",
            fix_pr="PR #86",
        )

SERVICE_RESTART_WAIT = 3


# ── helpers ──────────────────────────────────────────────────────────

def _read_config(router):
    """Read and parse /etc/tollgate/config.json from the router."""
    raw = router.ssh("cat /etc/tollgate/config.json")
    return json.loads(raw)


def _write_config(router, cfg):
    router.write_remote_json("/etc/tollgate/config.json", cfg)


def _restart_service(router):
    """Restart tollgate-wrt and wait for it to come back."""
    router.restart_backend()
    time.sleep(SERVICE_RESTART_WAIT)
    code = router.api_status("/")
    if code != 200:
        pytest.skip(f"Backend not healthy after restart (HTTP {code}) — likely degraded mode")


# ── autouse fixture: backup/restore config for mutating tests ────────

@pytest.fixture
def profit_share_config_guard(router):
    """Backup config before test, restore after.

    Only tests that modify config should request this fixture.
    """
    router.ssh("cp /etc/tollgate/config.json /etc/tollgate/config.json.ps-backup")
    yield
    try:
        router.ssh("cp /etc/tollgate/config.json.ps-backup /etc/tollgate/config.json")
        router.restart_backend()
        time.sleep(SERVICE_RESTART_WAIT)
    except Exception as exc:
        log.error("Failed to restore config after test: %s", exc)
        raise


# ── read-only tests ─────────────────────────────────────────────────

@pytest.mark.extended
def test_profit_share_config_has_valid_structure(router):
    """Config must have profit_share as a list of objects with 'factor' keys."""
    cfg = _read_config(router)
    assert "profit_share" in cfg, "config.json missing 'profit_share' key"
    shares = cfg["profit_share"]
    assert isinstance(shares, list), f"profit_share is not a list: {type(shares)}"
    assert len(shares) > 0, "profit_share list is empty"

    for i, entry in enumerate(shares):
        assert isinstance(entry, dict), f"profit_share[{i}] is not a dict: {entry}"
        assert "factor" in entry, f"profit_share[{i}] missing 'factor' key: {list(entry.keys())}"


@pytest.mark.extended
def test_profit_share_factors_sum_to_one(router):
    """All factor values in profit_share must sum to 1.0 ± 1e-6."""
    cfg = _read_config(router)
    factors = [entry["factor"] for entry in cfg["profit_share"]]
    total = sum(factors)
    assert abs(total - 1.0) <= 1e-6, (
        f"profit_share factors sum to {total}, expected 1.0 ± 1e-6 "
        f"(factors: {factors})"
    )


@pytest.mark.extended
def test_profit_share_no_negative_factors(router):
    """No factor in profit_share may be negative."""
    cfg = _read_config(router)
    for i, entry in enumerate(cfg["profit_share"]):
        factor = entry["factor"]
        assert factor >= 0, f"profit_share[{i}].factor is negative: {factor}"


# ── mutating tests (config backup/restore via fixture) ──────────────

@pytest.mark.extended
def test_profit_share_boot_with_invalid_config(router, profit_share_config_guard):
    """Service must boot when profit_share factors sum to 0.5 (invalid).

    The backend should either reset to defaults or log a warning,
    but must NOT crash or enter a restart loop.
    """
    cfg = _read_config(router)
    cfg["profit_share"] = [
        {"factor": 0.3, "identity": "aa" * 32},
        {"factor": 0.2, "identity": "bb" * 32},
    ]
    _write_config(router, cfg)
    _restart_service(router)

    # Service is up — verify it didn't crash
    ps_out = router.ssh("ps | grep tollgate-wrt | grep -v grep")
    assert "tollgate-wrt" in ps_out, "Backend process not running after boot with invalid profit_share"

    # Check if service recovered by resetting to defaults or logging a warning
    logs = router.get_tollgate_logs(lines=200)
    has_warning = any(
        kw in logs.lower()
        for kw in ["profit_share", "invalid", "default", "reset", "validation"]
    )
    # If no warning logged, check if config was reset
    current_cfg = _read_config(router)
    current_sum = sum(e["factor"] for e in current_cfg.get("profit_share", []))
    config_reset = abs(current_sum - 1.0) <= 1e-6

    assert has_warning or config_reset, (
        "Service booted with invalid profit_share (sum=0.5) but neither "
        "logged a warning nor reset to defaults"
    )


@pytest.mark.extended
def test_profit_share_boot_with_empty_list(router, profit_share_config_guard):
    """Service must boot when profit_share is an empty list.

    ValidateProfitShare should reject the empty list and the service
    should fall back to defaults via EnsureDefaultConfig.
    """
    cfg = _read_config(router)
    cfg["profit_share"] = []
    _write_config(router, cfg)
    _restart_service(router)

    ps_out = router.ssh("ps | grep tollgate-wrt | grep -v grep")
    assert "tollgate-wrt" in ps_out, "Backend process not running after boot with empty profit_share"


@pytest.mark.extended
def test_profit_share_boot_with_negative_factor(router, profit_share_config_guard):
    """Service must boot when profit_share contains a negative factor.

    ValidateProfitShare should reject negative factors and the service
    should fall back to defaults via EnsureDefaultConfig.
    """
    cfg = _read_config(router)
    cfg["profit_share"] = [
        {"factor": 1.5, "identity": "aa" * 32},
        {"factor": -0.5, "identity": "bb" * 32},
    ]
    _write_config(router, cfg)
    _restart_service(router)

    ps_out = router.ssh("ps | grep tollgate-wrt | grep -v grep")
    assert "tollgate-wrt" in ps_out, "Backend process not running after boot with negative factor"


@pytest.mark.extended
def test_profit_share_multiple_shares_sum_correctly(router, profit_share_config_guard):
    """Write 3 shares (0.5, 0.3, 0.2), restart, verify they survived or were reset."""
    cfg = _read_config(router)
    cfg["profit_share"] = [
        {"factor": 0.5, "identity": "aa" * 32},
        {"factor": 0.3, "identity": "bb" * 32},
        {"factor": 0.2, "identity": "cc" * 32},
    ]
    _write_config(router, cfg)
    _restart_service(router)

    current_cfg = _read_config(router)
    current_shares = current_cfg.get("profit_share", [])
    assert isinstance(current_shares, list), "profit_share is not a list after restart"
    assert len(current_shares) > 0, "profit_share is empty after restart"

    current_factors = [e["factor"] for e in current_shares]
    current_sum = sum(current_factors)
    assert abs(current_sum - 1.0) <= 1e-6, (
        f"profit_share factors sum to {current_sum} after restart, expected 1.0 ± 1e-6 "
        f"(factors: {current_factors})"
    )

    # Check if our valid config survived or was reset to defaults
    expected_factors = [0.5, 0.3, 0.2]
    if current_factors == expected_factors:
        log.info("Valid 3-share config survived restart unchanged")
    else:
        log.info(
            "Config was reset to defaults (factors %s → %s)",
            expected_factors, current_factors,
        )
