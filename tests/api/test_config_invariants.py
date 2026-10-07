"""Config validation invariants for the TollGate backend.

Guards against regressions of historically painful config bugs:

* #35 — ``profit_share`` factors did not sum to 1.0, silently producing
  incorrect payout splits.
* #175 — ``config_version`` was not bumped on schema changes, breaking
  migration detection.

These tests read the live config from ``/etc/tollgate/config.json`` on the
router and assert the invariants that, when violated, have caused production
incidents.
"""
import json
from urllib.parse import urlparse

import pytest

from lib.helpers import parse_json_or_fail

pytestmark = [pytest.mark.story("O4"),pytest.mark.api, pytest.mark.go_only, pytest.mark.smoke]


# ── helpers ──────────────────────────────────────────────────────────

def _read_config(router):
    """Read and parse /etc/tollgate/config.json from the router."""
    raw = router.ssh("cat /etc/tollgate/config.json")
    return parse_json_or_fail(raw, "config.json")


def _get_discovery(router):
    """Fetch the GET / discovery event and parse it as JSON."""
    body = router.api_body("/")
    return parse_json_or_fail(body, "discovery response")


# ── invariants ───────────────────────────────────────────────────────

@pytest.mark.smoke
def test_config_has_valid_version(router):
    """Discovery endpoint returns a healthy NIP-26 event and config carries a version.

    Regression guard for #175: ``config_version`` must be present so schema
    migrations can detect it. The GET / endpoint must return ``kind=10021``
    (a healthy discovery event), not an error/degraded payload.
    """
    event = _get_discovery(router)
    assert event.get("kind") == 10021, (
        f"Expected kind=10021 discovery event, got kind={event.get('kind')} "
        f"(backend may be in degraded mode): {str(event)[:200]}"
    )

    cfg = _read_config(router)
    assert "config_version" in cfg, (
        "config.json missing 'config_version' key — migrations cannot detect "
        "schema version (regression of #175)"
    )
    version = cfg["config_version"]
    assert version, f"'config_version' is empty/falsy in config.json: {version!r}"


@pytest.mark.smoke
def test_profit_shares_sum_to_one(router):
    """All ``profit_share`` factors must sum to 1.0 within ±0.01.

    Regression guard for #35: a profit_share list whose factors did not sum
    to 1.0 caused payouts to be silently mis-split.
    """
    cfg = _read_config(router)
    shares = cfg.get("profit_share")
    assert isinstance(shares, list) and shares, (
        f"profit_share must be a non-empty list, got: {shares!r}"
    )
    factors = []
    for i, entry in enumerate(shares):
        assert isinstance(entry, dict) and "factor" in entry, (
            f"profit_share[{i}] missing 'factor': {entry!r}"
        )
        factors.append(entry["factor"])
    total = sum(factors)
    assert abs(total - 1.0) <= 0.01, (
        f"profit_share factors sum to {total}, expected 1.0 ± 0.01 "
        f"(factors: {factors}) — regression of #35"
    )


@pytest.mark.smoke
def test_accepted_mints_non_empty(router):
    """Config must list at least one accepted mint with a valid URL.

    An empty or malformed accepted_mints list leaves the router unable to
    accept any payment.
    """
    cfg = _read_config(router)
    mints = cfg.get("accepted_mints")
    assert isinstance(mints, list) and mints, (
        f"accepted_mints must be a non-empty list, got: {mints!r}"
    )
    for i, mint in enumerate(mints):
        assert isinstance(mint, dict), f"accepted_mints[{i}] is not an object: {mint!r}"
        url = mint.get("url")
        assert url, f"accepted_mints[{i}] missing 'url': {list(mint.keys())}"
        parsed = urlparse(url)
        assert parsed.scheme in ("http", "https") and parsed.netloc, (
            f"accepted_mints[{i}].url is not a valid URL: {url!r}"
        )


@pytest.mark.smoke
def test_config_survives_restart(router):
    """Config must be byte-for-byte identical across a backend restart.

    A restart must not mutate, normalize, or drop config fields (including
    ``config_version``). Silent rewrites on boot indicate a migration or
    normalization bug.
    """
    before = _read_config(router)
    before_raw = router.ssh("cat /etc/tollgate/config.json")

    router.restart_backend(timeout=45)

    after = _read_config(router)
    after_raw = router.ssh("cat /etc/tollgate/config.json")

    assert before == after, (
        "Config changed across backend restart (parsed diff):\n"
        f"before keys: {sorted(before.keys())}\n"
        f"after keys:  {sorted(after.keys())}"
    )
    assert before_raw == after_raw, (
        "Config file content changed across backend restart (raw text differs)."
    )


@pytest.mark.smoke
def test_config_step_size_positive(router):
    """``step_size`` must be a positive integer.

    A non-positive step_size produces zero-length or infinite billing steps.
    """
    cfg = _read_config(router)
    assert "step_size" in cfg, "config.json missing 'step_size' key"
    step_size = cfg["step_size"]
    assert isinstance(step_size, (int, float)) and not isinstance(step_size, bool), (
        f"step_size must be numeric, got {type(step_size).__name__}: {step_size!r}"
    )
    assert step_size > 0, f"step_size must be > 0, got {step_size}"


@pytest.mark.smoke
def test_config_metric_valid(router):
    """``metric`` must be one of the supported billing dimensions.

    Only ``bytes`` and ``milliseconds`` are valid; anything else breaks
    usage accounting.
    """
    cfg = _read_config(router)
    assert "metric" in cfg, "config.json missing 'metric' key"
    metric = cfg["metric"]
    assert metric in ("bytes", "milliseconds"), (
        f"metric must be 'bytes' or 'milliseconds', got {metric!r}"
    )
