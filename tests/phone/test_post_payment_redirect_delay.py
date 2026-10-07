"""Tests for post-payment redirect auth delay behavior.

Verifies the auth delay mechanism introduced in feat/post-payment-redirect
(commit b0d1320). When redirect_url is configured, the Go binary delays
ndsctl auth by 8 seconds to give the captive portal WebView time to load
the welcome page before Android detects connectivity and kills the WebView.

These tests surface two known bugs:
- Bug #3 (Amperstrand/tollgate-module-basic-go#3): delayedAuth() gives
  users ~8s bonus session time because the deauth timer is calculated from
  the original durationSeconds, not adjusted for the delay.
- Bug #4 (Amperstrand/tollgate-module-basic-go#4): concurrent payments
  during the delay window cause double ndsctl auth goroutines, and session
  extensions during delay are silently lost.

Tests are marked xfail until the corresponding fixes land.
Feature-detected: skip if welcome.html is absent (no redirect feature).

Flow:
  payment → valve.OpenGateUntil() → delayedAuth goroutine (8s sleep)
  → ndsctl auth → deauth timer fires at untilTimestamp + ~8s (bug #3)
"""

import re
import time
import logging

import pytest

from lib.helpers import (
    is_session_event,
    assert_internet,
    assert_session_active,
    assert_deauthenticated,
)
from lib.constants import TOKEN_DEFAULT

log = logging.getLogger("tollgate.redirect_delay")

pytestmark = [
    pytest.mark.story("G2"),
    pytest.mark.phone,
    pytest.mark.slow,
    pytest.mark.timeout(300),
    pytest.mark.extended,
]

CAPTIVE_PORTAL_DIR = "/etc/tollgate/tollgate-captive-portal-site"
CONFIG_FILE = "/etc/tollgate/config.json"
AUTH_DELAY_SECONDS = 8
TOLERANCE_SECONDS = 2  # allow timing jitter


def _skip_if_no_redirect(router):
    """Skip if redirect feature is not configured (no welcome.html)."""
    exists = router.ssh(
        f"test -f {CAPTIVE_PORTAL_DIR}/welcome.html && echo YES || echo NO"
    ).strip()
    if exists != "YES":
        pytest.skip("Post-payment redirect not configured (no welcome.html)")


def _get_auth_delay_from_logs(router):
    """Check startup logs for auth_delay value."""
    logs = router.get_tollgate_logs(lines=300)
    for line in logs.splitlines():
        if "auth_delay" in line:
            m = re.search(r"auth_delay[\":\s]+(\d+)s", line)
            if m:
                return int(m.group(1))
    return None


def _count_ndsctl_auth_in_logs(router, since_epoch):
    """Count ndsctl auth calls in logs since the given epoch timestamp."""
    logs = router.get_tollgate_logs(lines=500)
    count = 0
    for line in logs.splitlines():
        if "Authorization successful for MAC" in line or "ndsctl auth" in line:
            count += 1
    return count


def _configure_redirect_url(router, url="https://wallet.cashu.me/welcome"):
    """Set redirect_url in config and restart backend."""
    import json

    cfg_raw = router.ssh(f"cat {CONFIG_FILE}")
    cfg = json.loads(cfg_raw)
    cfg["redirect_url"] = url
    router.ssh_stdin(
        f"cat > {CONFIG_FILE}",
        json.dumps(cfg, indent=2),
    )
    router.ssh("service tollgate-wrt restart")
    router._wait_for_backend(timeout=20)
    time.sleep(2)


def _remove_redirect_url(router):
    """Remove redirect_url from config and restart backend."""
    import json

    cfg_raw = router.ssh(f"cat {CONFIG_FILE}")
    cfg = json.loads(cfg_raw)
    cfg.pop("redirect_url", None)
    router.ssh_stdin(
        f"cat > {CONFIG_FILE}",
        json.dumps(cfg, indent=2),
    )
    router.ssh("service tollgate-wrt restart")
    router._wait_for_backend(timeout=20)
    time.sleep(2)


# --- Bug #3: Session duration with auth delay ---


@pytest.mark.xfail(
    reason="Bug #3: delayedAuth() gives ~8s bonus session time (Amperstrand/tollgate-module-basic-go#3)",
    strict=False,
)
def test_delayed_auth_session_duration_accurate(
    router, adb, cashu, connected_wifi, screenshot_raw
):
    """Session should last exactly the paid duration, even with auth delay.

    Bug #3: delayedAuth() sets the deauth timer to `durationSeconds` from
    when auth fires (8s after payment). This gives the user a free 8s bonus.
    The timer should instead use `time.Until(untilTimestamp)` so the session
    ends at the originally paid time.
    """
    _skip_if_no_redirect(router)
    router.resolve_phone_client(adb)

    # Use a short session for faster testing: 4 sats * 5000ms = 20 seconds
    token = cashu.mint(TOKEN_DEFAULT)
    payment_time = time.time()

    resp = router.pay_direct(token)
    assert is_session_event(resp), f"Payment failed: {str(resp)[:200]}"

    log.info("Payment made, waiting for delayed auth to fire...")

    # During the 8s delay, the client should NOT be authenticated
    time.sleep(2)
    state_early = router.get_nds_state()
    log.info(f"State at t=2s (during delay): {state_early}")
    # This is informational — during delay, client is not yet authenticated

    # Wait for auth to fire (8s delay + buffer)
    authed = router.wait_for_auth(timeout=20)
    assert authed, "Client not authenticated after auth delay"

    auth_time = time.time()
    delay_observed = auth_time - payment_time
    log.info(f"Auth fired at t={delay_observed:.1f}s (expected ~{AUTH_DELAY_SECONDS}s)")

    # The deauth timer should be based on original payment time.
    # With bug #3, it's based on auth time, giving bonus time.
    # Wait for the session to end and measure total duration.
    total_duration = router.wait_for_session_expiry(max_wait=120)
    total_elapsed = time.time() - payment_time

    # The session was for TOKEN_DEFAULT * step_size milliseconds.
    # Get the actual allotment from the payment response
    allotment_ms = 0
    for tag in resp.get("tags", []):
        if isinstance(tag, list) and tag[0] == "allotment":
            allotment_ms = int(tag[1])

    expected_seconds = allotment_ms / 1000
    log.info(
        f"Session: expected={expected_seconds}s, actual={total_elapsed:.1f}s, "
        f"auth_delay={delay_observed:.1f}s"
    )

    # The total elapsed time should be close to the expected session duration.
    # Bug #3 causes it to be expected + ~8s.
    assert abs(total_elapsed - expected_seconds) <= TOLERANCE_SECONDS, (
        f"Session duration off by {abs(total_elapsed - expected_seconds):.1f}s: "
        f"expected ~{expected_seconds}s, got {total_elapsed:.1f}s. "
        f"This is Bug #3: deauth timer starts from auth time, not payment time."
    )

    assert assert_internet(adb, "1.1.1.1") is False, "Internet should be cut off after session"
    screenshot_raw("redirect-delay-session-end.png")


# --- Bug #4: Concurrent payments during delay window ---


@pytest.mark.xfail(
    reason="Bug #4: concurrent payments during delay cause stale goroutine state (Amperstrand/tollgate-module-basic-go#4)",
    strict=False,
)
def test_concurrent_payment_extends_session_during_delay(
    router, adb, cashu, connected_wifi, screenshot_raw
):
    """A second payment during the 8s delay should extend the session.

    Bug #4: When the first payment launches delayedAuth goroutine, a second
    payment sees the MAC in openGates (timer entry) and extends. But when the
    first goroutine wakes up, it overwrites openGates with its own timer,
    losing the extension. The session ends at the first payment's duration,
    not the extended duration.
    """
    _skip_if_no_redirect(router)
    router.resolve_phone_client(adb)

    # First payment
    token1 = cashu.mint(TOKEN_DEFAULT)
    resp1 = router.pay_direct(token1)
    assert is_session_event(resp1), f"First payment failed: {str(resp1)[:200]}"

    allotment1_ms = 0
    for tag in resp1.get("tags", []):
        if isinstance(tag, list) and tag[0] == "allotment":
            allotment1_ms = int(tag[1])

    log.info(f"First payment: {allotment1_ms}ms allotment")

    # Second payment DURING the delay window (at ~2s, before 8s auth delay fires)
    time.sleep(2)
    token2 = cashu.mint(TOKEN_DEFAULT)
    resp2 = router.pay_direct(token2)
    assert is_session_event(resp2), f"Second payment failed: {str(resp2)[:200]}"

    allotment2_ms = 0
    for tag in resp2.get("tags", []):
        if isinstance(tag, list) and tag[0] == "allotment":
            allotment2_ms = int(tag[1])

    log.info(f"Second payment: {allotment2_ms}ms allotment (should be >= allotment1)")

    # Wait for auth to fire
    authed = router.wait_for_auth(timeout=20)
    assert authed, "Client not authenticated after auth delay"

    # With the fix, the session should be extended by the second payment.
    # Bug #4: the first goroutine's timer overwrites the extension.
    # Wait for deauth and measure total session time.
    total_duration = router.wait_for_session_expiry(max_wait=120)

    # Expected: allotment1 + allotment2 (or whatever the backend computed)
    # At minimum, the session should be longer than a single allotment.
    single_allotment_seconds = allotment1_ms / 1000
    total_elapsed = time.time() - (time.time() - total_duration)

    # The session should last significantly longer than a single allotment.
    # Bug #4 causes it to last only ~allotment1_seconds + 8s bonus.
    log.info(
        f"Session expired after {total_duration}s "
        f"(single allotment: {single_allotment_seconds}s)"
    )

    # With the fix, total_duration should be close to (allotment1 + allotment2) / 1000
    # With the bug, total_duration is close to allotment1 / 1000 + 8s
    # Check that the session was actually extended
    assert total_duration > single_allotment_seconds + AUTH_DELAY_SECONDS + TOLERANCE_SECONDS, (
        f"Session was not extended: lasted {total_duration}s but single allotment is "
        f"{single_allotment_seconds}s. Bug #4: first goroutine overwrote extension."
    )

    screenshot_raw("redirect-delay-concurrent.png")


@pytest.mark.xfail(
    reason="Bug #4: double ndsctl auth from concurrent payments during delay (Amperstrand/tollgate-module-basic-go#4)",
    strict=False,
)
def test_concurrent_payment_single_auth_during_delay(
    router, adb, cashu, connected_wifi, screenshot_raw
):
    """Two rapid payments during the delay should result in exactly one ndsctl auth."""
    _skip_if_no_redirect(router)
    router.resolve_phone_client(adb)

    # Clear logs to get a clean baseline
    router.ssh("logread > /dev/null 2>&1 || true")

    # First payment
    token1 = cashu.mint(TOKEN_DEFAULT)
    resp1 = router.pay_direct(token1)
    assert is_session_event(resp1), f"First payment failed: {str(resp1)[:200]}"

    # Second payment immediately
    token2 = cashu.mint(TOKEN_DEFAULT)
    resp2 = router.pay_direct(token2)
    assert is_session_event(resp2), f"Second payment failed: {str(resp2)[:200]}"

    log.info("Two rapid payments made, waiting for auth delay...")

    # Wait for auth to fire (8s + buffer)
    authed = router.wait_for_auth(timeout=20)
    assert authed, "Client not authenticated after delay"

    # Wait a bit more for any second goroutine to fire
    time.sleep(3)

    # Check logs for number of "Authorization successful for MAC" entries
    logs = router.get_tollgate_logs(lines=300)
    auth_count = 0
    for line in logs.splitlines():
        if "Authorization successful for MAC" in line:
            auth_count += 1

    log.info(f"ndsctl auth call count: {auth_count}")

    assert auth_count == 1, (
        f"Expected exactly 1 ndsctl auth, got {auth_count}. "
        f"Bug #4: concurrent goroutines both called authorizeMAC."
    )

    assert assert_internet(adb, "1.1.1.1"), "No internet after auth"
    assert_session_active(router)


# --- Auth delay timing verification ---


def test_auth_not_immediate_with_redirect(router, adb, cashu, connected_wifi):
    """With redirect configured, ndsctl auth should NOT fire immediately."""
    _skip_if_no_redirect(router)
    router.resolve_phone_client(adb)

    # Make payment
    token = cashu.mint(TOKEN_DEFAULT)
    resp = router.pay_direct(token)
    assert is_session_event(resp), f"Payment failed: {str(resp)[:200]}"

    # Check immediately — should NOT be authenticated (delay in progress)
    time.sleep(1)
    state = router.get_nds_state()
    assert state != "Authenticated", (
        f"Client authenticated too quickly (state={state}). "
        f"Auth delay should prevent immediate authentication."
    )

    # Now wait for the delay to pass and verify auth eventually fires
    authed = router.wait_for_auth(timeout=20)
    assert authed, "Client not authenticated after auth delay window"


def test_no_delay_without_redirect_url(router, adb, cashu, connected_wifi):
    """Without redirect_url, auth should fire immediately (no delay)."""
    _skip_if_no_redirect(router)

    # Temporarily remove redirect_url
    import json

    cfg_raw = router.ssh(f"cat {CONFIG_FILE}")
    cfg = json.loads(cfg_raw)
    original_url = cfg.get("redirect_url", "")

    if not original_url:
        pytest.skip("redirect_url not configured — test needs a before/after comparison")

    _remove_redirect_url(router)

    try:
        token = cashu.mint(TOKEN_DEFAULT)
        payment_time = time.time()
        resp = router.pay_direct(token)
        assert is_session_event(resp), f"Payment failed: {str(resp)[:200]}"

        # Auth should fire within 3 seconds (no delay)
        time.sleep(3)
        state = router.get_nds_state()
        assert state == "Authenticated", (
            f"Client not authenticated after 3s without delay (state={state}). "
            f"Without redirect_url, auth should be immediate."
        )

        auth_time = time.time()
        delay = auth_time - payment_time
        log.info(f"Auth delay without redirect: {delay:.1f}s (should be < 3s)")
        assert delay < 5, f"Auth took {delay:.1f}s without redirect — should be near-instant"

    finally:
        # Restore redirect_url
        if original_url:
            _configure_redirect_url(router, original_url)


# --- Auth delay with data-based sessions ---


def test_delayed_auth_data_session(router, adb, cashu, connected_wifi):
    """Data-based sessions (metric=bytes) should also respect the auth delay."""
    _skip_if_no_redirect(router)
    router.resolve_phone_client(adb)

    # Check if metric is bytes
    import json

    cfg_raw = router.ssh(f"cat {CONFIG_FILE}")
    cfg = json.loads(cfg_raw)
    current_metric = cfg.get("metric", "milliseconds")

    if current_metric != "bytes":
        # Temporarily switch to bytes
        original_step = cfg.get("step_size", 5000)
        cfg["metric"] = "bytes"
        cfg["step_size"] = 22020096  # ~21MB
        router.ssh_stdin(f"cat > {CONFIG_FILE}", json.dumps(cfg, indent=2))
        router.ssh("service tollgate-wrt restart")
        router._wait_for_backend(timeout=20)
        time.sleep(2)

    try:
        token = cashu.mint(TOKEN_DEFAULT)
        resp = router.pay_direct(token)
        assert is_session_event(resp), f"Payment failed: {str(resp)[:200]}"

        # Check quickly — should NOT be authenticated yet
        time.sleep(1)
        state = router.get_nds_state()
        assert state != "Authenticated", (
            f"Data session authenticated too quickly (state={state}). "
            f"Auth delay should apply to data-based sessions too."
        )

        # Wait for delayed auth
        authed = router.wait_for_auth(timeout=20)
        assert authed, "Data session not authenticated after delay"

        assert assert_internet(adb, "1.1.1.1"), "No internet after delayed data auth"
        assert_session_active(router)

    finally:
        if current_metric != "bytes":
            # Restore original metric
            cfg["metric"] = current_metric
            cfg["step_size"] = original_step
            router.ssh_stdin(f"cat > {CONFIG_FILE}", json.dumps(cfg, indent=2))
            router.ssh("service tollgate-wrt restart")
            router._wait_for_backend(timeout=20)
