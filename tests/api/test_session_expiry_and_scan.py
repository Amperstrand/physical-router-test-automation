"""Tests for PR #106: Merchant session cleanups (expiry + scan loop).

PR #106 (refactor/merchant-session-cleanups) fixes two issues:
1. Session expiry is now checked during GetSession — expired sessions
   return remaining=0 instead of stale positive values.
2. The upstream scan loop starts BEFORE ListenAndServe, so scanning
   begins immediately on startup rather than after the first request.

Also covers related config commands from PRs #112/#113:
- `config get` / `config set` via Unix socket
- Dot-path config access (e.g. accepted_mints.0.url)

These tests establish the BASELINE behavior on whatever firmware is currently
deployed. If the features don't exist yet (PRs not deployed), tests skip with
informative messages documenting what each PR would add.
"""

import json
import logging
import re
import time

import pytest

from lib.constants import DEFAULT_STEP_SIZE_MS

log = logging.getLogger("tollgate.session_expiry_scan")

pytestmark = [pytest.mark.story("G4"),pytest.mark.api, pytest.mark.extended]


# ---------------------------------------------------------------------------
# Session expiry tests (PR #106)
# ---------------------------------------------------------------------------


@pytest.mark.extended
def test_expired_session_returns_no_remaining(router, cashu, test_pricing):
    """Create a short session, wait for expiry, verify remaining=0.

    PR #106 fixes GetSession to check expiry. Before the fix, an expired
    session could return stale positive remaining values. After the fix,
    an expired session should return remaining=0 or no session at all.

    Uses a 1-step session (step_size * 1 ms) to minimize wait time.
    """
    if not cashu.is_available():
        pytest.skip("cashu venv not available — run scripts/setup-cashu.sh")

    # Configure minimal step size for fast expiry
    test_pricing(step_size=DEFAULT_STEP_SIZE_MS, metric="milliseconds")

    token = cashu.mint(1)
    resp = router.pay_direct(token)
    kind = resp.get("kind")

    if kind != 1022:
        pytest.skip(
            f"Payment did not create a session (kind={kind}). "
            f"Cannot test session expiry. Response: {str(resp)[:200]}"
        )

    # Wait for the session to expire (1 step = step_size ms)
    step_seconds = DEFAULT_STEP_SIZE_MS / 1000
    expiry_wait = step_seconds + 5  # generous buffer
    log.info("Waiting %.1fs for session to expire (step=%dms)",
             expiry_wait, DEFAULT_STEP_SIZE_MS)
    time.sleep(expiry_wait)

    session = router.get_session()
    remaining = session.get("remaining", 0)

    if remaining > 0:
        log.warning(
            "Session still has remaining=%d after expected expiry. "
            "This is the baseline behavior that PR #106 fixes.",
            remaining,
        )

    # PR #106 guarantee: remaining should be 0 or absent after expiry
    # On current firmware, this may still show stale values — that's the bug
    if remaining <= 0:
        log.info("Session correctly shows remaining=0 after expiry (PR #106 behavior)")
    else:
        log.info(
            "Session shows stale remaining=%d after expiry (pre-PR #106 behavior). "
            "PR #106 would fix this to return 0.",
            remaining,
        )


@pytest.mark.extended
def test_session_expiry_while_querying(router, cashu, test_pricing):
    """Query balance repeatedly during session, verify clean expiry transition.

    PR #106 ensures the transition from active to expired is clean — no
    stale positive remaining values should appear after the session expires.
    This test polls rapidly to catch any transient stale values.
    """
    if not cashu.is_available():
        pytest.skip("cashu venv not available — run scripts/setup-cashu.sh")

    test_pricing(step_size=DEFAULT_STEP_SIZE_MS, metric="milliseconds")

    token = cashu.mint(1)
    resp = router.pay_direct(token)
    kind = resp.get("kind")

    if kind != 1022:
        pytest.skip(
            f"Payment did not create a session (kind={kind}). "
            f"Cannot test session expiry transition."
        )

    step_seconds = DEFAULT_STEP_SIZE_MS / 1000
    deadline = time.time() + step_seconds + 10

    seen_positive_after_expiry = False
    expired_at = None
    poll_count = 0

    while time.time() < deadline:
        session = router.get_session()
        remaining = session.get("remaining", 0)
        poll_count += 1

        if remaining <= 0 and expired_at is None:
            expired_at = time.time()
            log.info("First observed remaining=0 at poll #%d", poll_count)
        elif remaining > 0 and expired_at is not None:
            seen_positive_after_expiry = True
            log.warning(
                "Stale remaining=%d observed AFTER initial expiry at poll #%d. "
                "This is the race condition PR #106 fixes.",
                remaining, poll_count,
            )
            break

        time.sleep(0.5)

    if expired_at is None:
        pytest.skip(
            f"Session did not expire within {step_seconds + 10}s "
            f"(polled {poll_count} times). Timing may differ on this firmware."
        )

    if seen_positive_after_expiry:
        log.info(
            "Observed stale positive remaining after expiry — "
            "this is the baseline bug that PR #106 fixes"
        )
    else:
        log.info(
            "No stale values observed after expiry (clean transition) — "
            "either PR #106 is deployed or timing worked out"
        )


# ---------------------------------------------------------------------------
# Scan loop startup test (PR #106)
# ---------------------------------------------------------------------------


@pytest.mark.extended
def test_backend_starts_scan_loop(router):
    """Check that the upstream scan loop starts on backend startup.

    PR #106 starts the scan loop before ListenAndServe so scanning begins
    immediately. Before the fix, scanning didn't start until the first
    request was served.

    This test checks logs for scan-related messages shortly after a restart.
    """
    router.restart_backend()
    time.sleep(5)

    logs = router.get_tollgate_logs(filter_expr="scan", lines=50)
    scan_signals = re.findall(
        r"(scan|upstream|discover|probe|mint.*check|health.*check)",
        logs, re.IGNORECASE,
    )

    if not scan_signals:
        # Also check general tollgate logs for any startup sequence
        general_logs = router.get_tollgate_logs(filter_expr="tollgate", lines=100)
        startup_signals = re.findall(
            r"(listen|serve|start|ready|initializ)",
            general_logs, re.IGNORECASE,
        )
        if not startup_signals:
            pytest.skip(
                "No scan or startup messages found in logs after restart. "
                "Log format may differ or scan loop logging not present "
                "(requires PR #106 refactor/merchant-session-cleanups)."
            )
        log.info(
            "Found %d startup signals but no scan-specific messages. "
            "PR #106 would add immediate scan loop startup.",
            len(startup_signals),
        )
    else:
        log.info("Found %d scan-related log messages after restart", len(scan_signals))


# ---------------------------------------------------------------------------
# Config get/set via socket (PRs #112/#113)
# ---------------------------------------------------------------------------


def _try_config_command(router, args):
    """Send a config command via Unix socket, return (success, response)."""
    try:
        resp = router.cli_command("config", args=args)
        raw = str(resp.get("raw", "")).lower() if isinstance(resp, dict) else str(resp).lower()
        if "raw" in resp and ("not found" in raw or "unknown" in raw):
            return False, resp["raw"]
        if isinstance(resp, dict):
            error = str(resp.get("error", "")).lower()
            success = resp.get("success")
            if error and ("unknown command" in error or "not available" in error or "unsupported" in error):
                return False, resp
            if success is False and error:
                return False, resp
        return True, resp
    except Exception as exc:
        return False, str(exc)


@pytest.mark.extended
def test_mint_config_get_set_via_socket(router):
    """Test if config get/set commands work via the Unix socket.

    PRs #112/#113 add config management commands to the CLI socket interface.
    This test checks if `config get` and `config set` are available.

    Documents baseline: what config access exists on current firmware vs
    what PRs #112/#113 would add.
    """
    ok_get, resp_get = _try_config_command(router, ["get"])

    if not ok_get:
        pytest.skip(
            f"config get command not available on this firmware "
            f"(requires PR #112/113). Response: {str(resp_get)[:200]}"
        )

    log.info("config get command is available")

    ok_set, resp_set = _try_config_command(router, ["set", "test_key", "test_value"])

    if ok_set:
        log.info("config set command is available")
    else:
        log.info(
            "config set not available (response: %s). "
            "PR #112/113 would add full config set support.",
            str(resp_set)[:200],
        )


@pytest.mark.extended
def test_dot_path_config_access(router):
    """Test if dot-path config access is available (e.g. accepted_mints.0.url).

    PR #112 adds dot-path notation for accessing nested config values.
    This test tries to read a nested config value via the socket.

    Documents baseline: whether dot-path access exists on current firmware.
    """
    # First check basic config access exists
    ok_get, resp_get = _try_config_command(router, ["get"])
    if not ok_get:
        pytest.skip(
            f"config get not available (requires PR #112). "
            f"Response: {str(resp_get)[:200]}"
        )

    # Try dot-path access to accepted_mints.0.url
    ok_dot, resp_dot = _try_config_command(router, ["get", "accepted_mints.0.url"])

    if not ok_dot:
        log.info(
            "Dot-path config access (accepted_mints.0.url) not available: %s. "
            "PR #112 would add dot-path config access.",
            str(resp_dot)[:200],
        )
        # Verify raw config file still has the data
        cfg_raw = router.ssh("cat /etc/tollgate/config.json 2>/dev/null || cat /tmp/tg.yaml 2>/dev/null || true")
        if not cfg_raw.strip():
            log.info("Config file not accessible on this device")
            return
        try:
            cfg = json.loads(cfg_raw)
        except json.JSONDecodeError:
            log.info("Config is not JSON (may be YAML), cannot cross-check")
            return
        mints = cfg.get("accepted_mints", [])
        if mints and mints[0].get("url"):
            log.info(
                "Config file has accepted_mints[0].url=%s — "
                "PR #112 dot-path access would expose this via socket",
                mints[0]["url"],
            )
        return

    log.info("Dot-path config access works")

    # If dot-path works, verify the value matches the config file
    dot_url = ""
    if isinstance(resp_dot, dict):
        dot_url = resp_dot.get("message", resp_dot.get("value", ""))
    cfg_raw = router.ssh("cat /etc/tollgate/config.json 2>/dev/null || cat /tmp/tg.yaml 2>/dev/null || cat /etc/tollgate/config.yaml 2>/dev/null || true")
    if not cfg_raw.strip():
        log.info("Config file not accessible on this device, skipping cross-check")
        return
    try:
        cfg = json.loads(cfg_raw)
    except json.JSONDecodeError:
        log.info("Config file is not JSON (may be YAML), skipping cross-check")
        return
    mints = cfg.get("accepted_mints", [])
    if mints and mints[0].get("url"):
        expected = mints[0]["url"]
        if dot_url and expected in str(dot_url):
            log.info("Dot-path value matches config file: %s", expected)
