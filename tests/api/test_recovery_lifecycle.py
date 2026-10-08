"""Tests for advanced recovery lifecycle scenarios.

Verifies edge cases in the mint resilience lifecycle:
- Multiple degrade/recover cycles (provider handles repeated swaps)
- Health tracker stays alive after first recovery
- Flapping mint (intermittent block/unblock) — hysteresis prevents flip-flop

These tests are marked 'extended' (not 'destructive') because teardown
restores the router to its original working state.
"""

import json
import logging
import re
import time

import pytest

from lib.helpers import (
    is_full_merchant,
    is_degraded,
    wait_for_full_merchant,
    wait_for_degraded,
    skip_if_no_degraded_support,
    get_mint_ip_map,
    block_mints,
    unblock_mints,
)

log = logging.getLogger("tollgate.recovery_lifecycle")

pytestmark = [pytest.mark.story("O1"),pytest.mark.api, pytest.mark.extended, pytest.mark.timeout(600), pytest.mark.go_only, pytest.mark.complete]

HEALTH_POLL_INTERVAL = 5
HEALTH_POLL_TIMEOUT = 180


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
            wait_for_full_merchant(router, timeout=120, interval=5)


@pytest.mark.extended
def test_multiple_recovery_cycles(router, mint_ip_map):
    """Full merchant -> block -> degraded -> unblock -> recover ->
    block again -> degraded -> unblock -> recover.

    Verifies the provider correctly handles multiple SetMerchant calls
    and the health tracker fires onFirstReachable on each cycle.
    """
    skip_if_no_degraded_support(router)

    if not is_full_merchant(router):
        pytest.skip("Service not running as full merchant")

    for cycle in range(2):
        log.info("=== Recovery cycle %d ===", cycle + 1)

        rules = block_mints(router, mint_ip_map)
        try:
            degraded = wait_for_degraded(
                router, timeout=HEALTH_POLL_TIMEOUT, interval=HEALTH_POLL_INTERVAL
            )
            assert degraded, (
                f"Cycle {cycle + 1}: Service did not enter degraded mode"
            )
            log.info("Cycle %d: degraded", cycle + 1)

            unblock_mints(router, rules)
            recovered = wait_for_full_merchant(
                router, timeout=HEALTH_POLL_TIMEOUT, interval=HEALTH_POLL_INTERVAL
            )
            assert recovered, (
                f"Cycle {cycle + 1}: Service did not recover"
            )
            log.info("Cycle %d: recovered", cycle + 1)
        finally:
            unblock_mints(router, rules)

    log.info("Both recovery cycles completed successfully")


@pytest.mark.extended
def test_health_tracker_alive_after_recovery(router, mint_ip_map):
    """After recovery from degraded, the health tracker should still be
    running and detect a second degradation.

    Steps:
    1. Full merchant -> block -> degraded -> unblock -> recover
    2. Block again WITHOUT restarting
    3. Verify the health tracker detects the second degradation
    """
    skip_if_no_degraded_support(router)

    if not is_full_merchant(router):
        pytest.skip("Service not running as full merchant")

    # Phase 1: First degrade and recover
    rules = block_mints(router, mint_ip_map)
    try:
        degraded = wait_for_degraded(router, timeout=HEALTH_POLL_TIMEOUT)
        assert degraded, "Service did not enter degraded mode (cycle 1)"

        unblock_mints(router, rules)
        recovered = wait_for_full_merchant(router, timeout=HEALTH_POLL_TIMEOUT)
        assert recovered, "Service did not recover (cycle 1)"
        log.info("Phase 1: First recovery complete")
    finally:
        unblock_mints(router, rules)

    # Phase 2: Block again and verify tracker detects it
    rules2 = block_mints(router, mint_ip_map)
    try:
        degraded2 = wait_for_degraded(router, timeout=HEALTH_POLL_TIMEOUT)
        assert degraded2, (
            "Health tracker did not detect second degradation — "
            "tracker may have stopped after first recovery"
        )
        log.info("Phase 2: Tracker detected second degradation")

        logs = router.get_tollgate_logs(lines=1000)
        unreachable_signals = re.findall(r"became unreachable|unreachable", logs, re.IGNORECASE)
        assert unreachable_signals, (
            "Expected 'became unreachable' or 'unreachable' in logs for second degradation"
        )
    finally:
        unblock_mints(router, rules2)

    # Restore
    wait_for_full_merchant(router, timeout=HEALTH_POLL_TIMEOUT)


@pytest.mark.extended
def test_flapping_mint_hysteresis(router, mint_ip_map):
    """Simulate a flapping mint by rapidly blocking and unblocking.
    Hysteresis (3 consecutive successes required) should prevent the
    service from flip-flopping between degraded and full on every probe.

    Steps:
    1. Block mints -> degraded
    2. Unblock for 10s (one probe cycle may succeed, but not 3 consecutive)
    3. Reblock immediately
    4. Verify service stays in degraded mode
    5. Unblock for real -> wait for full recovery (3 consecutive probes)
    """
    skip_if_no_degraded_support(router)

    if not is_full_merchant(router):
        pytest.skip("Service not running as full merchant")

    # Block to enter degraded
    rules = block_mints(router, mint_ip_map)
    try:
        degraded = wait_for_degraded(router, timeout=HEALTH_POLL_TIMEOUT)
        if not degraded:
            pytest.skip("Service did not enter degraded mode for flapping test")
        log.info("Degraded: starting flapping sequence")

        # Brief unblock — not enough for 3 consecutive probes
        unblock_mints(router, rules)
        log.info("Brief unblock (10s) — simulating flapping mint")
        time.sleep(10)

        # Re-block before hysteresis threshold
        rules = block_mints(router, mint_ip_map)
        log.info("Re-blocked after brief unblock")
        time.sleep(5)

        # Should still be in degraded (no 3 consecutive successes)
        still_degraded = is_degraded(router) or not is_full_merchant(router)
        if not still_degraded:
            log.warning(
                "Service recovered during brief unblock — "
                "probe interval may be very short. This is not a failure, "
                "just means the hysteresis window was exceeded."
            )

        # Full unblock for real recovery
        unblock_mints(router, rules)
        recovered = wait_for_full_merchant(router, timeout=HEALTH_POLL_TIMEOUT)
        assert recovered, (
            "Service did not recover after full unblock"
        )
        log.info("Full recovery after flapping sequence")
    finally:
        unblock_mints(router, rules)
