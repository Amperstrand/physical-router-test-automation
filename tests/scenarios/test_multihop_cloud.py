"""
Multi-hop TollGate chain tests.

Tests a configurable chain of N TollGate routers (N >= 3) where:
  - Router[0] (Alpha) is the client-facing reseller
  - Router[1..N-2] are intermediate reseller/merchant hops
  - Router[N-1] is the topmost merchant with direct mint access

Each router pays the one above it for internet access. Payment must
propagate through the full chain.

Requires TOLLGATE_CHAIN_ROUTER_HOSTS to be set (done automatically by
the cloud worker when --routers N is used).
"""

import json
import os
import time

import pytest

from lib.cloud_lab.constants import chain_lan_ip
from lib.helpers import is_full_merchant, is_degraded

pytestmark = [pytest.mark.story("P9"),pytest.mark.api, pytest.mark.extended, pytest.mark.virtual_lab]


def _skip_if_no_chain(chain_routers):
    if not chain_routers or len(chain_routers) < 3:
        pytest.skip("Multi-hop chain not configured (need --routers N with N >= 3)")


def _router_healthy(router) -> bool:
    try:
        body = router.api_body("/")
        data = json.loads(body)
        return data.get("kind") in (10021, 21023)
    except Exception:
        return False


def _router_has_wan(router) -> bool:
    try:
        result = router.ssh("ip addr show eth1 2>/dev/null | grep 'inet '", timeout=10)
        return bool(result.strip())
    except Exception:
        return False


def _router_can_reach(router, target_ip) -> bool:
    try:
        result = router.ssh(f"ping -c 2 -W 3 {target_ip}", timeout=10)
        return "100% packet loss" not in result
    except Exception:
        return False


@pytest.mark.timeout(120)
def test_all_routers_healthy(chain_routers):
    _skip_if_no_chain(chain_routers)
    unhealthy = []
    for i, router in enumerate(chain_routers):
        if not _router_healthy(router):
            unhealthy.append(i)
    assert not unhealthy, f"Routers not healthy: {unhealthy}"


@pytest.mark.timeout(120)
def test_chain_l3_connectivity(chain_routers):
    _skip_if_no_chain(chain_routers)
    failures = []
    for i in range(len(chain_routers) - 1):
        downstream = chain_routers[i]
        upstream = chain_routers[i + 1]

        if not _router_has_wan(downstream):
            failures.append(f"router[{i}] has no WAN (eth1) IP")

        upstream_lan_ip = chain_lan_ip(i + 1)
        if not _router_can_reach(downstream, upstream_lan_ip):
            failures.append(f"router[{i}] cannot reach upstream gateway {upstream_lan_ip}")

    assert not failures, "Chain L3 connectivity failures:\n  " + "\n  ".join(failures)


@pytest.mark.timeout(180)
def test_topmost_router_is_merchant(chain_routers):
    _skip_if_no_chain(chain_routers)
    topmost = chain_routers[-1]

    for _ in range(15):
        if is_full_merchant(topmost):
            return
        time.sleep(2)

    pytest.fail(f"Topmost router[{len(chain_routers) - 1}] is not in full merchant mode")


@pytest.mark.timeout(180)
def test_bottom_router_is_reseller(chain_routers):
    _skip_if_no_chain(chain_routers)
    bottom = chain_routers[0]

    try:
        raw = bottom.ssh("cat /etc/tollgate/config.json 2>/dev/null", timeout=10)
        cfg = json.loads(raw)
        assert cfg.get("reseller_mode") is True, "router[0] reseller_mode is not True"
    except (json.JSONDecodeError, Exception) as e:
        pytest.fail(f"Cannot read router[0] config: {e}")


@pytest.mark.timeout(300)
def test_payment_propagates_through_chain(chain_routers):
    """Verify that the reseller chain is functional: each router can reach
    the internet through its upstream, proving the payment chain works."""
    _skip_if_no_chain(chain_routers)

    # The topmost router should be able to reach the internet directly
    # (via Host NAT on its bridge).
    topmost = chain_routers[-1]
    try:
        result = topmost.ssh("ping -c 2 -W 5 8.8.8.8 2>/dev/null || echo PING_FAIL", timeout=15)
        if "PING_FAIL" in result:
            pytest.skip("Topmost router cannot reach internet (mint may be local-only)")
    except Exception:
        pytest.skip("Cannot test internet reachability from topmost router")

    # Each lower router should be able to reach the internet through
    # its upstream (which requires the payment chain to work).
    failures = []
    for i in range(len(chain_routers) - 1):
        router = chain_routers[i]
        try:
            result = router.ssh("ping -c 3 -W 5 8.8.8.8 2>/dev/null || echo PING_FAIL", timeout=20)
            if "PING_FAIL" in result:
                failures.append(f"router[{i}]")
        except Exception:
            failures.append(f"router[{i}] (SSH error)")

    if failures:
        pytest.fail(
            f"Internet unreachable from routers (payment chain may be broken): {failures}"
        )


@pytest.mark.timeout(120)
def test_no_router_in_degraded_mode(chain_routers):
    _skip_if_no_chain(chain_routers)
    degraded = []
    for i, router in enumerate(chain_routers):
        if is_degraded(router):
            degraded.append(i)
    if degraded:
        pytest.fail(f"Routers in degraded mode: {degraded}")


def test_chain_length_matches_config(chain_routers):
    _skip_if_no_chain(chain_routers)
    expected = int(os.environ.get("TOLLGATE_CHAIN_ROUTER_COUNT", "0"))
    actual = len(chain_routers)
    assert actual == expected, f"Expected {expected} chain routers, got {actual}"
