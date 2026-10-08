"""Tests for try-all-mints wallet initialization fallback.

Verifies that tollwallet.New() iterates through all configured mints
instead of hardcoding acceptedMints[0]. When the first mint is
unreachable, the wallet should fall back to subsequent mints and the
service should start as a full merchant (not degraded).

Key behaviors under test:
- First mint unreachable, second works -> full merchant
- All mints unreachable -> degraded mode
- Logs show mint fallback messages

Uses http://10.99.99.1:9999 as a guaranteed-unreachable mint URL
(RFC 5737 TEST-NET-1, no standard service on port 9999).
"""

import json
import logging
import re
import time

import pytest

from lib.constants import TEST_MINT_URL
from lib.helpers import (
    is_full_merchant,
    is_degraded,
    skip_if_no_degraded_support,
)

log = logging.getLogger("tollgate.try_all_mints")

pytestmark = [pytest.mark.story("O2"),pytest.mark.api, pytest.mark.extended, pytest.mark.go_only]

UNREACHABLE_MINT = "http://10.99.99.1:9999"
CONFIG_BACKUP_PATH = "/etc/tollgate/config.json.taom-backup"


@pytest.fixture
def config_guard(router):
    router.ssh(f"cp /etc/tollgate/config.json {CONFIG_BACKUP_PATH}")
    original_mints_raw = router.ssh("cat /etc/tollgate/config.json")
    original_mints = json.loads(original_mints_raw).get("accepted_mints", [])
    yield original_mints
    router.ssh(f"cp {CONFIG_BACKUP_PATH} /etc/tollgate/config.json")
    router.ssh(f"rm -f {CONFIG_BACKUP_PATH}")
    router.restart_backend()
    time.sleep(10)


def _set_mints(router, mint_urls):
    cfg_raw = router.ssh("cat /etc/tollgate/config.json")
    cfg = json.loads(cfg_raw)
    new_mints = []
    for url in mint_urls:
        new_mints.append({
            "url": url,
            "min_balance": 0,
            "balance_tolerance_percent": 0,
            "payout_interval_seconds": 86400,
            "min_payout_amount": 999999,
            "price_per_step": 1,
            "price_unit": "sat",
            "purchase_min_steps": 1,  # 0 is not representable post-parse: tbmg #104 normalizes 0/absent -> 1
        })
    cfg["accepted_mints"] = new_mints
    router.write_remote_json("/etc/tollgate/config.json", cfg)
    router.restart_backend()
    time.sleep(15)


@pytest.mark.extended
def test_first_mint_unreachable_second_works(router, config_guard):
    """Configure [unreachable, working] mints. Service should start as
    full merchant using the second mint, NOT degraded."""
    skip_if_no_degraded_support(router)

    _set_mints(router, [UNREACHABLE_MINT, TEST_MINT_URL])

    ps_out = router.ssh("ps | grep tollgate-wrt | grep -v grep")
    assert "tollgate-wrt" in ps_out, (
        f"Backend process not running after try-all-mints: {ps_out!r}"
    )

    assert is_full_merchant(router), (
        "Service should be running as full merchant when second mint is reachable, "
        "but got degraded or non-200 response"
    )

    body = router.api_body("/")
    data = json.loads(body)
    tags = data.get("tags", [])
    price_tags = [
        t for t in tags
        if isinstance(t, list) and t[0] == "price_per_step"
    ]
    assert price_tags, "Full merchant should have price_per_step tags"


@pytest.mark.extended
def test_wallet_logs_show_mint_fallback(router, config_guard):
    """After try-all-mints fallback, logs should show evidence of:
    - A connection failure when the first (unreachable) mint was attempted
    - The working mint being loaded successfully

    We avoid asserting on exact log message templates (which are
    implementation-specific) and instead check for the observable
    signals an operator would rely on.
    """
    skip_if_no_degraded_support(router)

    _set_mints(router, [UNREACHABLE_MINT, TEST_MINT_URL])

    assert is_full_merchant(router), "Service should be full merchant for log check"

    logs = router.get_tollgate_logs(lines=2000)

    # The working mint URL must appear in logs (wallet initialized with it)
    assert TEST_MINT_URL in logs, (
        f"Expected working mint URL {TEST_MINT_URL} in wallet logs"
    )

    # Evidence that the unreachable mint was attempted and failed
    error_indicators = re.findall(
        r"(unreachable|connection refused|dial (?:tcp )?error|timeout|no route to host|connect: connection)",
        logs,
        re.IGNORECASE,
    )
    assert error_indicators, (
        f"Expected connection error for unreachable mint {UNREACHABLE_MINT} in logs"
    )


@pytest.mark.extended
def test_all_mints_unreachable_falls_to_degraded(router, config_guard):
    """Configure [unreachable, unreachable] mints. Service should
    start in degraded mode."""
    skip_if_no_degraded_support(router)

    _set_mints(router, [UNREACHABLE_MINT, "http://10.99.99.1:9998"])

    ps_out = router.ssh("ps | grep tollgate-wrt | grep -v grep")
    assert "tollgate-wrt" in ps_out, (
        f"Backend process not running in degraded mode: {ps_out!r}"
    )

    code = router.api_status("/")
    assert code in (200, 503), (
        f"Expected 200 or 503 with all mints unreachable, got {code}"
    )

    assert is_degraded(router) or not is_full_merchant(router), (
        "Service should be in degraded mode when all mints are unreachable"
    )
