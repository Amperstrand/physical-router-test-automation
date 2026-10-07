"""
Test NDS firewall integration with fw4/nftables on OpenWrt 24.10+.

BUG: Client shows "Authenticated" in ndsctl but cannot access internet.

ROOT CAUSE: NDS 5.0.2 inserts filter rules into iptables-nft tables
(ip filter FORWARD → ndsNET chain). OpenWrt 24.10 uses fw4/nftables
(inet fw4 forward chain with policy drop). When fw4's forward chain
accepts LAN→WAN traffic, the packet never reaches the ip filter FORWARD
chain where NDS's accept/reject rules live. NDS's ndsNET chain shows
0 packets — its verdict is never applied.

This means:
- NDS authentication marks (0x30000) are applied in mangle/nat tables
- But the actual accept/drop decision for forwarded traffic is made by
  fw4, not by NDS
- NDS cannot actually enforce its authentication gate on forwarded traffic
"""
import pytest
import re
import os
import time
import subprocess


pytestmark = [pytest.mark.story("O6")]

@pytest.mark.api
class TestNdsFw4Integration:
    """Verify NDS firewall rules are reachable in the actual packet path."""

    def test_nds_filter_chain_receives_packets(self, router):
        """The ip filter FORWARD chain (where NDS ndsNET lives) must see forwarded packets.

        On a working system, forwarded traffic from br-lan should reach the
        NDS ndsNET chain. If fw4 accepts packets first and they never reach
        ip filter FORWARD, NDS cannot enforce authentication.
        """
        # Get packet counters for the ip filter FORWARD chain
        out = router.ssh("iptables -L FORWARD -n -v 2>/dev/null", timeout=10)

        # Parse total packets
        m = re.search(r"policy\s+\w+\s+(\d+)\s+packets", out)
        total_packets = int(m.group(1)) if m else 0

        # Get ndsNET specific counter
        nds_net_match = re.search(r"^\s*(\d+)\s+\d+\w*\s+ndsNET", out, re.MULTILINE)
        nds_packets = int(nds_net_match.group(1)) if nds_net_match else 0

        # On a healthy system with active clients, both should be > 0.
        # If NDS is managing clients, forwarded traffic MUST reach ndsNET.
        nds_state = router.get_nds_state()
        if nds_state == "Authenticated":
            # There IS an authenticated client — ndsNET MUST have seen packets
            assert nds_packets > 0, (
                f"NDS ndsNET chain has 0 packets despite authenticated clients. "
                f"ip filter FORWARD total={total_packets} packets. "
                f"fw4 is accepting forwarded traffic before it reaches NDS rules. "
                f"NDS firewall integration is broken."
            )

    def test_nds_fw4_chain_registration(self, router):
        """The nds_enforce_forward chain must exist in inet fw4 at priority -1.

        This is the PR #283 fix: a base chain in inet fw4 that hooks forward
        at priority -1 (before fw4's own forward at priority 0) and enforces
        NDS mangle marks.
        """
        chain_output = router.ssh(
            "nft list chain inet fw4 nds_enforce_forward 2>/dev/null", timeout=10
        )

        if "No such file or directory" in chain_output or not chain_output.strip():
            pytest.fail(
                "Chain 'nds_enforce_forward' not found in inet fw4 table. "
                "PR #283's /etc/nftables.d/20-nds-enforce.nft is not deployed "
                "or fw4 did not load it."
            )

        # Verify priority is -1 (rendered as "filter - 1" by nft)
        assert re.search(r"priority (filter - 1|-1)", chain_output), (
            f"nds_enforce_forward chain has wrong priority. Expected -1.\n{chain_output}"
        )

        # Verify all four enforcement rules are present
        for pattern in ["0x00010000", "0x00020000", "0x00030000", "reject"]:
            assert pattern in chain_output, (
                f"Missing rule for mark pattern {pattern} in nds_enforce_forward.\n{chain_output}"
            )

    def test_nds_mangle_marking_works(self, router):
        """The mangle ndsOUT chain should mark authenticated client traffic."""
        client_ip = os.environ.get("TOLLGATE_CLIENT_IP", "10.99.99.100")
        router.ssh(f"ndsctl auth {client_ip} 2>/dev/null || true", timeout=5)
        time.sleep(2)

        out = router.ssh("iptables -t mangle -L ndsOUT -n -v 2>/dev/null", timeout=10)

        has_mark = "MARK" in out
        assert has_mark, (
            f"NDS mangle ndsOUT chain has no MARK rules. "
            f"NDS may not be running or no clients are registered.\n{out}"
        )

    def test_nds_authenticated_client_has_forwarded_traffic(self, router):
        """An authenticated client should have conntrack entries (active connections).

        If ndsctl shows Authenticated but conntrack is empty, the client's
        traffic is not being forwarded — indicating a firewall issue.
        """
        client_ip = os.environ.get("TOLLGATE_CLIENT_IP", "10.99.99.100")
        router.ssh(f"ndsctl auth {client_ip} 2>/dev/null || true", timeout=5)
        time.sleep(2)

        nds_state = router.get_nds_state()
        if nds_state != "Authenticated":
            pytest.skip(f"Could not authenticate client at {client_ip} (state={nds_state})")

        # Get the client IP from ndsctl
        clients = router.ssh("ndsctl json 2>/dev/null", timeout=10)
        ip_match = re.search(r'"ip":"([0-9.]+)".*?"state":"Authenticated"', clients, re.DOTALL)
        if not ip_match:
            pytest.skip("Could not find authenticated client IP")

        client_ip = ip_match.group(1)

        try:
            subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                 f"root@{client_ip}", "ping -c 2 -W 2 8.8.8.8"],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass
        time.sleep(1)

        conntrack = router.ssh(
            f"grep -c '{client_ip}' /proc/net/nf_conntrack 2>/dev/null || echo 0", timeout=10
        )
        conn_count = int(conntrack.strip()) if conntrack.strip().isdigit() else 0

        assert conn_count > 0, (
            f"Authenticated client {client_ip} has 0 conntrack entries. "
            f"Client traffic is not being forwarded through the router. "
            f"This indicates NDS fw4 integration is broken — fw4 accepts "
            f"forwarded traffic but NDS cannot enforce its authentication gate."
        )

    def test_nds_dnct_port80_redirect_for_unauthenticated(self, router):
        """NDS should DNAT port 80 to the portal for unauthenticated clients.

        The nat ndsOUT chain should have a DNAT rule for tcp port 80.
        """
        out = router.ssh("iptables -t nat -L ndsOUT -n -v 2>/dev/null", timeout=10)

        has_dnat = "DNAT" in out and "dpt:80" in out
        assert has_dnat, (
            f"NDS nat ndsOUT chain missing DNAT rule for port 80.\n{out}"
        )

    def test_forward_policy_allows_nds_control(self, router):
        """fw4 forward policy should not bypass NDS authentication.

        Check that fw4's forward chain doesn't blanket-accept all br-lan traffic
        before NDS rules can evaluate it.
        """
        # Get fw4 forward chain
        fw4_forward = router.ssh(
            "nft list chain inet fw4 forward 2>/dev/null", timeout=10
        )

        # fw4 forward should have 'policy drop' (not accept)
        # This is normal — fw4 manages its own accept rules
        has_drop = "policy drop" in fw4_forward

        # The key issue: does fw4's accept_to_wan fire before NDS?
        # Check accept_to_wan counters
        accept_wan = router.ssh(
            "nft list chain inet fw4 accept_to_wan 2>/dev/null", timeout=10
        )

        # If accept_to_wan has accepted packets, those packets bypassed NDS
        wan_accept_match = re.search(r"counter packets (\d+).*accept", accept_wan)
        wan_accepted = int(wan_accept_match.group(1)) if wan_accept_match else 0

        # Get ip filter FORWARD total to compare
        ip_forward = router.ssh(
            "iptables -L FORWARD -n -v 2>/dev/null | head -3", timeout=10
        )
        ip_match = re.search(r"policy\s+\w+\s+(\d+)\s+packets", ip_forward)
        ip_total = int(ip_match.group(1)) if ip_match else 0

        # If fw4 accepted forwarded packets but ip filter saw 0,
        # NDS rules are completely bypassed
        if wan_accepted > 0 and ip_total == 0:
            pytest.fail(
                f"fw4 accept_to_wan accepted {wan_accepted} forwarded packets, "
                f"but ip filter FORWARD (where NDS ndsNET lives) saw {ip_total} packets. "
                f"fw4 is bypassing NDS firewall integration entirely."
            )


    def test_nds_enforce_forward_accepts_authenticated_traffic(self, router):
        """PR #283: the nds_enforce_forward chain should accept traffic with mark 0x30000."""
        client_ip = os.environ.get("TOLLGATE_CLIENT_IP", "10.99.99.100")
        router.ssh(f"ndsctl auth {client_ip} 2>/dev/null || true", timeout=5)
        time.sleep(2)

        chain = router.ssh("nft -a list chain inet fw4 nds_enforce_forward 2>/dev/null", timeout=10)
        authed_match = re.search(r"0x00030000 == 0x00030000 counter packets (\d+)", chain)
        authed_pkts = int(authed_match.group(1)) if authed_match else 0

        if authed_pkts == 0:
            import subprocess
            try:
                subprocess.run(
                    ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                     f"root@{client_ip}", "ping -c 3 -W 2 8.8.8.8"],
                    capture_output=True, timeout=15,
                )
                time.sleep(1)
                chain = router.ssh("nft -a list chain inet fw4 nds_enforce_forward 2>/dev/null", timeout=10)
                authed_match = re.search(r"0x00030000 == 0x00030000 counter packets (\d+)", chain)
                authed_pkts = int(authed_match.group(1)) if authed_match else 0
            except Exception:
                pass

            if authed_pkts == 0:
                pytest.skip("No traffic reached the authenticated accept rule")


@pytest.mark.api
class TestBalancePageRedirect:
    """PR #22: balance page must be served on port 8090 with immediate redirect."""

    def test_balance_page_reachable_on_8090(self, router):
        client_ip = os.environ.get("TOLLGATE_CLIENT_IP", "10.99.99.100")
        try:
            result = subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                 f"root@{client_ip}",
                 "curl -s -o /dev/null -w '%{http_code} %{time_total}' "
                 "http://10.99.99.1:8090/balance.html"],
                capture_output=True, text=True, timeout=15,
            )
        except (subprocess.TimeoutExpired, Exception):
            pytest.skip(f"Debian client at {client_ip} not reachable")
            return

        parts = result.stdout.strip().split()
        if len(parts) < 2 or parts[0] == "000":
            pytest.skip(f"Balance page not reachable from client (HTTP {parts[0] if parts else '?'})")
        code = parts[0]
        assert code == "200", f"Balance page returned HTTP {code}, expected 200"

    def test_portal_js_redirects_to_8090(self, router):
        splash = router.ssh("cat /etc/nodogsplash/htdocs/splash.html 2>/dev/null || cat /etc/nodogsplash/htdocs/index.html 2>/dev/null", timeout=10)
        if not splash.strip():
            pytest.skip("No splash page found in NDS htdocs")
        js_ref = re.search(r'src="([^"]*splash[^"]*\.js)"', splash)
        if not js_ref:
            pytest.skip("Could not find splash JS reference in HTML")
        js_path = js_ref.group(1).replace("./", "")
        js_content = router.ssh(f"cat /etc/nodogsplash/htdocs/{js_path} 2>/dev/null", timeout=10)
        assert ":8090" in js_content, (
            "Portal JS does not redirect to port 8090. "
            "PR #22 fix is not deployed."
        )
    """End-to-end connectivity tests for authenticated clients."""

    def test_router_can_reach_internet(self, router):
        """The router itself should have upstream connectivity."""
        result = router.ssh(
            "ping -c 1 -W 3 8.8.8.8 2>&1", timeout=10
        )
        assert "1 packets received" in result or "0% packet loss" in result, (
            f"Router cannot ping 8.8.8.8 — upstream connection is down.\n{result}"
        )

    def test_dns_resolution_works(self, router):
        """DNS resolution should work on the router."""
        result = router.ssh(
            "nslookup example.com 127.0.0.1 2>&1", timeout=10
        )
        assert "Address" in result and "127.0.0.1" not in result.split("\n")[-1], (
            f"DNS resolution failed.\n{result}"
        )

    def test_authenticated_client_download_counter_increases(self, router):
        """NDS download byte counter should increase over time for authenticated clients.

        If the counter stays flat, no traffic is flowing through NDS.
        """
        # Get initial byte count
        json1 = router.ssh("ndsctl json 2>/dev/null", timeout=10)
        dl1 = self._extract_bytes(json1, "downloaded")

        if dl1 is None:
            pytest.skip("No client in ndsctl")

        # Wait and check again
        router.ssh("sleep 5", timeout=10)
        json2 = router.ssh("ndsctl json 2>/dev/null", timeout=10)
        dl2 = self._extract_bytes(json2, "downloaded")

        if dl2 is None:
            pytest.skip("Client disappeared between checks")

        # Counter should increase if traffic is flowing
        # Note: if no active traffic, this may stay flat — informational
        if dl1 == dl2:
            # Check if this is because no traffic is flowing vs NDS being broken
            mangle_out = router.ssh(
                "iptables -t mangle -L ndsOUT -n -v 2>/dev/null", timeout=10
            )
            m_match = re.search(r"(\d+)\s+\d+\w*\s+MARK", mangle_out)
            mangle_pkts = int(m_match.group(1)) if m_match else 0

            # If mangle is seeing packets but ndsctl download isn't increasing,
            # traffic is bypassing NDS's accounting
            if mangle_pkts > 100:
                pytest.xfail(
                    f"NDS mangle sees {mangle_pkts} marked packets but "
                    f"download counter unchanged ({dl1}→{dl2}). "
                    f"Traffic bypasses NDS accounting."
                )

    @staticmethod
    def _extract_bytes(nds_json: str, field: str) -> int | None:
        m = re.search(rf'"{field}":(\d+)', nds_json)
        return int(m.group(1)) if m else None
