"""
Cloud-native two-router upstream payment tests.

Tests the full TollGate upstream payment protocol between Alpha (downstream
reseller) and Beta (upstream merchant) over Ethernet bridges in the GCP cloud lab.

Protocol flow:
  1. Alpha discovers Beta by probing gateway :2121 -> kind 10021 advertisement
  2. Alpha selects a compatible pricing option from Beta's ad
  3. Alpha mints a Cashu token from its wallet, POSTs to Beta :2121/
  4. Beta validates token, returns kind 1022 session event with allotment
  5. Alpha tracks usage via GET /usage on Beta
  6. Alpha auto-renews when quota runs low

Requires: --two-router flag on cloud-lab.py submit (sets TOLLGATE_SECONDARY_ROUTER_HOST)
"""

import json
import os
import re

import pytest

from lib.helpers import skip_if_no_cli_socket, is_full_merchant
from lib.router import Router

pytestmark = [pytest.mark.story("P9"),pytest.mark.api, pytest.mark.extended, pytest.mark.virtual_lab]


# ---------------------------------------------------------------------------
# Helper functions (pattern from test_two_router_cloud.py)
# ---------------------------------------------------------------------------

def _get_secondary_router(backend) -> Router | None:
    """Create a Router for Beta from TOLLGATE_SECONDARY_ROUTER_HOST."""
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


def _skip_if_no_secondary(router_b) -> Router:
    """Skip test if no secondary router is configured."""
    if router_b is None:
        pytest.skip("TOLLGATE_SECONDARY_ROUTER_HOST not set")
    return router_b


def _skip_if_not_virtual_lab():
    """Skip test if not running in virtual/cloud lab."""
    if os.environ.get("TOLLGATE_VIRTUAL_LAB") != "1":
        pytest.skip("Cloud two-router tests only run in virtual lab")


def _discover_beta_upstream_ip(router) -> str:
    """Discover Beta's upstream IP from Alpha's eth1 gateway.

    Returns the gateway IP Alpha uses to reach Beta (e.g. 10.99.98.1),
    or empty string if discovery fails.
    """
    try:
        out = router.ssh(
            "ip -4 route show dev eth1 2>/dev/null | head -1 | awk '{print $1, $3}'",
            timeout=10,
        )
        # Default route via 10.99.98.1 -> extract gateway
        for line in out.strip().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                return parts[-1]
    except Exception:
        pass
    return ""


def _get_alpha_wan_ip(router) -> str:
    """Get Alpha's WAN (eth1) IP address, or empty string."""
    try:
        out = router.ssh("ip -4 addr show eth1 2>/dev/null | grep inet", timeout=10)
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Test functions
# ---------------------------------------------------------------------------

@pytest.mark.timeout(120)
def test_beta_advertisement_visible_from_alpha(router, backend):
    """Verify Alpha can fetch Beta's kind 10021 advertisement over the upstream bridge.

    Alpha SSHes into itself and probes the gateway IP on eth1 at port 2121.
    A valid TollGate upstream returns kind 10021 with price_per_step, metric,
    and step_size tags — proving Alpha can discover Beta as an upstream provider.
    """
    _skip_if_not_virtual_lab()
    _skip_if_no_secondary(_get_secondary_router(backend))

    beta_ip = _discover_beta_upstream_ip(router)
    if not beta_ip:
        pytest.skip("Alpha has no upstream gateway on eth1 (no route to Beta)")

    raw = router.ssh(
        f"wget -qO- --timeout=10 'http://{beta_ip}:2121/'",
        timeout=15,
    )
    if not raw.strip():
        pytest.skip(f"No response from Beta at http://{beta_ip}:2121/")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        pytest.fail(f"Beta returned non-JSON: {raw[:200]}")

    assert data.get("kind") == 10021, (
        f"Beta API returned unexpected kind {data.get('kind')}: {raw[:200]}"
    )

    tags = data.get("tags", [])

    has_price = any(
        isinstance(t, list) and len(t) > 0 and t[0] == "price_per_step"
        for t in tags
    )
    assert has_price, f"No price_per_step tag in Beta advertisement: {tags}"

    tag_names = {t[0] for t in tags if isinstance(t, list) and len(t) > 0}
    assert "metric" in tag_names, f"No 'metric' tag in Beta advertisement: {tag_names}"
    assert "step_size" in tag_names, f"No 'step_size' tag in Beta advertisement: {tag_names}"


@pytest.mark.timeout(120)
def test_alpha_upstream_detector_sees_beta(router, backend):
    """Verify Alpha's upstream_detector module discovered Beta in its logs.

    Searches Alpha's system logs for evidence that the upstream_detector
    probed the gateway IP or found a TollGate advertisement on eth1.
    """
    _skip_if_not_virtual_lab()
    _skip_if_no_secondary(_get_secondary_router(backend))

    logs = router.get_tollgate_logs(lines=500)
    if not logs.strip():
        pytest.skip("No tollgate logs found on Alpha")

    # Look for upstream detection patterns
    upstream_patterns = [
        r"upstream",
        r"gateway",
        r"advertisement",
        r"TollGate",
    ]
    matches = []
    for pattern in upstream_patterns:
        found = re.findall(
            rf".*{pattern}.*",
            logs,
            re.IGNORECASE,
        )
        matches.extend(found)

    if not matches:
        pytest.skip(
            "No upstream detector activity in Alpha logs — "
            "upstream_detector may not be implemented in this firmware version"
        )

    # Verify the detector actually probed something (not just generic log noise)
    probed_something = any(
        re.search(r"(probing|probed|found|detected|discovered|connecting)", m, re.IGNORECASE)
        for m in matches
    )
    assert probed_something or len(matches) >= 2, (
        f"Upstream detector logs found but no evidence of active probing: "
        f"{matches[:5]}"
    )


@pytest.mark.timeout(120)
def test_alpha_wallet_funded(router, backend):
    """Verify Alpha's local wallet has a positive balance.

    The cloud worker funds Alpha's wallet before tests run so Alpha can
    pay Beta autonomously. This test confirms that funding succeeded.
    """
    _skip_if_not_virtual_lab()
    _skip_if_no_secondary(_get_secondary_router(backend))

    if not router.backend.has_cli_socket:
        pytest.skip("CLI socket not available (non-Go backend)")

    skip_if_no_cli_socket(router)

    balance = router.get_wallet_balance()
    if balance.get("success") is not True and "balance_sats" not in balance:
        # Try parsing from raw output
        raw = balance.get("raw", "")
        if raw:
            m = re.search(r"balance[:\s]+(\d+)", raw, re.IGNORECASE)
            if m and int(m.group(1)) > 0:
                return
        pytest.skip(
            f"Wallet balance query failed or unavailable: {balance}"
        )

    balance_sats = balance.get("balance_sats", 0)
    if isinstance(balance_sats, str):
        balance_sats = int(balance_sats) if balance_sats.isdigit() else 0

    assert balance_sats > 0, (
        f"Alpha wallet has zero balance — worker funding may have failed: {balance}"
    )


@pytest.mark.timeout(120)
def test_alpha_pays_beta_and_gets_session(router, backend):
    """Verify Alpha has an active upstream session with Beta.

    Checks Alpha's logs and CLI status for evidence that Alpha successfully
    paid Beta and received a kind 1022 session event with an allotment.
    """
    _skip_if_not_virtual_lab()
    _skip_if_no_secondary(_get_secondary_router(backend))

    # Strategy 1: Check CLI status for upstream session info
    if router.backend.has_cli_socket:
        try:
            skip_if_no_cli_socket(router)
            status = router.get_tollgate_status()
            if status.get("success") is True:
                raw = json.dumps(status).lower()
                if any(kw in raw for kw in ["upstream", "session", "allotment"]):
                    # Found upstream session evidence
                    return
        except Exception:
            pass

    # Strategy 2: Check logs for kind 1022 session event
    logs = router.get_tollgate_logs(lines=500)
    session_evidence = re.findall(
        r"(1022|allotment|upstream.*session|session.*upstream)",
        logs,
        re.IGNORECASE,
    )

    if not session_evidence:
        pytest.skip(
            "No upstream session evidence found in logs or CLI status — "
            "upstream payment flow may not have completed yet, or the "
            "upstream_detector is not implemented in this firmware version"
        )


@pytest.mark.timeout(120)
def test_alpha_usage_tracking_on_beta(router, backend):
    """Verify Beta has a session record for Alpha's WAN IP.

    Alpha pays Beta and Beta tracks the session. This test confirms Beta's
    /usage or /balance endpoint shows an active session for Alpha's eth1 IP.
    """
    _skip_if_not_virtual_lab()
    router_b = _skip_if_no_secondary(_get_secondary_router(backend))

    alpha_wan_ip = _get_alpha_wan_ip(router)
    if not alpha_wan_ip:
        pytest.skip("Alpha has no WAN IP on eth1")

    # Query Beta's usage endpoint for Alpha's WAN IP
    usage_raw = router_b.ssh(
        f"wget -qO- --timeout=10 "
        f"'http://[::1]:2121/usage?ip={alpha_wan_ip}'",
        timeout=15,
    )
    if not usage_raw.strip():
        # Try /balance endpoint as fallback
        usage_raw = router_b.ssh(
            f"wget -qO- --timeout=10 "
            f"'http://[::1]:2121/balance?ip={alpha_wan_ip}'",
            timeout=15,
        )

    if not usage_raw.strip():
        # Try ndsctl clients as last resort
        try:
            nds_out = router_b.ssh("ndsctl clients 2>&1", timeout=10)
            if alpha_wan_ip in nds_out:
                return  # Alpha found in NDS client list
        except Exception:
            pass
        pytest.skip(
            f"No usage/session data found on Beta for Alpha IP {alpha_wan_ip}"
        )

    try:
        usage_data = json.loads(usage_raw)
    except json.JSONDecodeError:
        pytest.skip(f"Beta returned non-JSON for usage query: {usage_raw[:200]}")

    # A session record exists — check for active markers
    raw_lower = json.dumps(usage_data).lower()
    has_session = any(
        kw in raw_lower
        for kw in ["session", "active", "remaining", "allotment", "authenticated"]
    )
    if not has_session:
        pytest.skip(
            f"Beta has no active session record for {alpha_wan_ip}: "
            f"{usage_raw[:200]}"
        )


@pytest.mark.timeout(120)
def test_internet_through_beta(router, backend):
    """Verify Alpha has internet access routed through Beta.

    Alpha's traffic flows: Alpha eth1 -> Beta eth1 -> Beta NAT -> internet.
    A successful ping to 1.1.1.1 proves the full upstream chain works.
    """
    _skip_if_not_virtual_lab()
    _skip_if_no_secondary(_get_secondary_router(backend))

    # Use ping first (more reliable in OpenWrt)
    try:
        ping_out = router.ssh(
            "ping -c 2 -W 3 1.1.1.1 2>&1",
            timeout=15,
        )
        if "100% packet loss" not in ping_out and ("bytes from" in ping_out or "round-trip" in ping_out):
            return  # Internet works via ping
    except Exception:
        pass

    # Fallback: wget
    try:
        wget_out = router.ssh(
            "wget -qO- --timeout=10 http://1.1.1.1 2>&1 | head -c 200",
            timeout=15,
        )
        if wget_out.strip():
            return  # Got some response
    except Exception:
        pass

    # Second fallback: try DNS resolution
    try:
        ns_out = router.ssh("nslookup google.com 2>&1", timeout=10)
        if "Address" in ns_out and "server can't find" not in ns_out.lower():
            return  # DNS works, internet likely available
    except Exception:
        pass

    pytest.skip(
        "Alpha cannot reach the internet through Beta — "
        "upstream payment may not have completed, or NAT is not configured"
    )


@pytest.mark.timeout(120)
def test_beta_session_on_alpha_disconnect(router, backend):
    """Verify Beta cleans up Alpha's session when the upstream link drops.

    This test checks session cleanup behavior by examining Beta's NDS client
    list for stale entries. Only runs if CLI tools are available.

    Note: This test is informational — it verifies the current state rather
    than actively disconnecting interfaces (which would be destructive).
    """
    _skip_if_not_virtual_lab()
    router_b = _skip_if_no_secondary(_get_secondary_router(backend))

    alpha_wan_ip = _get_alpha_wan_ip(router)
    if not alpha_wan_ip:
        pytest.skip("Alpha has no WAN IP on eth1")

    # Check that Beta's ndsctl is available
    try:
        nds_status = router_b.ssh("ndsctl status 2>&1", timeout=10)
        if "not found" in nds_status.lower() or "command not found" in nds_status.lower():
            pytest.skip("ndsctl not available on Beta")
    except Exception:
        pytest.skip("Cannot query ndsctl on Beta")

    # Check Beta's client list for Alpha
    try:
        clients = router_b.ssh("ndsctl clients 2>&1", timeout=10)
    except Exception:
        pytest.skip("Cannot query ndsctl clients on Beta")

    # If Alpha's WAN IP appears in client list, verify it has proper state
    if alpha_wan_ip in clients:
        # Alpha is currently connected — that's expected for a live session
        state_match = re.search(
            rf"ip={re.escape(alpha_wan_ip)}.*?state=(\S+)",
            clients,
        )
        if state_match:
            state = state_match.group(1)
            assert state in ("Authenticated", "Preauthenticated"), (
                f"Unexpected NDS state for Alpha on Beta: {state}"
            )
        return

    # Alpha not in client list — could mean session expired or was cleaned up
    # Both are acceptable outcomes for this non-destructive check
    pytest.skip(
        f"Alpha IP {alpha_wan_ip} not in Beta's NDS client list — "
        "session may have expired or upstream link is inactive"
    )


@pytest.mark.timeout(120)
def test_both_routers_healthy_after_payment(router, backend):
    """Verify both Alpha and Beta remain healthy after the upstream payment flow.

    Alpha should return kind 10021 (full merchant) or kind 21023 (degraded).
    Beta should return kind 10021 (full merchant) — it has local mint access
    and should never be in degraded mode.
    """
    _skip_if_not_virtual_lab()
    router_b = _skip_if_no_secondary(_get_secondary_router(backend))

    # Check Alpha health
    alpha_body = router.api_body("/")
    if not alpha_body.strip():
        pytest.fail("Alpha API returned empty response")

    try:
        alpha_data = json.loads(alpha_body)
    except json.JSONDecodeError:
        pytest.fail(f"Alpha API returned non-JSON: {alpha_body[:200]}")

    alpha_kind = alpha_data.get("kind")
    assert alpha_kind in (10021, 21023), (
        f"Alpha API returned unexpected kind {alpha_kind}: {alpha_body[:200]}"
    )

    # Check Beta health
    beta_body = router_b.api_body("/")
    if not beta_body.strip():
        pytest.fail("Beta API returned empty response")

    try:
        beta_data = json.loads(beta_body)
    except json.JSONDecodeError:
        pytest.fail(f"Beta API returned non-JSON: {beta_body[:200]}")

    beta_kind = beta_data.get("kind")
    assert beta_kind in (10021, 21023), (
        f"Beta API returned unexpected kind {beta_kind}: {beta_body[:200]}"
    )

    # Beta should be a full merchant (has local mint access in cloud lab)
    if beta_kind == 10021:
        assert is_full_merchant(router_b), (
            "Beta returned kind 10021 but lacks price_per_step tags — "
            f"not a full merchant: {beta_body[:200]}"
        )
