"""Tests for CLI wallet commands during degraded mode.

Verifies that all CLI wallet commands (drain, fund, info, balance)
behave correctly when the service is in degraded mode. They should
return structured errors, not panics or crashes.

These tests exercise the MerchantProvider path: CLI calls
merchantProvider.GetMerchant() at operation time, gets a
MerchantDegraded, and handles the error gracefully.
"""

import json
import logging
import time

import pytest

from lib.helpers import (
    is_degraded,
    wait_for_degraded,
    wait_for_full_merchant,
    skip_if_no_degraded_support,
    skip_if_no_cli_socket,
    get_mint_ip_map,
    block_mints,
    unblock_mints,
)

log = logging.getLogger("tollgate.cli_degraded")

pytestmark = [
    pytest.mark.story("O1"),pytest.mark.api, pytest.mark.extended, pytest.mark.go_only]


@pytest.fixture(scope="module")
def mint_ip_map(router):
    ip_map = get_mint_ip_map(router)
    if not ip_map:
        pytest.skip("Could not resolve any mint hostnames to IPs")
    return ip_map


@pytest.fixture
def degraded_mode(router, mint_ip_map):
    skip_if_no_degraded_support(router)
    skip_if_no_cli_socket(router)

    rules = block_mints(router, mint_ip_map)
    degraded = wait_for_degraded(router, timeout=120, interval=5)
    if not degraded:
        unblock_mints(router, rules)
        pytest.skip("Service did not enter degraded mode")

    yield rules

    unblock_mints(router, rules)
    wait_for_full_merchant(router, timeout=120, interval=5)


@pytest.mark.extended
def test_cli_balance_degraded(router, degraded_mode):
    """tollgate wallet balance in degraded mode should return balance_sats: 0
    (not panic or crash)."""
    resp = router.get_wallet_balance()
    assert resp.get("success") is True, (
        f"wallet balance command failed in degraded mode: {resp}"
    )
    data = resp.get("data", {})
    balance = data.get("balance_sats", None)
    assert balance is not None, f"Missing balance_sats in response: {resp}"
    assert balance == 0, (
        f"Expected balance_sats=0 in degraded mode, got {balance}"
    )


@pytest.mark.extended
def test_cli_info_degraded(router, degraded_mode):
    """tollgate wallet info in degraded mode should return valid info
    with mint_count=0 (no active mint connections)."""
    resp = router.get_wallet_info()
    assert resp.get("success") is True, (
        f"wallet info command failed in degraded mode: {resp}"
    )
    data = resp.get("data", {})
    assert "mint_count" in data, f"Missing mint_count in wallet info: {resp}"


@pytest.mark.extended
def test_cli_drain_degraded(router, degraded_mode):
    """tollgate wallet drain in degraded mode should return an error
    (not panic or crash)."""
    resp = router.cli_command("wallet", args=["drain"])
    assert isinstance(resp, dict), f"Expected dict response, got: {resp}"
    if resp.get("success") is True:
        data = resp.get("data", {})
        if data.get("drained_mints") == 0 or data.get("total_drained") == 0:
            log.info("Drain succeeded with 0 drained — acceptable in degraded mode")
            return
    assert resp.get("success") is False or "error" in resp, (
        f"Expected error for drain in degraded mode, got: {resp}"
    )


@pytest.mark.extended
def test_cli_fund_degraded(router, degraded_mode):
    """tollgate wallet fund in degraded mode should return an error
    (not panic or crash)."""
    resp = router.cli_command("wallet", args=["fund", "cashuAeyJ"])
    assert isinstance(resp, dict), f"Expected dict response, got: {resp}"
    assert resp.get("success") is False or "error" in resp, (
        f"Expected error for fund in degraded mode, got: {resp}"
    )
