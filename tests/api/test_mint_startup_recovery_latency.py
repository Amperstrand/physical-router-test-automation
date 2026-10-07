"""Diagnostic: measure startup mint-recovery latency (A/B instrument).

This is the diagnostic instrument for evaluating the "aggressive mint health
check retry on startup" change (upstream commit 32f43d8). It measures how long
the backend takes to recover when mints are unreachable AT STARTUP and then
become reachable again.

Unlike the correctness tests in test_degraded_mode.py (which only assert that
recovery eventually happens), this test MEASURES AND REPORTS the latency. It
deliberately does NOT hard-fail on a tight threshold, so it can be run
diagnostically against both:

  - upstream ``main``   -> expected slow recovery (minutes): the normal 5-minute
                           proactive ticker drives recovery, with the 3-success
                           recovery threshold.
  - patched branch      -> expected fast recovery (~15-30s): the aggressive
                           15s retry with threshold=1 fires at startup because
                           ``reachableCount == 0``.

The measured latency is emitted on a dedicated marker line so it can be
harvested straight out of the cloud run's ``output.log``:

    >>> STARTUP_RECOVERY_LATENCY: 287s <<<

The A/B delta (main vs patched) is the evidence that the startup-retry issue
is real and quantifies its severity:
  - main recovers in minutes, patched in seconds  -> reliability/speed fix
  - main never recovers within the window         -> correctness fix (higher
                                                     severity than assumed)

Scenario:
  1. Resolve mint IPs and block all of them via iptables (mints unreachable).
  2. Restart the backend so it boots with ``reachableCount == 0`` (startup path
     that the aggressive-retry change keys on).
  3. Confirm the backend actually entered a no-reachable-mint state at boot
     (otherwise the test is invalid — the block did not take effect).
  4. ``t0`` = the moment the mints are unblocked.
  5. Poll the router logs for the first recovery signal; record ``t_recover``.
  6. Report the latency. This is a pure measurement instrument: it does NOT
     assert on the latency, so the same green test runs on both main (slow)
     and the patched branch (fast) and the A/B delta is read from the reported
     ``STARTUP_RECOVERY_LATENCY`` numbers.
"""

import json
import logging
import time

import pytest

from lib.helpers import skip_if_no_mint_health_tracker

# Reuse the proven helpers from the degraded-mode suite so this diagnostic
# stays consistent with the canonical block/unblock/restart logic (single
# source of truth).
from tests.api.test_degraded_mode import (
    _block_mints,
    _unblock_mints,
    _get_mint_urls,
    _resolve_mint_ips,
    _restart_and_wait,
    _wait_for_healthy,
    _is_degraded_mode,
)

log = logging.getLogger("tollgate.startup_recovery_latency")

# API-tier, extended suite. The 1200s timeout comfortably covers main's slow
# path (up to ~15 min) while still bounding the run.
pytestmark = [pytest.mark.story("O1"),pytest.mark.api, pytest.mark.extended, pytest.mark.timeout(1200)]

# Generous ceiling: must exceed main's worst-case recovery, which is
# defaultRecoveryThreshold (3) consecutive successes at probeInterval (5min)
# = ~15min. 1200s (20min) gives main room to fully complete its recovery and
# report a clean latency rather than tripping the stuck guard.
STUCK_FOREVER_CEILING_S = 1200  # 20 minutes

# Polling cadence for the recovery signal.
POLL_INTERVAL_S = 5
# Grace window after restart for the health tracker to log a degraded signal
# before we assert the block took effect.
DEGRADED_SIGNAL_GRACE_S = 30


def test_startup_mint_recovery_latency(router):
    """Measure time-to-recovery when mints are unreachable at startup."""
    skip_if_no_mint_health_tracker(router)

    mint_urls = _get_mint_urls(router)
    mint_ip_map = _resolve_mint_ips(router, mint_urls)
    assert mint_ip_map, f"Could not resolve IPs for mints: {mint_urls}"
    log.info("Resolved mint IPs: %s", mint_ip_map)

    # 1. Block all mints BEFORE restart so the backend boots with none
    #    reachable. This is what triggers the startup code path
    #    (reachableCount == 0) that the aggressive-retry change keys on.
    rules = _block_mints(router, mint_ip_map)
    try:
        # 2. Restart -> startup path.
        _restart_and_wait(router)

        # 3. Confirm the backend booted into a no-reachable-mint state. If the
        #    block silently failed, the latency measurement is meaningless —
        #    SKIP cleanly (test preconditions not met) rather than hard-fail,
        #    so we neither report a bogus number nor poison the suite.
        logs = router.get_tollgate_logs(lines=500)
        if not _is_degraded_mode(logs):
            # Degraded signal may lag the health tracker's first probe.
            time.sleep(DEGRADED_SIGNAL_GRACE_S)
            logs = router.get_tollgate_logs(lines=500)
        if not _is_degraded_mode(logs):
            pytest.skip(
                "could not confirm degraded state after restart+mint-block; "
                "block may not have taken effect, so latency measurement is invalid"
            )
        log.info("Confirmed backend booted into degraded state with mints blocked.")

        # 4. t0: the instant mints become reachable again.
        t0 = time.monotonic()
        _unblock_mints(router, rules)
        log.info("Mints unblocked at t0; polling for the first recovery signal...")

        # 5. Poll the discovery endpoint for TRUE user-visible recovery:
        #    kind flips from degraded (21023) to healthy (10021) with
        #    price_per_step tags. This is the same signal _wait_for_healthy
        #    uses. Unlike log-grepping it cannot be fooled by stale restart
        #    lines (the "=== Merchant ready ===" emitted at step 2 persists in
        #    the log tail and would otherwise match instantly -> bogus 0s).
        deadline = t0 + STUCK_FOREVER_CEILING_S
        recovered_at = None
        while time.monotonic() < deadline:
            if router.api_status("/") == 200:
                try:
                    data = json.loads(router.api_body("/"))
                except (json.JSONDecodeError, ValueError):
                    data = {}
                if data.get("kind") == 10021 and any(
                    isinstance(t, list) and t and t[0] == "price_per_step"
                    for t in data.get("tags", [])
                ):
                    recovered_at = time.monotonic()
                    break
            time.sleep(POLL_INTERVAL_S)

        # 6. Report — this is the diagnostic payload. This is a PURE
        # measurement instrument: it reports the latency (or STUCK) and passes
        # either way, so the same green test can run on both main (slow) and
        # the patched branch (fast) and the A/B delta is harvested from the
        # reported numbers. Do NOT add a hard assertion on latency here.
        if recovered_at is None:
            log.error(
                "STARTUP_RECOVERY_LATENCY: STUCK (no recovery within %ds)",
                STUCK_FOREVER_CEILING_S,
            )
            print(f"\n>>> STARTUP_RECOVERY_LATENCY: STUCK (>{STUCK_FOREVER_CEILING_S}s) <<<\n")
        else:
            latency_s = int(recovered_at - t0)
            log.info("STARTUP_RECOVERY_LATENCY: %ds", latency_s)
            print(f"\n>>> STARTUP_RECOVERY_LATENCY: {latency_s}s <<<\n")
    finally:
        # Always remove the iptables block so a failure here cannot poison the
        # rest of the cloud run with a lingering mint block.
        _unblock_mints(router, rules)
        # Restore the suite baseline: this test restarts the backend and may
        # leave it mid-recovery/degraded. Without waiting for health here, the
        # next test in the suite runs against a sick backend and fails
        # spuriously (observed: test_mint_wallet_compat cascade). Best-effort —
        # cleanup failures are logged, not raised, so they never mask the real
        # result or fail this test.
        try:
            _wait_for_healthy(router, timeout=180, interval=5)
        except Exception as exc:  # noqa: BLE001 - cleanup must never mask the result
            log.warning("post-test health restore failed: %s", exc)
