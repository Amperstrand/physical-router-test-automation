"""NDS firewall gating verification test.

Tests that NDS actually blocks/unblocks traffic based on payment state.
Runs against the local OpenWrt VM (10.99.99.1) with real NDS.

Prerequisites:
  - OpenWrt VM running at 10.99.99.1 with NDS + tollgate-wrt
  - Client VM at 10.99.99.100 (root SSH access)
  - CDK mint at 10.99.99.2:8383

Run:
  python3 tests/api/test_nds_gating.py
  # or via pytest (skip conftest with --override-ini="addopts="):
  TOLLGATE_SSH_HOST=10.99.99.1 python3 -m pytest tests/api/test_nds_gating.py -v --override-ini="addopts="
"""

import json
import os
import subprocess
import sys
import time

OPENWRT = "10.99.99.1"
CLIENT = "10.99.99.100"
MINT = os.environ.get("TOLLGATE_TEST_MINT_URL", "http://10.99.99.2:8383")
BACKEND = f"http://{OPENWRT}:2121"


def ssh(host, cmd, timeout=30):
    r = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "-o", "ConnectTimeout=5", f"root@{host}", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout.strip()


def check_internet():
    """Returns (is_redirected, detail).

    is_redirected=True means NDS is intercepting (client NOT authed).
    is_redirected=False means client has direct internet access.

    IP-literal probe, no redirect-following: 1.1.1.1's 301 targets a
    hostname, and DNS-through-NDS is governed by the users_to_router
    allow-list (only tcp/53) — a different concern from the mark-based
    forwarding under test. redirect_url carries the portal marker when
    NDS intercepts, or 1.1.1.1's own Location when direct.
    """
    out = ssh(
        CLIENT,
        "curl -s --max-time 5 -o /dev/null -w '%{redirect_url}' http://1.1.1.1 2>/dev/null || echo BLOCKED",
    )
    if "BLOCKED" in out:
        return True, "blocked entirely"
    if "2050" in out or "splash" in out or OPENWRT in out:
        return True, f"redirected to portal: {out[:80]}"
    return False, f"direct access: {out[:80]}"


def get_nds_client():
    out = ssh(OPENWRT, "ndsctl json")
    try:
        d = json.loads(out)
        for mac, info in d.get("clients", {}).items():
            return mac, info.get("state", "unknown")
    except Exception:
        pass
    return None, "none"


def fix_auth_marks():
    """Repair NDS 5.0.2 auth marks so authed clients can open NEW connections.

    ndsctl-auth inserts 0x30000 marks that the ndsNET accept rule
    (0x20000/0x30000) can never match — a paid + Authenticated client stays
    blocked. See Router.fix_nodogsplash_auth_marks (lib/router.py).
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from lib.router import Router
    Router(OPENWRT, CLIENT, "de:54:4e:91:49:da", "").fix_nodogsplash_auth_marks()


def deauth():
    mac, _ = get_nds_client()
    if mac:
        ssh(OPENWRT, f"ndsctl deauth {mac}")
        # NDS 5.0.2 removes the per-client ndsOUT mark rule asynchronously,
        # on its own loop tick — until then the client keeps the auth bit.
        for _ in range(10):
            rules = ssh(
                OPENWRT,
                f"iptables -t mangle -S ndsOUT 2>/dev/null | grep '{mac}' | wc -l",
            )
            if rules.strip() == "0":
                break
            time.sleep(1)
    return mac


def pay():
    subprocess.run(
        ["cashu", "-h", MINT, "-w", "cdk-wallet", "-t", "invoice", "4"],
        capture_output=True, timeout=30,
    )
    r = subprocess.run(
        ["cashu", "-h", MINT, "-w", "cdk-wallet", "-t", "send", "4"],
        capture_output=True, text=True, timeout=15,
    )
    token = next((l for l in r.stdout.splitlines() if l.startswith("cashu")), None)
    if not token:
        return False, "no token"
    result = ssh(
        CLIENT,
        f"curl -s --max-time 30 -X POST {BACKEND}/ -H 'Content-Type: text/plain' -d '{token}'",
    )
    try:
        d = json.loads(result)
        return d.get("kind") == 1022, f"kind={d.get('kind')}"
    except Exception:
        return False, f"response: {result[:100]}"


# ─── Tests ─────────────────────────────────────────────────────────

def test_nds_redirects_unauthenticated():
    """NDS must intercept HTTP traffic when client is not authenticated."""
    deauth()
    redirected, detail = check_internet()
    assert redirected, f"Expected NDS redirect, but: {detail}"
    print(f"  ✅ NDS intercepts: {detail}")


def test_nds_allows_after_payment():
    """After payment, NDS must allow direct internet access."""
    deauth()
    redirected_before, detail_before = check_internet()
    assert redirected_before, "Pre-payment: should be redirected"

    ok, pay_detail = pay()
    assert ok, f"Payment failed: {pay_detail}"
    print(f"  Payment: {pay_detail}")

    fix_auth_marks()

    time.sleep(3)
    mac, state = get_nds_client()
    print(f"  NDS state: {mac} → {state}")

    redirected_after, detail_after = check_internet()
    if redirected_after:
        dbg = ssh(
            OPENWRT,
            "iptables -t mangle -S ndsOUT; iptables -S ndsNET | head -3",
        )
        print(f"  fw state at failure:\n{dbg}")
    assert not redirected_after, f"Post-payment: should have direct access, but: {detail_after}"
    print(f"  ✅ Direct access: {detail_after}")


def test_nds_deauth_blocks_again():
    """After deauth, NDS must re-intercept traffic."""
    deauth()
    redirected, detail = check_internet()
    assert redirected, f"After deauth: should be redirected, but: {detail}"
    print(f"  ✅ Blocked again: {detail}")


if __name__ == "__main__":
    print("=== NDS Firewall Gating Test ===\n")

    print("Test 1: NDS redirects unauthenticated traffic")
    try:
        test_nds_redirects_unauthenticated()
        print("  PASS\n")
    except AssertionError as e:
        print(f"  FAIL: {e}\n")

    print("Test 2: NDS allows after payment")
    try:
        test_nds_allows_after_payment()
        print("  PASS\n")
    except AssertionError as e:
        print(f"  FAIL: {e}\n")

    print("Test 3: NDS re-blocks after deauth")
    try:
        test_nds_deauth_blocks_again()
        print("  PASS\n")
    except Exception as e:
        print(f"  FAIL: {e}\n")
