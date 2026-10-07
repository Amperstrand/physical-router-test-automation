#!/usr/bin/env python3
"""E2E test: Cashu pay -> JWT -> WG peer -> tunnel.

Mints testnuts on nodns.shop (where cashu Python works), then runs the
full payment-gated VPN flow locally via curl.

Usage:
    python3 conwrt/test_vpn_e2e.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

TOLLGATE_AUTH = os.environ.get("TOLLGATE_AUTH", "https://nodns.shop")
VPN_SERVER_ID = os.environ.get("VPN_SERVER_ID", "vpn-shc-860")
VPN_PEER_API = os.environ.get("VPN_PEER_API", "http://66.92.204.239:8082")
NSEC_FILE = os.environ.get("NSEC_FILE", str(Path.home() / ".config" / "prta" / "nsec"))

SUITE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUITE_ROOT))


import pytest

pytestmark = [pytest.mark.story("O6")]

def run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def curl_json(url: str, method: str = "GET", data: dict | None = None,
              headers: dict | None = None, timeout: int = 60) -> tuple[int, dict | str]:
    cmd = ["curl", "-sf", "-X", method, "--connect-timeout", "10", "--max-time", str(timeout)]
    if headers:
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
    if data:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(url)
    rc, out, err = run(cmd, timeout=timeout + 5)
    if rc != 0:
        return rc, err or out
    try:
        return 0, json.loads(out)
    except json.JSONDecodeError:
        return 0, out


def mint_testnuts_via_nodns(amount: int = 10) -> str | None:
    """Mint testnuts using the cashu library on nodns.shop via SSH."""
    rc, out, err = run([
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "root@nodns.shop",
        "cat > /tmp/mint_token.py << 'PYEOF'\n"
        "import asyncio\n"
        "async def m():\n"
        "    from cashu.wallet.wallet import Wallet\n"
        f"    w = await Wallet.with_db('https://testnut.cashu.exchange', '/tmp/e2e-mint-{int(time.time())}', 'sat')\n"
        "    await w.load_mint()\n"
        f"    q = await w.request_mint({amount})\n"
        "    import time; time.sleep(3)\n"
        f"    p = await w.mint({amount}, quote_id=q.quote)\n"
        "    t = await w.serialize_proofs(p)\n"
        "    with open('/tmp/cashu-token-out.txt', 'w') as f: f.write(t)\n"
        "asyncio.run(m())\n"
        "PYEOF\n"
        "/root/.local/share/pipx/venvs/cashu/bin/python3 /tmp/mint_token.py > /dev/null 2>&1\n"
        "cat /tmp/cashu-token-out.txt"
    ], timeout=30)
    if rc != 0:
        print(f"  mint via nodns failed: {err[:200]}")
        return None
    for line in out.splitlines():
        if line.startswith("cashu"):
            return line
    return None


def test_vpn_e2e() -> dict:
    results = {"tests": {}, "passed": 0, "failed": 0}

    print("1. Minting testnuts via nodns.shop...")
    token = mint_testnuts_via_nodns(10)
    if not token:
        results["tests"]["mint"] = False
        results["failed"] += 1
        return results
    print(f"   token: {token[:50]}...")
    results["tests"]["mint"] = True
    results["passed"] += 1

    print("2. Generating client WG keypair...")
    wg_bin = None
    for p in ["/usr/local/bin/wg", "/opt/homebrew/bin/wg", "/usr/bin/wg"]:
        if os.path.isfile(p):
            wg_bin = p
            break
    if not wg_bin:
        rc, out, _ = run(["which", "wg"])
        if rc == 0:
            wg_bin = out.strip()
    if not wg_bin:
        print("   wg not found locally, generating via SSH on nodns.shop...")
        rc, out, err = run([
            "ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "root@nodns.shop",
            "wg genkey | tee /tmp/wg-priv | wg pubkey > /tmp/wg-pub; cat /tmp/wg-priv /tmp/wg-pub"
        ], timeout=15)
        if rc != 0:
            print(f"   wg genkey via SSH failed: {err}")
            results["tests"]["wg_keygen"] = False
            results["failed"] += 1
            return results
        lines = out.strip().splitlines()
        priv = lines[0].strip()
        pub = lines[1].strip()
    else:
        rc, priv, _ = run([wg_bin, "genkey"])
        if rc != 0:
            print("   wg genkey failed")
            results["tests"]["wg_keygen"] = False
            results["failed"] += 1
            return results
        rc, pub, _ = run(["bash", "-c", f"echo '{priv}' | {wg_bin} pubkey"])
    print(f"   pubkey: {pub}")
    results["tests"]["wg_keygen"] = True
    results["passed"] += 1

    print("3. Paying at tollgate-auth (via nodns.shop localhost)...")
    rc, pay_out, pay_err = run([
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "root@nodns.shop",
        f"curl -sf -X POST http://localhost:8091/v1/wg/connect "
        f"-H 'Content-Type: application/json' "
        f"-d '{json.dumps({"token": token, "pubkey": pub, "server_id": VPN_SERVER_ID})}' "
        f"--max-time 60"
    ], timeout=70)
    if rc != 0:
        print(f"   payment SSH failed: {pay_err[:200]}")
        results["tests"]["payment"] = False
        results["failed"] += 1
        return results
    try:
        resp = json.loads(pay_out)
    except json.JSONDecodeError:
        print(f"   payment response not JSON: {pay_out[:200]}")
        results["tests"]["payment"] = False
        results["failed"] += 1
        return results
    if "jwt" not in resp:
        print(f"   no JWT in response: {resp}")
        results["tests"]["payment"] = False
        results["failed"] += 1
        return results
    jwt_token = resp["jwt"]
    endpoint = resp.get("endpoint", "")
    server_pubkey = resp.get("server_pubkey", "")
    client_ip = resp.get("client_ip", "")
    expires_at = resp.get("expires_at", 0)
    print(f"   JWT: {jwt_token[:40]}...")
    print(f"   endpoint: {endpoint}, client_ip: {client_ip}")
    results["tests"]["payment"] = True
    results["passed"] += 1

    print("4. Submitting JWT to wg-jwt-peer...")
    rc, resp2 = curl_json(
        f"{VPN_PEER_API}/peer",
        method="POST",
        data={"jwt": jwt_token},
        timeout=15,
    )
    if rc != 0 or not isinstance(resp2, dict) or resp2.get("status") != "connected":
        print(f"   peer add failed: rc={rc} resp={resp2}")
        results["tests"]["jwt_peer"] = False
        results["failed"] += 1
        return results
    print(f"   peer added: {resp2}")
    results["tests"]["jwt_peer"] = True
    results["passed"] += 1

    print("5. Verifying WG peer...")
    rc, resp3 = curl_json(f"{VPN_PEER_API}/peers", headers={"Authorization": "Bearer e2e-test-secret"})
    peer_found = False
    if isinstance(resp3, dict):
        for p in resp3.get("peers", []):
            if p.get("pubkey") == pub:
                peer_found = True
                print(f"   peer confirmed: ip={p['allowed_ip']}")
    if not peer_found:
        print("   peer NOT found")
        results["tests"]["peer_verify"] = False
        results["failed"] += 1
    else:
        results["tests"]["peer_verify"] = True
        results["passed"] += 1

    print("6. Connecting WireGuard tunnel...")
    wg_host = endpoint.split(":")[0] if ":" in endpoint else endpoint
    wg_port = endpoint.split(":")[1] if ":" in endpoint else "51820"
    run(["sudo", "ip", "link", "del", "wgtest"], timeout=5)
    run(["sudo", "ip", "link", "add", "dev", "wgtest", "type", "wireguard"], timeout=5)
    run(["sudo", "ip", "addr", "add", f"{client_ip}/32", "dev", "wgtest"], timeout=5)
    run(["sudo", "ip", "route", "add", "10.66.42.0/24", "dev", "wgtest"], timeout=5)
    run(["bash", "-c", f"echo '{priv}' | sudo wg set wgtest private-key /dev/stdin peer '{server_pubkey}' endpoint {wg_host}:{wg_port} allowed-ips 10.66.42.0/24 persistent-keepalive 1"], timeout=5)
    run(["sudo", "ip", "link", "set", "wgtest", "up"], timeout=5)
    time.sleep(3)
    rc, ping_out, _ = run(["ping", "-c", "3", "-W", "2", "10.66.42.1"], timeout=10)
    run(["sudo", "ip", "link", "del", "wgtest"], timeout=5)
    if "0% packet loss" in ping_out and "0 packets received" not in ping_out:
        print(f"   tunnel works! {ping_out.splitlines()[-1]}")
        results["tests"]["tunnel"] = True
        results["passed"] += 1
    elif "3 received" in ping_out:
        print(f"   tunnel works! {ping_out.splitlines()[-1]}")
        results["tests"]["tunnel"] = True
        results["passed"] += 1
    else:
        print(f"   tunnel failed: {ping_out}")
        results["tests"]["tunnel"] = False
        results["failed"] += 1

    return results


def main():
    print("=" * 60)
    print("VPN Payment E2E Test")
    print(f"  tollgate-auth: {TOLLGATE_AUTH}")
    print(f"  vpn server:    {VPN_SERVER_ID}")
    print(f"  peer api:      {VPN_PEER_API}")
    print("=" * 60)
    print()

    results = test_vpn_e2e()

    print()
    print("=" * 60)
    total = results["passed"] + results["failed"]
    print(f"Results: {results['passed']}/{total} passed, {results['failed']} failed")
    for name, passed in results["tests"].items():
        print(f"  {name}: {'PASS' if passed else 'FAIL'}")
    print("=" * 60)

    results_dir = Path("/tmp/vpn-e2e-results")
    results_dir.mkdir(exist_ok=True)
    (results_dir / "summary.md").write_text(
        f"# VPN Payment E2E Test\n\n"
        f"**Date:** {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        f"**tollgate-auth:** {TOLLGATE_AUTH}\n"
        f"**VPN server:** {VPN_SERVER_ID}\n\n"
        f"## Results: {results['passed']}/{total} passed\n\n"
        f"| Test | Status |\n|------|--------|\n"
        + "\n".join(f"| {n} | {'PASS' if p else 'FAIL'} |" for n, p in results["tests"].items())
    )
    (results_dir / "comparison.json").write_text(json.dumps(results, indent=2))

    nsec_path = Path(NSEC_FILE)
    if nsec_path.exists():
        print("\nPublishing to Nostr...")
        os.environ["PROJECT_TAG"] = "conwrt"
        try:
            from lib.result_publisher import publish_results
            publish_results(
                results_dir=str(results_dir),
                nsec_file=str(nsec_path),
                run_id=f"conwrt-vpn-e2e-{int(time.time())}",
                blossom_server="https://blossom.psbt.me",
                relays=["wss://relay.cashu.email"],
                metadata={
                    "project": "conwrt",
                    "summary": f"VPN payment e2e: {results['passed']}/{total} passed",
                    "passed": results["passed"],
                    "failed": results["failed"],
                },
            )
            print("Published to tests.tollgate.me")
        except Exception as e:
            print(f"Publish failed: {e}")

    return 0 if results["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
