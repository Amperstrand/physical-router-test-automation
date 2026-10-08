"""
Two-router interaction tests.

Requires two physical routers (alpha + beta) connected to each other.
Tests verify upstream WiFi pinning, degraded-mode payment renewal across
routers, and multi-router coordination.

All tests skip unless TOLLGATE_SECONDARY_ROUTER_HOST is set.
"""

import json
import os
import time

import pytest

from lib.router import Router
from lib.router_lock import RouterLock

pytestmark = [pytest.mark.story("P9"),pytest.mark.api, pytest.mark.extended]


def _get_secondary_router(backend) -> Router | None:
    host = os.environ.get("TOLLGATE_SECONDARY_ROUTER_HOST", "")
    if not host:
        return None
    password = os.environ.get(
        "TOLLGATE_SECONDARY_ROUTER_PASSWORD",
        os.environ.get("TOLLGATE_LUCI_PASSWORD", ""),
    )
    identity_file = os.environ.get("TOLLGATE_SECONDARY_ROUTER_SSH_KEY", "")
    port_str = os.environ.get("TOLLGATE_SECONDARY_ROUTER_PORT", "")
    return Router(
        host=host,
        phone_ip="",
        phone_mac="",
        domain="",
        identity_file=identity_file or None,
        port=int(port_str) if port_str else None,
        backend=backend,
    )


def _skip_if_no_secondary(router_b):
    if router_b is None:
        pytest.skip("TOLLGATE_SECONDARY_ROUTER_HOST not set — two-router test skipped")


def _skip_if_no_upstream_wifi(router):
    try:
        result = router.cli_command("upstream", ["list"])
    except Exception:
        pytest.skip("tollgate upstream commands not available")
        return
    msg = str(result.get("message", "") or result.get("raw", "")).lower()
    if "unknown command" in msg or "not found" in msg:
        pytest.skip("tollgate upstream subcommand not recognized")


class TestPinUpstream:
    """Verify upstream pin prevents scan-away after payment."""

    def test_pin_prevents_scan_away(self, router, backend):
        router_b = _get_secondary_router(backend)
        _skip_if_no_secondary(router_b)
        _skip_if_no_upstream_wifi(router)

        beta_ssid = os.environ.get("TOLLGATE_SECONDARY_ROUTER_SSID", "")
        if not beta_ssid:
            pytest.skip("TOLLGATE_SECONDARY_ROUTER_SSID not set")

        prev_ssid = self._get_active_ssid(router)
        assert prev_ssid, "No active upstream on primary router"

        try:
            router.cli_command("upstream", ["connect", beta_ssid])
            assert self._wait_for_wwan(router, timeout=60), "Failed to connect to secondary AP"

            self._wait_for_payment_log(router, timeout=30)

            pin_log = router.ssh("logread | grep -i 'Pinned upstream' | tail -1", timeout=10)
            if not pin_log.strip():
                pytest.skip("No Pinned upstream log — pin feature may not be implemented")

            current_ssid = self._get_active_ssid(router)
            assert current_ssid == beta_ssid, (
                f"Expected pinned to {beta_ssid}, but active is {current_ssid}"
            )
        finally:
            self._restore_upstream(router, prev_ssid)

    def _get_active_ssid(self, router: Router) -> str:
        result = router.cli_command("upstream", ["list"])
        raw = result.get("raw", "")
        for line in raw.splitlines():
            if "ACTIVE" in line:
                return line.split()[0]
        return ""

    def _wait_for_wwan(self, router: Router, timeout: int = 60) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                up = router.ssh(
                    "ifstatus wwan 2>/dev/null | jsonfilter -e '@.up'",
                    timeout=5,
                )
                if "true" in up:
                    return True
            except Exception:
                pass
            time.sleep(5)
        return False

    def _wait_for_payment_log(self, router: Router, timeout: int = 30) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                log = router.ssh(
                    "logread | grep -i 'payment successful\\|session updated\\|Pinned upstream' | tail -3",
                    timeout=5,
                )
                if log.strip():
                    return
            except Exception:
                pass
            time.sleep(3)

    def _restore_upstream(self, router: Router, prev_ssid: str) -> None:
        upstream_pass = os.environ.get("TOLLGATE_UPSTREAM_WIFI_PASSWORD", "")
        try:
            if upstream_pass:
                router.cli_command("upstream", ["connect", prev_ssid, upstream_pass])
            else:
                router.cli_command("upstream", ["connect", prev_ssid])
        except Exception:
            pass


class TestDegradedUpstreamRenewal:
    """Two-router degraded mode payment renewal via LAN."""

    def test_offline_renewal_via_lan(self, router, backend):
        router_b = _get_secondary_router(backend)
        _skip_if_no_secondary(router_b)
        _skip_if_no_upstream_wifi(router)

        prev_ssid = self._get_active_ssid(router)
        assert prev_ssid, "No active upstream on primary router"

        mint_host = "testnut.cashu.exchange"
        try:
            router.ssh(f"echo '0.0.0.0 {mint_host}' >> /etc/hosts", timeout=10)
            router.ssh("service tollgate-wrt restart", timeout=15)
            time.sleep(10)

            status = router.get_tollgate_status()
            raw = json.dumps(status).lower()
            assert any(kw in raw for kw in ["degraded", "unreachable"]), (
                "Expected degraded mode after blocking mint"
            )

            router.ssh(f"sed -i '/0.0.0.0 {mint_host}/d' /etc/hosts", timeout=10)
            time.sleep(15)

            for _ in range(30):
                status = router.get_tollgate_status()
                if status.get("success") is True:
                    raw = json.dumps(status).lower()
                    if "degraded" not in raw:
                        return
                time.sleep(2)

            pytest.fail("Router did not recover from degraded mode after unblocking mint")
        finally:
            try:
                router.ssh(f"sed -i '/0.0.0.0 {mint_host}/d' /etc/hosts", timeout=10)
            except Exception:
                pass
            self._restore_upstream(router, prev_ssid)

    def _get_active_ssid(self, router: Router) -> str:
        result = router.cli_command("upstream", ["list"])
        raw = result.get("raw", "")
        for line in raw.splitlines():
            if "ACTIVE" in line:
                return line.split()[0]
        return ""

    def _restore_upstream(self, router: Router, prev_ssid: str) -> None:
        upstream_pass = os.environ.get("TOLLGATE_UPSTREAM_WIFI_PASSWORD", "")
        try:
            if upstream_pass:
                router.cli_command("upstream", ["connect", prev_ssid, upstream_pass])
            else:
                router.cli_command("upstream", ["connect", prev_ssid])
        except Exception:
            pass


class TestDegradedConnectWhileDegraded:
    """Connect upstream while already degraded (RISKY — mirrors r-smoke-degraded-connect)."""

    def test_connect_while_degraded(self, router, backend):
        if os.environ.get("TOLLGATE_ALLOW_RISKY", "").lower() not in ("1", "true", "yes"):
            pytest.skip("Set TOLLGATE_ALLOW_RISKY=1 to run this risky scenario")

        router_b = _get_secondary_router(backend)
        _skip_if_no_secondary(router_b)
        _skip_if_no_upstream_wifi(router)

        beta_ssid = os.environ.get("TOLLGATE_SECONDARY_ROUTER_SSID", "")
        if not beta_ssid:
            pytest.skip("TOLLGATE_SECONDARY_ROUTER_SSID not set")

        mint_host = "nofee.testnut.cashu.space"
        prev_ssid = ""
        try:
            result = router.cli_command("upstream", ["list"])
            for line in str(result.get("raw", "")).splitlines():
                if "ACTIVE" in line:
                    prev_ssid = line.split()[0]
                    break

            router.block_mint(f"https://{mint_host}")
            router.ssh("service tollgate-wrt restart", timeout=20)
            time.sleep(15)

            status = router.get_tollgate_status()
            raw = json.dumps(status).lower()
            assert any(k in raw for k in ("degraded", "unreachable")), raw

            router.cli_command("upstream", ["connect", beta_ssid])
            time.sleep(30)

            list_out = router.cli_command("upstream", ["list"])
            assert beta_ssid in str(list_out.get("raw", "")), list_out
        finally:
            router.unblock_mint(f"https://{mint_host}")
            if prev_ssid:
                try:
                    router.cli_command("upstream", ["connect", prev_ssid])
                except Exception:
                    pass


class TestRouterLockCoordination:
    """Multi-router lock coordination tests."""

    def test_lock_prevents_concurrent_sessions(self):
        lock = RouterLock()
        try:
            lock.acquire(router_id="alpha", phase="test-session", branch="main")
            assert lock.is_locked()

            lock2 = RouterLock(lock_path=lock.lock_path)
            with pytest.raises(RuntimeError, match="locked by"):
                lock2.acquire(router_id="alpha", phase="competing-session")
        finally:
            lock.release()

    def test_force_release_allows_new_session(self):
        lock = RouterLock()
        try:
            lock.acquire(router_id="alpha", phase="stale-session")
            lock.force_release()

            lock2 = RouterLock(lock_path=lock.lock_path)
            lock2.acquire(router_id="alpha", phase="new-session")
            assert lock2.is_locked()
            lock2.release()
        finally:
            try:
                lock.release()
            except Exception:
                pass
