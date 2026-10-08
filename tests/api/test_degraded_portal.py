"""Degraded-mode captive portal tests.

Verifies that when mints are unreachable, the captive portal (NDS on port 2050)
still serves the splash page and the backend returns degraded-mode indicators.
After recovery, everything returns to normal.

This complements test_merchant_provider.py which tests HTTP endpoints during
degraded mode but does not check the captive portal surface.
"""
import json
import time

import pytest

from lib.helpers import (
    is_full_merchant,
    is_degraded,
    wait_for_full_merchant,
    wait_for_degraded,
    skip_if_no_mint_health_tracker,
    get_mint_ip_map,
    block_mints,
    unblock_mints,
)

pytestmark = [
    pytest.mark.story("O1"),pytest.mark.api, pytest.mark.extended, pytest.mark.timeout(300), pytest.mark.go_only]


@pytest.fixture(scope="module")
def mint_ip_map(router):
    ip_map = get_mint_ip_map(router)
    if not ip_map:
        pytest.skip("Could not resolve any mint hostnames to IPs")
    return ip_map


@pytest.fixture(autouse=True)
def ensure_full_merchant_and_cleanup(router, mint_ip_map):
    skip_if_no_mint_health_tracker(router)
    if is_degraded(router):
        wait_for_full_merchant(router, timeout=60)
    yield
    output = router.ssh("iptables -L OUTPUT -n 2>/dev/null")
    if "REJECT" in output:
        for url, ip in mint_ip_map.items():
            router.ssh(f"while iptables -C OUTPUT -d {ip} -p tcp --dport 443 -j REJECT 2>/dev/null; do iptables -D OUTPUT -d {ip} -p tcp --dport 443 -j REJECT; done 2>/dev/null || true")
        wait_for_full_merchant(router, timeout=120)



def _portal_up(router):
    """Layout-aware 'portal is serving' check (PRTA #103).

    net4sats layout: /splash.html on NDS :2050 returns 200. The builtin
    layout has no splash.html — NDS answers 500 for it — so "up" there
    is NDS responding on :2050 at all plus the SPA origin responding
    on :2051. Either proves users can reach the portal during degraded
    mode, which is the assertion's intent.
    """
    splash = router.ssh(
        "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:2050/splash.html 2>/dev/null",
        timeout=10,
    ).strip()
    if splash == "200":
        return True, "net4sats splash 200"
    spa = router.ssh(
        "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:2051/ 2>/dev/null",
        timeout=10,
    ).strip()
    nds_alive = splash in ("200", "301", "302", "500", "511")
    return nds_alive and spa not in ("", "000"), (
        f"builtin layout: NDS :2050={splash or 'none'}, SPA :2051={spa or 'none'}"
    )


class TestDegradedPortal:

    def test_portal_serves_during_degraded_mode(self, router, mint_ip_map):
        rules = block_mints(router, mint_ip_map)
        try:
            assert wait_for_degraded(router, timeout=120), "Backend did not enter degraded mode"

            up, detail = _portal_up(router)
            assert up, (
                f"Captive portal returned HTTP {portal} during degraded mode — "
                f"portal must stay up so users can see error messages"
            )
        finally:
            unblock_mints(router, rules)

    def test_backend_returns_degraded_advertisement(self, router, mint_ip_map):
        rules = block_mints(router, mint_ip_map)
        try:
            assert wait_for_degraded(router, timeout=120), "Backend did not enter degraded mode"

            body = router.api_body("/")
            data = json.loads(body)
            kind = data.get("kind", 0)

            has_degraded_kind = kind == 21023
            has_notice_tag = any(
                "no-reachable" in str(t).lower() or "degraded" in str(t).lower()
                for tag in data.get("tags", [])
                for t in tag
            )

            assert has_degraded_kind or has_notice_tag, (
                f"Backend did not signal degraded mode in advertisement. "
                f"kind={kind}, tags={data.get('tags', [])}"
            )
        finally:
            unblock_mints(router, rules)

    def test_portal_recovers_after_mint_restore(self, router, mint_ip_map):
        rules = block_mints(router, mint_ip_map)
        try:
            assert wait_for_degraded(router, timeout=120), "Backend did not enter degraded mode"
        finally:
            unblock_mints(router, rules)

        assert wait_for_full_merchant(router, timeout=120), "Backend did not recover from degraded mode"

        body = router.api_body("/")
        data = json.loads(body)
        assert data.get("kind") == 10021, (
            f"Backend did not return normal advertisement after recovery. "
            f"kind={data.get('kind')}"
        )

        portal = router.ssh(
            "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:2050/splash.html 2>/dev/null",
            timeout=10,
        )
        assert portal.strip() == "200", (
            f"Captive portal returned HTTP {portal} after recovery"
        )

    def test_backend_no_500_during_degraded(self, router, mint_ip_map):
        rules = block_mints(router, mint_ip_map)
        try:
            assert wait_for_degraded(router, timeout=120), "Backend did not enter degraded mode"

            status = router.ssh(
                "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:2121/ 2>/dev/null",
                timeout=10,
            )
            assert status.strip() in ("200",), (
                f"Backend returned HTTP {status} during degraded mode — "
                f"must return 200 with degraded advertisement, not error"
            )
        finally:
            unblock_mints(router, rules)
