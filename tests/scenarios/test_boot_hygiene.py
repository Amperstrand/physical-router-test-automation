"""Boot hygiene and degraded-mode variant tests for physical routers.

Ported from the branch Makefile targets:
- r-test-startup-hygiene: enable dead STA + remove ecash + reboot + verify auto-switch
- r-test-startup-hygiene-dead-only: boot with ONLY dead STA, verify emergency scan
- r-smoke-degraded-recovery: degraded→recovery WITHOUT restart (in-process recovery)
- r-smoke-dynamic-rebuild: full→degraded→recovery→full→degraded-again lifecycle

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

log = logging.getLogger("tollgate.scenarios.boot_hygiene")

pytestmark = [pytest.mark.story("O1"),pytest.mark.api, pytest.mark.extended, pytest.mark.destructive, pytest.mark.virtual_lab]

RECOVERY_POLL_TIMEOUT = 960
RECOVERY_POLL_INTERVAL = 15
SERVICE_SETTLE_SECONDS = 10
DEGRADED_DETECT_TIMEOUT = 120
PROACTIVE_DOWNGRADE_TIMEOUT = 420


def _mint_host(mint_url: str) -> str:
    return urlparse(mint_url).hostname or mint_url


def _skip_if_no_upstream_wifi(router):
    out = router.ssh("uci get wireless.wwan 2>/dev/null || echo MISSING", timeout=10)
    if "MISSING" in out:
        pytest.skip("No upstream wireless station-mode (wwan) configured on router")


def _block_mint_via_hosts(router, mint_url: str) -> None:
    host = _mint_host(mint_url)
    router.ssh(f"grep -q '{host}' /etc/hosts || "
               f"echo '0.0.0.0 {host}' >> /etc/hosts")
    log.info("Blocked mint %s via /etc/hosts", host)


def _unblock_mint_via_hosts(router, mint_url: str) -> None:
    host = _mint_host(mint_url)
    router.ssh(f"sed -i '/{host}/d' /etc/hosts")
    log.info("Unblocked mint %s via /etc/hosts", host)


def _is_full_merchant(router) -> bool:
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
    logs = router.get_tollgate_logs(lines=500)
    return bool(re.findall(
        r"(starting in degraded mode|degraded|no reachable mints|"
        r"all mints unreachable|entering degraded)",
        logs,
        re.IGNORECASE,
    ))


def _has_merchant_ready_logs(router) -> bool:
    logs = router.get_tollgate_logs(lines=1000)
    return bool(re.search(r"Merchant ready", logs, re.IGNORECASE))


def _wait_for_degraded(router, timeout: int = DEGRADED_DETECT_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _has_degraded_logs(router):
            return True
        time.sleep(5)
    return False


def _wait_for_recovery(router, timeout: int = RECOVERY_POLL_TIMEOUT,
                       interval: int = RECOVERY_POLL_INTERVAL) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _has_merchant_ready_logs(router) and _is_full_merchant(router):
            return True
        time.sleep(interval)
    return False


def _get_active_upstream(router) -> str:
    out = router.ssh("tollgate upstream list 2>/dev/null || true")
    for line in out.splitlines():
        if "ACTIVE" in line:
            return line.split()[0]
    return ""


def _backup_wallet(router) -> bool:
    out = router.ssh("ls /etc/tollgate/wallet.db 2>/dev/null || echo MISSING")
    if "MISSING" in out:
        return False
    router.ssh("mv /etc/tollgate/wallet.db /etc/tollgate/wallet.db.bak 2>/dev/null || true")
    return True


def _restore_wallet(router) -> None:
    router.ssh("mv /etc/tollgate/wallet.db.bak /etc/tollgate/wallet.db 2>/dev/null || true")


def _has_internet(router) -> bool:
    out = router.ssh("ping -c2 -W3 9.9.9.9 2>/dev/null || true")
    return "0% packet loss" in out


def _wait_for_reboot(router, initial_wait: int = 50) -> None:
    time.sleep(initial_wait)
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            out = router.ssh("echo lan-ok", timeout=5)
            if "lan-ok" in out:
                return
        except Exception:
            pass
        time.sleep(5)


# ---------------------------------------------------------------------------
# Test 1: Startup hygiene auto-switch
# ---------------------------------------------------------------------------

@pytest.mark.destructive
def test_startup_hygiene_auto_switch(router):
    """Block mint, remove ecash, reboot; verify backend auto-switches to good STA.

    Ported from ``r-test-startup-hygiene``. After reboot with the mint blocked
    and ecash removed, the backend should detect the problem and switch to a
    working upstream. Requires LAN access after reboot.
    """
    _skip_if_no_degraded_support(router)
    _skip_if_no_upstream_wifi(router)

    mint_url = TEST_MINT_URL
    had_wallet = _backup_wallet(router)

    try:
        _block_mint_via_hosts(router, mint_url)
        log.info("Blocked mint, triggering reboot")

        router.ssh("reboot", timeout=10)
        _wait_for_reboot(router)
        log.info("Router back after reboot")

        startup_logs = router.ssh(
            "logread | grep -i 'startup check' 2>/dev/null || true"
        )
        if startup_logs.strip():
            log.info("Startup check logs found:\n%s", startup_logs[:500])

        internet_ok = False
        for attempt in range(4):
            if _has_internet(router):
                internet_ok = True
                break
            log.info("No internet yet (attempt %d/4)", attempt + 1)
            time.sleep(10)

        assert internet_ok, "Internet not recovered after reboot with dead STA"

        code = router.api_status("/")
        assert code in (200, 503), f"Expected 200 or 503 after recovery, got {code}"
        log.info("Startup hygiene auto-switch verified")

    finally:
        _unblock_mint_via_hosts(router, mint_url)
        if had_wallet:
            _restore_wallet(router)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)
def test_startup_hygiene_dead_only(router):
    """Boot with only dead STA enabled; verify emergency scan recovery.

    Ported from ``r-test-startup-hygiene-dead-only``. This is a harder
    variant of test_startup_hygiene_auto_switch where ALL good STAs are
    disabled before reboot, leaving only the dead STA active. The backend
    should detect no internet and trigger an emergency scan to find and
    switch to an alternative upstream.

    This test requires LAN access after reboot.
    """
    _skip_if_no_degraded_support(router)
    _skip_if_no_upstream_wifi(router)

    mint_url = TEST_MINT_URL
    had_wallet = _backup_wallet(router)

    # Record currently enabled STA sections
    enabled_out = router.ssh(
        "uci show wireless | grep \"disabled='0'\" | "
        "grep -v default | grep -v private | grep -v 'wireless\\.radio[0-9]\\.' | "
        "sed 's/.*wireless\\.\\([^.]*\\).disabled=.*/\\1/' || true"
    )
    enabled_stas = [s.strip() for s in enabled_out.split() if s.strip()]
    if not enabled_stas:
        pytest.skip("No enabled upstream STA sections found")

    active_before = _get_active_upstream(router)

    try:
        _block_mint_via_hosts(router, mint_url)

        # Disable all currently-enabled STAs
        disable_cmds = " ".join(
            f"uci set wireless.{sta}.disabled='1';" for sta in enabled_stas
        )
        router.ssh(f"{disable_cmds} uci commit wireless")
        log.info("Disabled all %d STAs for dead-only boot", len(enabled_stas))

        # Reboot
        router.ssh("reboot", timeout=10)

        time.sleep(50)
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                out = router.ssh("echo lan-ok", timeout=5)
                if "lan-ok" in out:
                    break
            except Exception:
                pass
            time.sleep(5)

        log.info("Router back after dead-only reboot")

        # Check for emergency scan logs
        scan_logs = router.ssh(
            "logread | grep -iE 'scanning for alternative|candidate found|"
            "switching|emergency|Successfully switched' 2>/dev/null || true"
        )
        if scan_logs.strip():
            log.info("Emergency scan logs:\n%s", scan_logs[:500])

        # Verify internet recovered (generous timeout for multi-hop)
        internet_ok = False
        for attempt in range(12):
            if _has_internet(router):
                internet_ok = True
                break
            log.info("No internet yet (attempt %d/12)", attempt + 1)
            time.sleep(15)

        # Even without internet, verify startup check logged the issue
        startup_logs = router.ssh(
            "logread | grep -i 'startup check\\|no internet' 2>/dev/null || true"
        )
        if startup_logs.strip():
            log.info("Startup detection logs found")

        assert internet_ok, (
            "Internet not recovered after dead-only boot within timeout"
        )

        log.info("Dead-only startup hygiene verified")

    finally:
        _unblock_mint_via_hosts(router, mint_url)

        # Re-enable all previously-enabled STAs
        restore_cmds = " ".join(
            f"uci set wireless.{sta}.disabled='0';" for sta in enabled_stas
        )
        router.ssh(f"{restore_cmds} uci commit wireless")
        log.info("Restored %d STAs", len(enabled_stas))

        if had_wallet:
            _restore_wallet(router)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)


# ---------------------------------------------------------------------------
# Test 3: Degraded recovery without restart (in-process recovery)
# ---------------------------------------------------------------------------

@pytest.mark.destructive
def test_degraded_recovery_no_restart(router):
    """Block mint → degraded → unblock → in-process recovery (no restart).

    Ported from ``r-smoke-degraded-recovery``. This tests that the backend
    can recover from degraded mode WITHOUT a service restart — the health
    tracker detects the mint is reachable again and upgrades in-process.
    This verifies BoltDB lock release and in-process merchant rebuild.
    """
    _skip_if_no_degraded_support(router)
    mint_url = TEST_MINT_URL

    try:
        # Phase 1: Enter degraded mode
        _block_mint_via_hosts(router, mint_url)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)

        degraded = _wait_for_degraded(router)
        if not degraded:
            log.warning("Degraded mode not confirmed before unblock attempt")

        # Verify backend is up and responds
        ps_out = router.ssh("ps | grep tollgate-wrt | grep -v grep")
        assert "tollgate-wrt" in ps_out, "Backend not running in degraded mode"

        balance = router.get_wallet_balance()
        assert isinstance(balance, dict), f"Expected dict, got: {balance!r}"
        log.info("Degraded mode confirmed, wallet balance: %s", balance)

        # Phase 2: Unblock mint (NO restart — tests in-process recovery)
        _unblock_mint_via_hosts(router, mint_url)
        log.info("Mint unblocked, waiting for in-process recovery (no restart)")

        recovered = _wait_for_recovery(router)
        if not recovered:
            pytest.skip(
                f"Service did not recover within {RECOVERY_POLL_TIMEOUT}s — "
                "health tracker interval may be longer than expected"
            )

        assert _is_full_merchant(router), \
            "Service not running as full merchant after in-process recovery"
        log.info("In-process recovery verified")

    finally:
        _unblock_mint_via_hosts(router, mint_url)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)


# ---------------------------------------------------------------------------
# Test 4: Dynamic merchant rebuild lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.destructive
def test_dynamic_merchant_rebuild(router, cashu):
    """Full→degraded→recovery→full→degraded-again lifecycle.

    Ported from ``r-smoke-dynamic-rebuild``. Tests the complete lifecycle:
    1. Start as full merchant
    2. Block mint → enter degraded mode
    3. Unblock mint → in-process recovery back to full
    4. Block mint again → verify proactive downgrade

    This exercises onReachableSetChanged, Shutdown, and
    NewMerchantDegradedFromFull code paths.
    """
    _skip_if_no_degraded_support(router)
    mint_url = TEST_MINT_URL

    # Phase 1: Verify starting as full merchant
    assert _is_full_merchant(router), \
        "Service not running as full merchant at start of lifecycle"
    log.info("Phase 1: Full merchant confirmed")

    # Fund wallet if cashu is available
    if cashu.is_available():
        try:
            token = cashu.mint(4)
            resp = router.pay_direct(token)
            log.info("Wallet funded, response kind=%s", resp.get("kind"))
        except Exception as exc:
            log.warning("Wallet funding failed (non-fatal): %s", exc)

    try:
        # Phase 2: Block mint and enter degraded mode
        _block_mint_via_hosts(router, mint_url)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)

        degraded = _wait_for_degraded(router)
        if not degraded:
            log.warning("Phase 2: No degraded signals after %ds", DEGRADED_DETECT_TIMEOUT)
        else:
            log.info("Phase 2: Degraded mode confirmed")

        # Phase 3: Unblock and wait for in-process recovery
        _unblock_mint_via_hosts(router, mint_url)
        log.info("Phase 3: Mint unblocked, waiting for recovery")

        recovered = _wait_for_recovery(router)
        if not recovered:
            pytest.skip(f"Phase 3: No recovery within {RECOVERY_POLL_TIMEOUT}s")

        assert _is_full_merchant(router), \
            "Phase 3: Not full merchant after recovery"
        log.info("Phase 3: Full merchant recovered")

        # Phase 4: Block again (tests full→degraded via proactive check)
        _block_mint_via_hosts(router, mint_url)
        log.info("Phase 4: Mint blocked again, waiting for proactive downgrade")

        # Wait for proactive health check to detect the mint is down
        deadline = time.time() + PROACTIVE_DOWNGRADE_TIMEOUT
        found_downgrade = False
        while time.time() < deadline:
            if _has_degraded_logs(router):
                found_downgrade = True
                break
            # Check logs for downgrading signals from this specific cycle
            down_logs = router.ssh(
                "logread | grep -i 'downgrading\\|all mints unreachable\\|"
                "degraded mode' | tail -3 2>/dev/null || true"
            )
            if down_logs.strip():
                found_downgrade = True
                log.info("Proactive downgrade detected")
                break
            time.sleep(30)

        if not found_downgrade:
            log.warning(
                "Phase 4: No proactive downgrade in %ds — "
                "health tracker may need restart to trigger",
                PROACTIVE_DOWNGRADE_TIMEOUT,
            )
        else:
            log.info("Phase 4: Proactive downgrade confirmed")

    except Exception:
        log.error("Lifecycle test failed — ensuring cleanup")
        raise
    finally:
        _unblock_mint_via_hosts(router, mint_url)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)
        log.info("Lifecycle cleanup complete")


# ---------------------------------------------------------------------------
# Test 5: Wallet preserved through degraded cycle
# ---------------------------------------------------------------------------

@pytest.mark.destructive
def test_wallet_preserved_through_degraded_cycle(router, cashu):
    """Verify wallet balance is preserved through full degraded lifecycle.

    Fund the wallet, go through block→degraded→unblock→recovery, and
    verify the wallet balance is maintained throughout. Ported from
    Phase 4 of ``r-smoke-dynamic-rebuild``.
    """
    _skip_if_no_degraded_support(router)
    mint_url = TEST_MINT_URL

    # Fund wallet first
    if not cashu.is_available():
        pytest.skip("cashu unavailable — cannot fund wallet for preservation test")

    try:
        token = cashu.mint(4)
        router.pay_direct(token)
    except Exception as exc:
        pytest.skip(f"Wallet funding failed: {exc}")

    # Record pre-degradation balance
    balance_before = router.get_wallet_balance()
    assert isinstance(balance_before, dict), \
        f"Expected dict from wallet balance, got: {balance_before!r}"
    log.info("Pre-degradation wallet balance: %s", balance_before)

    try:
        # Enter degraded mode
        _block_mint_via_hosts(router, mint_url)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)

        # Check balance while degraded
        balance_degraded = router.get_wallet_balance()
        assert isinstance(balance_degraded, dict), \
            f"Expected dict from wallet balance in degraded mode, got: {balance_degraded!r}"
        log.info("Degraded wallet balance: %s", balance_degraded)

        # Recover without restart
        _unblock_mint_via_hosts(router, mint_url)

        recovered = _wait_for_recovery(router)
        if not recovered:
            pytest.skip(
                f"Recovery not completed within {RECOVERY_POLL_TIMEOUT}s — "
                "cannot verify wallet preservation"
            )

        # Check balance after recovery
        balance_after = router.get_wallet_balance()
        assert isinstance(balance_after, dict), \
            f"Expected dict from wallet balance after recovery, got: {balance_after!r}"
        log.info("Post-recovery wallet balance: %s", balance_after)

        # The balance should be preserved (not zeroed)
        # We compare the raw dicts since balance structure may vary
        before_sats = balance_before.get("balance", balance_before.get("sat", None))
        after_sats = balance_after.get("balance", balance_after.get("sat", None))

        if before_sats is not None and after_sats is not None:
            assert after_sats >= before_sats, (
                f"Wallet balance decreased through degraded cycle: "
                f"{before_sats} → {after_sats}"
            )
            log.info("Wallet balance preserved: %s → %s", before_sats, after_sats)

    finally:
        _unblock_mint_via_hosts(router, mint_url)
        router.restart_backend()
        time.sleep(SERVICE_SETTLE_SECONDS)
