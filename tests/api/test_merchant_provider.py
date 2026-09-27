"""Tests for MerchantProvider propagation to CLI and HTTP handlers.

Verifies that after degraded-to-full recovery, all consumers (HTTP handlers,
CLI wallet commands) see the new merchant via MerchantProvider instead of
holding a stale reference.

Key behaviors under test:
- CLI wallet balance/info reflect the swapped merchant after recovery
- All HTTP endpoints return structured degraded responses (not 500/panic)
- All HTTP endpoints work normally after recovery
- Concurrent requests during merchant swap don't cause 500s or panics
"""

import json
import logging
import re
import threading
import time

import pytest

from lib.helpers import (
    is_full_merchant,
    is_degraded,
    wait_for_full_merchant,
    wait_for_degraded,
    skip_if_no_degraded_support,
    skip_if_no_cli_socket,
    get_mint_ip_map,
    block_mints,
    unblock_mints,
)

log = logging.getLogger("tollgate.merchant_provider")

# timeout(900): a test can wait for two full health transitions
# (block→degraded + unblock→full) of up to HEALTH_POLL_TIMEOUT each.
pytestmark = [pytest.mark.api, pytest.mark.extended, pytest.mark.timeout(900), pytest.mark.go_only, pytest.mark.complete]

HEALTH_POLL_INTERVAL = 5
# The backend flips mint health on a 300s proactive probe cadence, so a
# block→degraded or unblock→full transition can legally take up to ~5min
# before the prober notices. 180s expired before the first probe fired.
HEALTH_POLL_TIMEOUT = 420


@pytest.fixture(scope="module")
def mint_ip_map(router):
    ip_map = get_mint_ip_map(router)
    if not ip_map:
        pytest.skip("Could not resolve any mint hostnames to IPs")
    return ip_map


@pytest.fixture(autouse=True)
def cleanup_iptables(router):
    yield
    output = router.ssh("iptables -L OUTPUT -n 2>/dev/null")
    if "REJECT" in output:
        log.info("Found leftover iptables REJECT rules, removing them")
        router.ssh("iptables -D OUTPUT -j REJECT 2>/dev/null || true")
        if not is_full_merchant(router):
            router.restart_backend()
            time.sleep(10)


@pytest.fixture
def degraded_from_full(router, mint_ip_map):
    """Block all mints and wait for degraded mode. Returns (rules, pre_balance).
    Unblocks on teardown."""
    skip_if_no_degraded_support(router)

    if not is_full_merchant(router):
        pytest.skip("Service not running as full merchant")

    skip_if_no_cli_socket(router)
    balance_resp = router.get_wallet_balance()
    pre_balance = balance_resp.get("data", {}).get("balance_sats", 0)

    rules = block_mints(router, mint_ip_map)
    log.info("Blocked %d mint IPs for degraded setup", len(rules))

    degraded = wait_for_degraded(router, timeout=HEALTH_POLL_TIMEOUT, interval=HEALTH_POLL_INTERVAL)
    if not degraded:
        unblock_mints(router, rules)
        pytest.skip(f"Service did not enter degraded mode within {HEALTH_POLL_TIMEOUT}s")

    yield rules, pre_balance

    unblock_mints(router, rules)
    log.info("Cleaned up iptables rules")


@pytest.mark.extended
def test_cli_balance_after_recovery(router, mint_ip_map):
    """After degraded->full recovery, CLI wallet balance should return
    the real balance from the new merchant, not 0 or an error."""
    skip_if_no_degraded_support(router)
    skip_if_no_cli_socket(router)

    if not is_full_merchant(router):
        pytest.skip("Service not running as full merchant")

    balance_before = router.get_wallet_balance()
    pre_balance = balance_before.get("data", {}).get("balance_sats", 0)

    rules = block_mints(router, mint_ip_map)
    try:
        degraded = wait_for_degraded(router, timeout=HEALTH_POLL_TIMEOUT)
        if not degraded:
            pytest.skip("Service did not enter degraded mode")

        unblock_mints(router, rules)
        recovered = wait_for_full_merchant(router, timeout=HEALTH_POLL_TIMEOUT)
        assert recovered, "Service did not recover after unblocking mints"

        balance_after = router.get_wallet_balance()
        assert balance_after.get("success") is True, (
            f"wallet balance command failed after recovery: {balance_after}"
        )
        post_balance = balance_after.get("data", {}).get("balance_sats", 0)
        assert post_balance == pre_balance, (
            f"Balance changed after recovery: {pre_balance} -> {post_balance}. "
            "CLI may be reading from stale merchant."
        )
    finally:
        unblock_mints(router, rules)


@pytest.mark.extended
def test_cli_wallet_info_after_recovery(router, mint_ip_map):
    """After recovery, wallet info should show real mint data, not empty."""
    skip_if_no_degraded_support(router)
    skip_if_no_cli_socket(router)

    if not is_full_merchant(router):
        pytest.skip("Service not running as full merchant")

    rules = block_mints(router, mint_ip_map)
    try:
        degraded = wait_for_degraded(router, timeout=HEALTH_POLL_TIMEOUT)
        if not degraded:
            pytest.skip("Service did not enter degraded mode")

        info_degraded = router.get_wallet_info()
        assert info_degraded.get("success") is True, (
            f"wallet info failed in degraded mode: {info_degraded}"
        )

        unblock_mints(router, rules)
        recovered = wait_for_full_merchant(router, timeout=HEALTH_POLL_TIMEOUT)
        assert recovered, "Service did not recover"

        info_after = router.get_wallet_info()
        assert info_after.get("success") is True, (
            f"wallet info failed after recovery: {info_after}"
        )
        data = info_after.get("data", {})
        assert "mint_count" in data, f"Missing mint_count after recovery: {data}"
        assert data["mint_count"] > 0, (
            f"mint_count should be >0 after recovery, got {data['mint_count']}"
        )
    finally:
        unblock_mints(router, rules)


@pytest.mark.extended
def test_http_endpoints_degraded_responses(router, mint_ip_map):
    """In degraded mode, every HTTP endpoint should return a structured
    response (not 500 or panic)."""
    skip_if_no_degraded_support(router)

    rules = block_mints(router, mint_ip_map)
    try:
        degraded = wait_for_degraded(router, timeout=HEALTH_POLL_TIMEOUT)
        if not degraded:
            pytest.skip("Service did not enter degraded mode")

        code = router.api_status("/")
        assert code in (200, 503), f"GET / returned {code} in degraded mode"

        body = router.api_body("/")
        data = json.loads(body)
        kind = data.get("kind")
        assert kind in (10021, 21023), (
            f"Expected kind 10021 or 21023 in degraded, got {kind}"
        )

        code_health = router.api_status("/health")
        assert code_health in (200, 503), f"/health returned {code_health}"

        code_balance = router.api_status("/balance")
        assert code_balance in (200, 400, 503), (
            f"/balance returned {code_balance} in degraded mode"
        )
    finally:
        unblock_mints(router, rules)


@pytest.mark.extended
def test_http_endpoints_work_after_recovery(router, mint_ip_map):
    """After recovery, all endpoints return normal responses."""
    skip_if_no_degraded_support(router)

    if not is_full_merchant(router):
        pytest.skip("Service not running as full merchant")

    rules = block_mints(router, mint_ip_map)
    try:
        degraded = wait_for_degraded(router, timeout=HEALTH_POLL_TIMEOUT)
        if not degraded:
            pytest.skip("Service did not enter degraded mode")

        unblock_mints(router, rules)
        recovered = wait_for_full_merchant(router, timeout=HEALTH_POLL_TIMEOUT)
        assert recovered, "Service did not recover"

        code = router.api_status("/")
        assert code == 200, f"GET / returned {code} after recovery"
        body = router.api_body("/")
        data = json.loads(body)
        assert data.get("kind") == 10021, (
            f"Expected kind 10021 after recovery, got {data.get('kind')}"
        )

        code_health = router.api_status("/health")
        assert code_health == 200, f"/health returned {code_health} after recovery"
    finally:
        unblock_mints(router, rules)


@pytest.mark.extended
def test_concurrent_requests_during_swap(router, mint_ip_map):
    """Send concurrent GET / requests during degraded->full recovery.
    No request should get a 500 or connection error."""
    skip_if_no_degraded_support(router)

    if not is_full_merchant(router):
        pytest.skip("Service not running as full merchant")

    rules = block_mints(router, mint_ip_map)
    try:
        degraded = wait_for_degraded(router, timeout=HEALTH_POLL_TIMEOUT)
        if not degraded:
            pytest.skip("Service did not enter degraded mode")

        errors = []
        stop_event = threading.Event()
        stop_reason = []

        def requester():
            while not stop_event.is_set():
                try:
                    c = router.api_status("/")
                    if c >= 500:
                        errors.append(f"Got {c} during swap")
                except Exception as e:
                    errors.append(f"Connection error during swap: {e}")
                time.sleep(0.5)

        t = threading.Thread(target=requester, daemon=True)
        t.start()

        # Overall deadline: recovery polling + requester loop are each
        # bounded, but their sum is not — a stalled recovery must end the
        # concurrent window deterministically instead of hanging the suite.
        def _set(reason):
            stop_reason.append(reason)
            stop_event.set()

        recovery_deadline = threading.Timer(
            HEALTH_POLL_TIMEOUT * 2 + 60, _set, args=("overall deadline",)
        )
        recovery_deadline.start()
        try:
            unblock_mints(router, rules)
            log.info("Unblocked mints, concurrent requests running during recovery")

            recovered = wait_for_full_merchant(router, timeout=HEALTH_POLL_TIMEOUT)
        finally:
            recovery_deadline.cancel()
            _set("test window ended")
            t.join(timeout=20)

        assert not errors, (
            f"Errors during concurrent requests: {errors[:5]}"
        )
        assert recovered or ("overall deadline" in stop_reason), (
            "Service did not recover during concurrent test"
        )
        if "overall deadline" in stop_reason:
            pytest.fail(
                "Recovery did not complete within the overall deadline; "
                "concurrent window ended without a verdict"
            )
    finally:
        unblock_mints(router, rules)


@pytest.mark.extended
def test_cli_status_reflects_provider_state(router, mint_ip_map):
    """tollgate status should show wallet_ok=true when full, false when
    degraded, and transition correctly after recovery."""
    skip_if_no_degraded_support(router)
    skip_if_no_cli_socket(router)

    if not is_full_merchant(router):
        pytest.skip("Service not running as full merchant")

    status_before = router.get_tollgate_status()
    assert status_before.get("success") is True, f"status failed: {status_before}"
    data_before = status_before.get("data", status_before)
    wallet_ok_before = data_before.get("wallet_ok", data_before.get("WalletOK", None))
    if wallet_ok_before is not None:
        assert wallet_ok_before is True, (
            f"wallet_ok should be True for full merchant, got {wallet_ok_before}"
        )

    rules = block_mints(router, mint_ip_map)
    try:
        degraded = wait_for_degraded(router, timeout=HEALTH_POLL_TIMEOUT)
        if not degraded:
            pytest.skip("Service did not enter degraded mode")

        status_degraded = router.get_tollgate_status()
        data_degraded = status_degraded.get("data", status_degraded)
        wallet_ok_degraded = data_degraded.get("wallet_ok", data_degraded.get("WalletOK", None))
        if wallet_ok_degraded is not None:
            assert wallet_ok_degraded is False, (
                f"wallet_ok should be False in degraded mode, got {wallet_ok_degraded}"
            )

        unblock_mints(router, rules)
        recovered = wait_for_full_merchant(router, timeout=HEALTH_POLL_TIMEOUT)
        assert recovered, "Service did not recover"

        status_after = router.get_tollgate_status()
        data_after = status_after.get("data", status_after)
        wallet_ok_after = data_after.get("wallet_ok", data_after.get("WalletOK", None))
        if wallet_ok_after is not None:
            assert wallet_ok_after is True, (
                f"wallet_ok should be True after recovery, got {wallet_ok_after}"
            )
    finally:
        unblock_mints(router, rules)
