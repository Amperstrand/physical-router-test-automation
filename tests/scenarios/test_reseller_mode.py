"""Virtualizable reseller-mode scenarios.

These tests intentionally avoid RF/WiFi assertions. They exercise the parts of
PR 118/122 behavior that should work in all environments: physical routers,
the on-prem QEMU lab, and GCP nested KVM.
"""

from __future__ import annotations

import json
import os

import pytest

from lib.constants import TEST_MINT_URL
from lib.router import Router
from lib.reseller_mode import (
    block_host_via_hosts,
    get_reseller_mode,
    get_status_text,
    has_degraded_mode_support,
    reseller_mode,
    restart_and_wait,
    unblock_host_via_hosts,
    wait_for_status_without,
)

pytestmark = [
    pytest.mark.story("M6"),
    pytest.mark.api,
    pytest.mark.extended,
    pytest.mark.virtual_lab,
    pytest.mark.reseller_scenario,
    pytest.mark.go_only,
    pytest.mark.timeout(180),
]


def _skip_unless_enabled():
    if os.environ.get("TOLLGATE_ENABLE_RESELLER_SCENARIOS") != "1":
        pytest.skip("set TOLLGATE_ENABLE_RESELLER_SCENARIOS=1 to run reseller scenarios")


def _require_secondary_router(secondary_router: Router | None) -> Router:
    if secondary_router is None:
        pytest.fail(
            "TOLLGATE_ENABLE_RESELLER_SCENARIOS=1 requires "
            "TOLLGATE_SECONDARY_ROUTER_HOST; refusing to silently skip reseller coverage"
        )
    return secondary_router


def _skip_if_no_reseller_mode(router: Router) -> None:
    current = get_reseller_mode(router)
    if current == "" or current.lower().startswith("uci:"):
        pytest.skip("reseller_mode UCI key unavailable on this build")


def test_reseller_mode_toggle_persists_and_cli_stays_ready(router: Router) -> None:
    _skip_unless_enabled()
    _skip_if_no_reseller_mode(router)

    before = get_reseller_mode(router)
    with reseller_mode(router, enabled=True):
        assert get_reseller_mode(router) == "1"
        status = router.get_tollgate_status()
        assert status, "status command returned an empty response in reseller mode"
        raw = json.dumps(status).lower()
        assert "unknown command" not in raw

    assert get_reseller_mode(router) == before


def test_reseller_mode_suppresses_upstream_scan_churn(router: Router) -> None:
    _skip_unless_enabled()
    _skip_if_no_reseller_mode(router)

    with reseller_mode(router, enabled=True):
        before = router.get_tollgate_logs(filter_expr="upstream", lines=80)
        restart_and_wait(router, settle_seconds=10)
        after = router.get_tollgate_logs(filter_expr="upstream", lines=120)

    combined = f"{before}\n{after}".lower()
    forbidden = (
        "scanning for alternative",
        "candidate found",
        "emergency scan",
        "scan cycle",
    )
    assert not any(term in combined for term in forbidden), combined[-1200:]


def test_reseller_router_recovers_from_mint_dns_block(router: Router) -> None:
    _skip_unless_enabled()
    if not has_degraded_mode_support(router):
        pytest.skip("degraded mode / mint health support not detected")

    mint_host = TEST_MINT_URL.removeprefix("https://").split("/", 1)[0]
    try:
        with reseller_mode(router, enabled=True):
            block_host_via_hosts(router, mint_host)
            restart_and_wait(router, settle_seconds=10)
            blocked_status = get_status_text(router)
            assert any(token in blocked_status for token in ("degraded", "unreachable")), (
                "expected degraded/unreachable status after blocking mint, got: "
                f"{blocked_status[:800]}"
            )

            unblock_host_via_hosts(router, mint_host)
            restart_and_wait(router, settle_seconds=8)
            assert wait_for_status_without(router, "degraded", timeout=90), (
                "router did not leave degraded state after mint DNS unblock"
            )
    finally:
        unblock_host_via_hosts(router, mint_host)


def test_secondary_router_cli_available_when_configured(secondary_router: Router | None) -> None:
    _skip_unless_enabled()
    seller = _require_secondary_router(secondary_router)

    version = seller.get_tollgate_version()
    assert version, "secondary router version command returned an empty response"
    status = seller.get_tollgate_status()
    assert status, "secondary router status command returned an empty response"


def test_reseller_can_reach_secondary_router_when_configured(
    router: Router,
    secondary_router: Router | None,
) -> None:
    _skip_unless_enabled()
    seller = _require_secondary_router(secondary_router)

    probe = router.ssh(
        f"ping -c 1 -W 3 {seller.host} >/dev/null 2>&1 && echo REACHABLE || echo UNREACHABLE",
        timeout=8,
    )
    assert "REACHABLE" in probe, (
        f"primary/reseller router cannot reach secondary/seller router at {seller.host}"
    )
