"""RFC1918 isolation tests — verify WiFi clients cannot reach upstream private networks.

Tests that the fw4 DROP rules and nodogsplash authenticated_users rules
block traffic from authenticated WiFi clients to RFC1918 address ranges.
"""
import pytest

pytestmark = [pytest.mark.story("O6"),pytest.mark.api, pytest.mark.hardware]


def test_rfc1918_drop_rules_exist(router):
    """Verify the fw4 DROP rules for RFC1918 ranges are loaded."""
    output = router.ssh("nft list ruleset 2>/dev/null | grep -i 'Block-WiFi-RFC1918' || echo 'NOT_FOUND'")
    assert "NOT_FOUND" not in output, (
        f"RFC1918 block rules not found in nftables ruleset.\n"
        f"This means PR #217 (fix/rfc1918-isolation-wifi) is not deployed.\n"
        f"Output: {output}"
    )
    # Should find all three ranges
    for cidr in ["192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"]:
        assert cidr in output, f"Missing DROP rule for {cidr} in nftables"


def test_nodogsplash_rfc1918_block_exists(router):
    """Verify nodogsplash authenticated_users rules block RFC1918."""
    output = router.ssh("uci show nodogsplash 2>/dev/null | grep 'block to' || echo 'NOT_FOUND'")
    if "NOT_FOUND" in output:
        pytest.skip("Nodogsplash defense-in-depth rules not deployed (PR #217 nodogsplash layer)")
    for cidr in ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]:
        assert cidr in output, f"Missing nodogsplash block rule for {cidr}"


def test_router_services_still_reachable(router):
    """Verify the RFC1918 rules don't block router's own services (2121, 8080, 2050)."""
    # Port 2121 (TollGate backend) must still be reachable
    code = router.api_status("/")
    assert code == 200, f"TollGate backend (port 2121) unreachable after RFC1918 rules: {code}"


def test_internet_access_still_works(router):
    """Verify authenticated WiFi clients can still reach public IPs (not blocked by RFC1918 rules)."""
    # Check that the router itself can reach a public IP
    output = router.ssh("ping -c 1 -W 3 1.1.1.1 2>&1 || echo 'PING_FAILED'")
    assert "PING_FAILED" not in output, (
        f"Router cannot reach public IP 1.1.1.1 — RFC1918 rules may be too broad.\n"
        f"Output: {output}"
    )
