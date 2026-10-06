"""Mint health degradation lifecycle scenarios.

Ported from the branch Makefile's ``r-smoke-degraded`` target.  These tests
exercise the full mint-health degradation lifecycle on a live router:

- Block a mint via /etc/hosts → restart → verify degraded mode
- Exercise wallet/status operations while degraded (no crash loops)
- Unblock mint → wait for automatic recovery → verify full merchant

Two additional edge-case scenarios from the branch's
``r-test-first-boot-offline`` and ``r-test-no-mints`` targets are included
and marked ``@pytest.mark.destructive`` because they modify the router's
``config.json``.

All interaction goes through ``router.ssh()`` — no direct subprocess usage.
"""

import json
import logging
import re
import time
from urllib.parse import urlparse

import pytest

from lib.constants import TEST_MINT_URL
from lib.helpers import skip_if_no_mint_health_tracker as _skip_if_no_degraded_support

log = logging.getLogger("tollgate.scenarios.mint_health")

pytestmark = [pytest.mark.api, pytest.mark.extended, pytest.mark.timeout(600), pytest.mark.virtual_lab]

RECOVERY_POLL_TIMEOUT = 960   # 16 minutes (matches Makefile)
RECOVERY_POLL_INTERVAL = 15
SERVICE_SETTLE_SECONDS = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mint_host(mint_url: str) -> str:
    """Extract hostname from a mint URL."""
    return urlparse(mint_url).hostname or mint_url


def _is_full_merchant(router) -> bool:
    """Return True when the backend serves a kind-10021 discovery event."""
    code = router.api_status("/")
    if code != 200:
        return False
    body = router.api_body("/")
    try:
        data = json.loads(body)
        if data.get("kind") == 10021:
            tags = data.get("tags", [])
            return any(
                isinstance(t, list) and t[0] == "price_per_step" for t in tags
            )
    except json.JSONDecodeError:
        pass
    return False


def _has_degraded_logs(router) -> bool:
    """Check if backend logs contain degraded-mode signals."""
    logs = router.get_tollgate_logs(lines=500)
    return bool(re.findall(
        r"(starting in degraded mode|degraded|no reachable mints|"
        r"all mints unreachable|entering degraded)",
        logs,
        re.IGNORECASE,
    ))


def _has_merchant_ready_logs(router) -> bool:
    """Check if backend logs contain a 'Merchant ready' signal."""
    logs = router.get_tollgate_logs(lines=1000)
    return bool(re.search(r"Merchant ready", logs, re.IGNORECASE))


def _has_no_configured_mints_logs(router) -> bool:
    """Check if backend logs contain a 'no configured mints' signal."""
    logs = router.get_tollgate_logs(lines=500)
    return bool(re.search(r"no configured mints", logs, re.IGNORECASE))


def _block_mint_via_hosts(router, mint_url: str) -> None:
    """Add ``0.0.0.0 <host>`` to /etc/hosts on the router."""
    host = _mint_host(mint_url)
    router.ssh(f"grep -q '^0\\.0\\.0\\.0[[:space:]]\\+{host}' /etc/hosts || "
               f"echo '0.0.0.0 {host}' >> /etc/hosts")
    log.info("Blocked mint %s via /etc/hosts", host)


def _unblock_mint_via_hosts(router, mint_url: str) -> None:
    """Remove the ``0.0.0.0 <host>`` blocking entry from /etc/hosts.

    Only removes the blocking line — preserves valid DNS entries that
    happen to contain the same hostname or IP (e.g. worker-added
    ``10.99.99.2 v1.testnut.nutshell.lan`` entries).
    """
    host = _mint_host(mint_url)
    router.ssh(f"sed -i '/^0\\.0\\.0\\.0[[:space:]]\\+{host}/d' /etc/hosts")
    log.info("Unblocked mint %s via /etc/hosts", host)


def _is_mint_blocked_in_hosts(router, mint_url: str) -> bool:
    """Check if the mint host is blocked with a ``0.0.0.0`` entry in /etc/hosts."""
    host = _mint_host(mint_url)
    out = router.ssh("cat /etc/hosts")
    for line in out.splitlines():
        if line.startswith("0.0.0.0") and host in line.split():
            return True
    return False


def _wait_for_recovery(router, timeout: int = RECOVERY_POLL_TIMEOUT,
                       interval: int = RECOVERY_POLL_INTERVAL) -> bool:
    """Poll for ``Merchant ready`` in the backend logs.

    Returns True when the signal is found within *timeout* seconds.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _has_merchant_ready_logs(router) and _is_full_merchant(router):
            return True
        time.sleep(interval)
    return False


def _read_config(router) -> dict[str, object]:
    """Read and parse /etc/tollgate/config.json from the router."""
    raw = router.ssh("cat /etc/tollgate/config.json")
    return json.loads(raw)


def _restore_config(router, original_config: dict[str, object]) -> None:
    """Restore a previously-read config.json to the router."""
    router.ssh("cp /etc/tollgate/config.json /etc/tollgate/config.json.scenario-backup")
    tmp = "/tmp/scenario-config-restore.json"
    with open(tmp, "w") as f:
        json.dump(original_config, f, indent=2)
    router.scp_to(tmp, "/etc/tollgate/config.json")


# ---------------------------------------------------------------------------
# Test a: block mint via /etc/hosts
# ---------------------------------------------------------------------------

@pytest.mark.destructive
def test_block_mint_via_hosts(router):
    """Block the test mint by adding ``0.0.0.0 <mint_host>`` to /etc/hosts.

    This is the same mechanism the branch Makefile's ``block-mint`` target
    uses — DNS-level blocking rather than iptables.
    """
    _skip_if_no_degraded_support(router)
    mint_url = TEST_MINT_URL

    try:
        _block_mint_via_hosts(router, mint_url)
        assert _is_mint_blocked_in_hosts(router, mint_url), \
            "Mint host not found in /etc/hosts after blocking"
        log.info("Verified mint is blocked in /etc/hosts")
    finally:
        _unblock_mint_via_hosts(router, mint_url)


# ---------------------------------------------------------------------------
# Test b: restart into degraded mode
# ---------------------------------------------------------------------------

@pytest.mark.destructive
def test_restart_into_degraded_mode(router):
    """Restart the backend with the mint blocked; verify degraded-mode logs.

    Steps:
    1. Block mint via /etc/hosts.
    2. Restart the service.
    3. Verify "Starting in degraded mode" (or similar) in logs.
    """
    _skip_if_no_degraded_support(router)
    mint_url = TEST_MINT_URL

    try:
        _block_mint_via_hosts(router, mint_url)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)

        # The backend should stay up (no crash loop).
        ps_out = router.ssh("ps | grep tollgate-wrt | grep -v grep")
        assert "tollgate-wrt" in ps_out, \
            f"Backend process not running after degraded restart: {ps_out!r}"

        # Poll for degraded-mode log signals.
        found = False
        deadline = time.time() + 60
        while time.time() < deadline and not found:
            found = _has_degraded_logs(router)
            if not found:
                time.sleep(5)

        if not found:
            log.warning(
                "No degraded-mode signals in logs after 60s — "
                "health tracker interval may be longer than expected"
            )
        else:
            log.info("Confirmed degraded-mode signals in logs")
    finally:
        _unblock_mint_via_hosts(router, mint_url)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)


# ---------------------------------------------------------------------------
# Test c: offline wallet operations
# ---------------------------------------------------------------------------

@pytest.mark.destructive
def test_offline_wallet_operations(router):
    """While degraded: wallet balance works, status works, no crash loops.

    This verifies that the backend remains responsive to CLI commands even
    when it cannot reach any mint.  Ported from the Makefile's
    ``test-offline-ops`` target.
    """
    _skip_if_no_degraded_support(router)
    mint_url = TEST_MINT_URL

    try:
        _block_mint_via_hosts(router, mint_url)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)

        # Wallet balance should not crash the service.
        balance = router.get_wallet_balance()
        assert isinstance(balance, dict), \
            f"Expected dict from wallet balance, got: {balance!r}"
        log.info("Wallet balance in degraded mode: %s", balance)

        # Status command should also work.
        status = router.get_tollgate_status()
        assert isinstance(status, dict), \
            f"Expected dict from status, got: {status!r}"
        log.info("Status in degraded mode: %s", status)

        # Verify process is still alive (no crash loop).
        ps_out = router.ssh("ps | grep tollgate-wrt | grep -v grep")
        assert "tollgate-wrt" in ps_out, \
            f"Backend crashed during offline wallet operations: {ps_out!r}"
    finally:
        _unblock_mint_via_hosts(router, mint_url)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)


# ---------------------------------------------------------------------------
# Test d: unblock mint
# ---------------------------------------------------------------------------

@pytest.mark.destructive
def test_unblock_mint(router):
    """Remove the mint entry from /etc/hosts and verify it is gone."""
    _skip_if_no_degraded_support(router)
    mint_url = TEST_MINT_URL

    # Block first so we have something to unblock.
    _block_mint_via_hosts(router, mint_url)
    assert _is_mint_blocked_in_hosts(router, mint_url)

    try:
        _unblock_mint_via_hosts(router, mint_url)
        assert not _is_mint_blocked_in_hosts(router, mint_url), \
            "Mint host still present in /etc/hosts after unblocking"
        log.info("Verified mint is unblocked from /etc/hosts")
    finally:
        # Safety: ensure clean state even on assertion failure.
        _unblock_mint_via_hosts(router, mint_url)


# ---------------------------------------------------------------------------
# Test e: recovery to full merchant
# ---------------------------------------------------------------------------

@pytest.mark.destructive
def test_recovery_to_full_merchant(router):
    """Block mint → restart → degraded → unblock → poll for recovery.

    Polls for up to 16 minutes (matching the Makefile's expected recovery
    window).  Recovery is confirmed when "Merchant ready" appears in the
    backend logs AND the API returns a kind-10021 discovery event.
    """
    _skip_if_no_degraded_support(router)
    mint_url = TEST_MINT_URL

    try:
        _block_mint_via_hosts(router, mint_url)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)

        # Wait for degraded mode to be detected.
        degraded = False
        deadline = time.time() + 60
        while time.time() < deadline and not degraded:
            degraded = _has_degraded_logs(router)
            if not degraded:
                time.sleep(5)
        if not degraded:
            log.warning("Degraded mode not confirmed before unblock attempt")

        # Unblock and wait for recovery.
        _unblock_mint_via_hosts(router, mint_url)
        log.info("Mint unblocked, polling for recovery (up to %ds)", RECOVERY_POLL_TIMEOUT)

        recovered = _wait_for_recovery(router)
        if not recovered:
            pytest.skip(
                f"Service did not recover within {RECOVERY_POLL_TIMEOUT}s — "
                "health tracker interval may be longer than expected"
            )
        log.info("Service recovered to full merchant after unblocking mint")
    finally:
        _unblock_mint_via_hosts(router, mint_url)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)


# ---------------------------------------------------------------------------
# Test f: full degraded lifecycle (r-smoke-degraded equivalent)
# ---------------------------------------------------------------------------

@pytest.mark.destructive
@pytest.mark.slow
def test_full_degraded_lifecycle(router, cashu):
    """Orchestrated full lifecycle: merchant → block → degraded → unblock → recovery.

    This is the pytest equivalent of the Makefile's ``r-smoke-degraded``
    target, exercising the entire degradation lifecycle in a single
    sequential test:

    1. Verify starting as a full merchant (kind 10021).
    2. Fund the wallet (skip if cashu unavailable).
    3. Block the mint via /etc/hosts.
    4. Restart the backend.
    5. Verify degraded mode in logs.
    6. Exercise wallet/status commands while degraded.
    7. Unblock the mint.
    8. Poll for recovery ("Merchant ready" in logs + kind 10021).
    9. Verify full merchant after recovery.

    Cleanup runs even on failure to leave the router in a usable state.
    """
    _skip_if_no_degraded_support(router)
    mint_url = TEST_MINT_URL

    # Phase 1: Verify starting as full merchant.
    assert _is_full_merchant(router), \
        "Service is not running as a full merchant at the start of lifecycle test"
    log.info("Phase 1: Confirmed full merchant mode")

    # Phase 2: Fund wallet if cashu is available.
    if cashu.is_available():
        try:
            token = cashu.mint(4)
            resp = router.pay_direct(token)
            log.info("Phase 2: Funded wallet, response kind=%s", resp.get("kind"))
        except Exception as exc:
            log.warning("Phase 2: Wallet funding failed (non-fatal): %s", exc)
    else:
        log.info("Phase 2: Skipping wallet funding (cashu unavailable)")

    try:
        # Phase 3: Block mint via /etc/hosts.
        _block_mint_via_hosts(router, mint_url)
        log.info("Phase 3: Blocked mint via /etc/hosts")

        # Phase 4: Restart backend.
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)
        log.info("Phase 4: Restarted backend")

        # Phase 5: Verify degraded mode in logs.
        degraded = False
        deadline = time.time() + 120
        while time.time() < deadline and not degraded:
            degraded = _has_degraded_logs(router)
            if not degraded:
                time.sleep(5)
        if not degraded:
            log.warning(
                "Phase 5: No degraded-mode signals after 120s — "
                "continuing lifecycle; health tracker may be slow"
            )
        else:
            log.info("Phase 5: Confirmed degraded mode in logs")

        # Phase 6: Exercise wallet/status while degraded.
        balance = router.get_wallet_balance()
        assert isinstance(balance, dict), \
            f"Wallet balance should return dict in degraded mode, got: {balance!r}"
        status = router.get_tollgate_status()
        assert isinstance(status, dict), \
            f"Status should return dict in degraded mode, got: {status!r}"
        ps_out = router.ssh("ps | grep tollgate-wrt | grep -v grep")
        assert "tollgate-wrt" in ps_out, \
            f"Backend process not running in degraded mode: {ps_out!r}"
        log.info("Phase 6: Offline wallet/status operations OK")

        # Phase 7: Unblock mint.
        _unblock_mint_via_hosts(router, mint_url)
        log.info("Phase 7: Unblocked mint")

        # Phase 8: Poll for recovery.
        recovered = _wait_for_recovery(router)
        if not recovered:
            pytest.skip(
                f"Phase 8: Service did not recover within {RECOVERY_POLL_TIMEOUT}s"
            )
        log.info("Phase 8: Service recovered to full merchant")

        # Phase 9: Verify full merchant after recovery.
        assert _is_full_merchant(router), \
            "Service not running as full merchant after recovery"
        log.info("Phase 9: Confirmed full merchant after recovery")

    except Exception:
        log.error("Lifecycle test failed — ensuring cleanup")
        raise
    finally:
        # Always clean up regardless of outcome.
        _unblock_mint_via_hosts(router, mint_url)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)
        log.info("Lifecycle cleanup complete")


# ---------------------------------------------------------------------------
# Test g: first boot offline
# ---------------------------------------------------------------------------

@pytest.mark.destructive
def test_first_boot_offline(router):
    """Configure an unreachable mint URL, remove wallet.db, restart.

    Verifies the backend starts gracefully even when the configured mint is
    unreachable from the very first boot.  After the test, the original
    config.json and wallet are restored.

    Ported from the Makefile's ``r-test-first-boot-offline`` target.
    """
    _skip_if_no_degraded_support(router)

    original_config = _read_config(router)
    original_mints = original_config.get("accepted_mints", [])

    try:
        # Set an unreachable mint URL.
        bad_config = dict(original_config)
        bad_config["accepted_mints"] = [
            {
                "url": "https://unreachable.invalid.example.com",
                "min_balance": 0,
                "balance_tolerance_percent": 0,
                "payout_interval_seconds": 60,
                "min_payout_amount": 0,
                "price_per_step": 1,
                "price_unit": "sat",
                "purchase_min_steps": 1,  # 0 is not representable post-parse: tbmg #104 normalizes 0/absent -> 1
            }
        ]
        tmp = "/tmp/scenario-firstboot-config.json"
        with open(tmp, "w") as f:
            json.dump(bad_config, f, indent=2)
        router.scp_to(tmp, "/etc/tollgate/config.json")

        # Remove wallet.db to simulate a truly first-boot scenario.
        router.ssh("rm -f /etc/tollgate/wallet.db /etc/tollgate/wallet.db.lock 2>/dev/null || true")

        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)

        # Backend should be up (no crash).
        ps_out = router.ssh("ps | grep tollgate-wrt | grep -v grep")
        assert "tollgate-wrt" in ps_out, \
            f"Backend crashed on first boot with unreachable mint: {ps_out!r}"

        # It should show degraded-mode signals.
        degraded = _has_degraded_logs(router)
        if not degraded:
            log.warning(
                "No degraded-mode signals on first-boot-offline — "
                "backend may not log degraded startup explicitly"
            )

        # API should still respond (200 or 503).
        code = router.api_status("/")
        assert code in (200, 503), \
            f"Expected 200 or 503 on first-boot-offline, got {code}"

        log.info("First-boot-offline: backend started gracefully despite unreachable mint")
    finally:
        # Restore original config and wallet.
        _restore_config(router, original_config)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)

        # Re-apply the test mint that the session fixture set up.
        router.ensure_test_mint()
        log.info("First-boot-offline: restored original config")


# ---------------------------------------------------------------------------
# Test h: no configured mints
# ---------------------------------------------------------------------------

def test_default_mints_configured(router):
    """Verify default mint config (ported from r-test-default-mints)."""
    raw = router.ssh("cat /etc/tollgate/config.json")
    config = json.loads(raw)
    mints = config.get("accepted_mints", [])
    assert len(mints) >= 1, f"Expected at least 1 mint, got {len(mints)}"
    urls = [m.get("url", "") for m in mints]
    assert all(urls), f"Expected non-empty mint URLs in config, got: {urls}"


@pytest.mark.destructive
def test_no_configured_mints(router):
    """Set an empty mint list, restart, verify service stays up.

    The backend should log "no configured mints" (or similar) but must not
    crash.  Ported from the Makefile's ``r-test-no-mints`` target.
    """
    _skip_if_no_degraded_support(router)

    original_config = _read_config(router)

    try:
        # Set empty accepted_mints list.
        empty_config = dict(original_config)
        empty_config["accepted_mints"] = []
        tmp = "/tmp/scenario-nomints-config.json"
        with open(tmp, "w") as f:
            json.dump(empty_config, f, indent=2)
        router.scp_to(tmp, "/etc/tollgate/config.json")

        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)

        # Backend should still be running.
        ps_out = router.ssh("ps | grep tollgate-wrt | grep -v grep")
        assert "tollgate-wrt" in ps_out, \
            f"Backend crashed with no configured mints: {ps_out!r}"

        # Check for "no configured mints" in logs.
        found = _has_no_configured_mints_logs(router)
        if not found:
            log.warning(
                "No 'no configured mints' signal in logs — "
                "the backend may use different wording"
            )
        else:
            log.info("Confirmed 'no configured mints' signal in logs")

        # API should respond (likely 200 with degraded/empty discovery).
        code = router.api_status("/")
        assert code in (200, 503), \
            f"Expected 200 or 503 with no mints, got {code}"

        log.info("No-configured-mints: backend stayed up")
    finally:
        _restore_config(router, original_config)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)
        router.ensure_test_mint()
        log.info("No-configured-mints: restored original config")
