import pytest
#!/usr/bin/env python3
"""
TollGate Gateway Token Format Smoke Tests (x86)

Tests run from this Linux machine against the live TollGate gateway.
Gateway IP configurable via TOLLGATE_IP env var (default 10.230.237.1).

SETUP/TEARDOWN: These tests modify the gateway's /etc/tollgate/config.json
to add a testnut mint entry for test duration, then restore the original
config on exit. This makes the tests self-contained — no manual gateway
config needed.

Root cause of Android payment failure (verified):
  Android app produced tokens with "cashuB" prefix (V4 = CBOR) but JSON
  payload (V3 structure). Gateway DecodeToken() tries V4 CBOR first
  (sees cashuB, attempts CBOR unmarshal on JSON → fails), then V3
  fallback (expects cashuA → also fails). Error: "invalid V3 token".

  Additionally, the token used short keys (i/p/a/s/c) which are the V4
  CBOR convention — V3 JSON needs full keys (mint/proofs/amount/secret/C).
  And keyset ID was missing from proofs entirely.

Usage:
  python3 tests/api/test_gateway_token_format.py [--gateway IP] [--verbose]

Requirements:
  pip install requests cbor2
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

pytestmark = [
pytest.mark.story("R3"),
pytest.mark.tip("TIP-02"),
]


# cbor2 for generating proper V4 tokens
try:
    import cbor2
    HAS_CBOR = True
except ImportError:
    HAS_CBOR = False

DEFAULT_GATEWAY = os.environ.get("TOLLGATE_IP", "10.230.237.1")
GATEWAY_PORT = 2121
SSH_USER = "root"
CONFIG_PATH = "/etc/tollgate/config.json"
CONFIG_BACKUP = "/etc/tollgate/config.json.smoketest.bak"

# Test mint — added to gateway during setup, removed during teardown
TEST_MINT = "https://nofee.testnut.cashu.space"
TEST_KEYSET_ID = "00b4cd27d8861a44"

# Colors for terminal output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"


class TestResult:
    def __init__(self, name: str, passed: bool | None, detail: str = ""):
        self.name = name
        self.passed = passed
        self.detail = detail

    def __str__(self):
        status = f"{GREEN}PASS{RESET}" if self.passed else f"{RED}FAIL{RESET}" if self.passed is False else f"{YELLOW}SKIP{RESET}"
        line = f"  [{status}] {self.name}"
        if self.detail:
            line += f"\n           {self.detail}"
        return line


# ── HTTP helpers ──────────────────────────────────────────────────

def http_get(url: str, timeout: int = 10) -> tuple[int, str, dict]:
    """GET request, returns (status_code, body, headers)."""
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)
    except Exception as e:
        return 0, str(e), {}


def http_post(url: str, body: str, content_type: str = "text/plain", timeout: int = 30) -> tuple[int, str]:
    """POST request, returns (status_code, body)."""
    try:
        data = body.encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", content_type)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)


def http_options(url: str, timeout: int = 5) -> tuple[int, dict]:
    """OPTIONS request, returns (status_code, headers)."""
    try:
        req = urllib.request.Request(url, method="OPTIONS")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)
    except Exception as e:
        return 0, {}


# ── SSH helpers ───────────────────────────────────────────────────

def ssh_cmd(gateway_ip: str, remote_cmd: str, timeout: int = 15) -> tuple[int, str]:
    """Run a command on the gateway via SSH. Returns (exit_code, output)."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
             f"{SSH_USER}@{gateway_ip}", remote_cmd],
            capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return 1, "SSH timeout"
    except Exception as e:
        return 1, str(e)


def scp_to(gateway_ip: str, local_path: str, remote_path: str) -> bool:
    """Copy a local file to the gateway via SSH cat (more reliable than scp on OpenWrt)."""
    try:
        with open(local_path) as f:
            content = f.read()
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
             f"{SSH_USER}@{gateway_ip}", f"cat > {remote_path}"],
            input=content, capture_output=True, text=True, timeout=15
        )
        return result.returncode == 0
    except Exception:
        return False


def scp_from(gateway_ip: str, remote_path: str, local_path: str) -> bool:
    """Copy a remote file from the gateway via SSH cat."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
             f"{SSH_USER}@{gateway_ip}", f"cat {remote_path}"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return False
        with open(local_path, "w") as f:
            f.write(result.stdout)
        return True
    except Exception:
        return False


# ── Gateway config setup/teardown ────────────────────────────────

def setup_gateway(gateway_ip: str, verbose: bool = False) -> bool:
    """
    Back up the gateway config and add testnut mint to accepted_mints.
    Returns True on success.
    """
    print(f"{BOLD}  Setting up gateway config...{RESET}")

    # Step 1: Back up original config
    code, out = ssh_cmd(gateway_ip, f"cp {CONFIG_PATH} {CONFIG_BACKUP}")
    if code != 0:
        print(f"  {RED}FAIL:{RESET} Could not back up config: {out.strip()}")
        return False

    # Step 2: Fetch config to local machine
    local_config = f"/tmp/tollgate-config-{gateway_ip}.json"
    if not scp_from(gateway_ip, CONFIG_PATH, local_config):
        print(f"  {RED}FAIL:{RESET} Could not fetch config via SCP")
        return False

    # Step 3: Parse and add testnut mint if not present
    try:
        with open(local_config) as f:
            config = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(f"  {RED}FAIL:{RESET} Could not parse config: {e}")
        return False

    accepted_mints = config.get("accepted_mints", [])
    existing_urls = [m.get("url", "") for m in accepted_mints]

    if TEST_MINT in existing_urls:
        if verbose:
            print(f"  {YELLOW}NOTE:{RESET} Test mint already in config, skipping add")
        return True

    # Add testnut with minimal settings (no fees, no payout threshold)
    testnut_entry = {
        "url": TEST_MINT,
        "min_balance": 0,
        "balance_tolerance_percent": 0,
        "payout_interval_seconds": 999999,
        "min_payout_amount": 999999,
        "price_per_step": 1,
        "price_unit": "sats",
        "purchase_min_steps": 0,
    }
    accepted_mints.append(testnut_entry)
    config["accepted_mints"] = accepted_mints

    # Step 4: Write modified config and push back
    with open(local_config, "w") as f:
        json.dump(config, f, indent=2)

    if not scp_to(gateway_ip, local_config, CONFIG_PATH):
        print(f"  {RED}FAIL:{RESET} Could not push config via SCP")
        return False

    # Step 5: Restart tollgate-wrt to pick up new config
    code, out = ssh_cmd(gateway_ip, "/etc/init.d/tollgate-wrt restart", timeout=30)
    if code != 0:
        # Try alternative restart command
        code, out = ssh_cmd(gateway_ip, "killall -HUP tollgate-wrt 2>/dev/null; sleep 2", timeout=30)

    if verbose:
        print(f"  {GREEN}OK:{RESET} Added {TEST_MINT} to accepted_mints, restarted service")

    # Clean up local temp file
    try:
        os.unlink(local_config)
    except OSError:
        pass

    # Wait for service to come back
    import time
    time.sleep(3)

    return True


def teardown_gateway(gateway_ip: str, verbose: bool = False) -> None:
    """
    Restore the original gateway config from backup.
    Safe to call even if setup failed — checks for backup existence.
    """
    print(f"{BOLD}  Tearing down gateway config...{RESET}")

    # Check if backup exists
    code, out = ssh_cmd(gateway_ip, f"test -f {CONFIG_BACKUP} && echo exists")
    if "exists" not in out:
        if verbose:
            print(f"  {YELLOW}NOTE:{RESET} No backup found, skipping teardown")
        return

    # Restore backup
    code, out = ssh_cmd(gateway_ip, f"mv {CONFIG_BACKUP} {CONFIG_PATH}")
    if code != 0:
        print(f"  {RED}WARN:{RESET} Could not restore config: {out.strip()}")
        return

    # Restart service
    code, out = ssh_cmd(gateway_ip, "/etc/init.d/tollgate-wrt restart", timeout=30)
    if code != 0:
        code, out = ssh_cmd(gateway_ip, "killall -HUP tollgate-wrt 2>/dev/null; sleep 2", timeout=30)

    if verbose:
        print(f"  {GREEN}OK:{RESET} Restored original config, restarted service")


# ── Token builders ───────────────────────────────────────────────

def build_v3_token(mint: str, keyset_id: str, amount: int = 1) -> str:
    """Build a proper cashuA V3 token (JSON + base64url) with FULL keys.

    This matches what the fixed Android app produces (commit cd4a31c):
    - cashuA prefix (V3 JSON)
    - Full key names: mint, proofs, amount, secret, C, id
    - Keyset ID included for proof verification
    """
    token_json = {
        "token": [{
            "mint": mint,
            "proofs": [{
                "amount": amount,
                "secret": "test_secret_hex_" + "0" * 40,
                "C": "02" + "a" * 62,  # fake compressed pubkey
                "id": keyset_id,
            }]
        }],
        "unit": "sat",
        "memo": "x86 smoke test",
    }
    json_bytes = json.dumps(token_json).encode()
    b64 = base64.urlsafe_b64encode(json_bytes).rstrip(b"=").decode()
    return f"cashuA{b64}"


def build_v4_cbor_token(mint: str, keyset_id: str, amount: int = 1) -> str:
    """Build a proper cashuB V4 token (CBOR + base64url)."""
    if not HAS_CBOR:
        return build_v3_token(mint, keyset_id, amount)  # fallback
    keyset_bytes = bytes.fromhex(keyset_id)
    token_cbor = {
        "mint": mint,
        "proofs": [{
            "id": keyset_bytes,
            "amount": amount,
            "secret": "test_secret_hex",
            "C": bytes.fromhex("02" + "a" * 62),
        }],
    }
    cbor_bytes = cbor2.dumps(token_cbor)
    b64 = base64.urlsafe_b64encode(cbor_bytes).rstrip(b"=").decode()
    return f"cashuB{b64}"


def build_buggy_token(mint: str, keyset_id: str, amount: int = 1) -> str:
    """Build the BUGGY token (what the Android app produced before fix).

    Three bugs reproduced:
    1. cashuB prefix with JSON content (should be cashuA for JSON)
    2. Short keys (i/p/a/s/c) — V4 CBOR convention, not V3 JSON
    3. No keyset ID in proofs
    """
    token_json = {
        "token": [{
            "i": mint,
            "p": [{
                "a": amount,
                "s": "test_secret_hex_" + "0" * 40,
                "c": "02" + "a" * 62,
            }]
        }],
        "unit": "sat",
        "memo": "buggy token",
    }
    json_bytes = json.dumps(token_json).encode()
    b64 = base64.urlsafe_b64encode(json_bytes).rstrip(b"=").decode()
    # BUG: cashuB prefix with JSON content
    return f"cashuB{b64}"


# ── Tests ─────────────────────────────────────────────────────────

def run_tests(gateway_ip: str, verbose: bool = False) -> list[TestResult]:
    base = f"http://{gateway_ip}:{GATEWAY_PORT}"
    results = []

    # ── Test 1: Gateway advertisement (GET /) ──────────────────
    code, body, headers = http_get(f"{base}/")
    if code == 200:
        try:
            ad = json.loads(body)
            has_kind = ad.get("kind") == 10021
            tags = ad.get("tags", [])
            tag_names = [t[0] if isinstance(t, list) and t else "" for t in tags]
            has_metric = "metric" in tag_names
            has_step = "step_size" in tag_names
            has_price = "price_per_step" in tag_names
            has_tips = "tips" in tag_names

            passed = has_kind and has_metric and has_step and has_price and has_tips
            detail = ""
            if not passed:
                missing = []
                if not has_kind: missing.append("kind:10021")
                if not has_metric: missing.append("metric")
                if not has_step: missing.append("step_size")
                if not has_price: missing.append("price_per_step")
                if not has_tips: missing.append("tips")
                detail = f"Missing: {', '.join(missing)}"
            elif verbose:
                mint_urls = [t[4] for t in tags if t[0] == "price_per_step" and len(t) >= 5]
                detail = f"Mints: {mint_urls}"
            results.append(TestResult("GET / returns valid 10021 advertisement", passed, detail))
        except json.JSONDecodeError:
            results.append(TestResult("GET / returns valid 10021 advertisement", False, "Invalid JSON"))
    else:
        results.append(TestResult("GET / returns valid 10021 advertisement", False, f"HTTP {code}"))

    # ── Test 2: POST proper V3 token (cashuA + JSON, full keys) ─
    token_v3 = build_v3_token(TEST_MINT, TEST_KEYSET_ID)
    code, body = http_post(f"{base}/", token_v3)
    try:
        resp = json.loads(body)
        code_tag = ""
        for tag in resp.get("tags", []):
            if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "code":
                code_tag = tag[1]
                break
        # V3 token should parse OK. May fail at wallet receive (fake proofs)
        # but must NOT fail with "payment-error-invalid-token"
        passed = code_tag != "payment-error-invalid-token"
        detail = f"HTTP {code}, code={code_tag}" if verbose else ""
        results.append(TestResult("POST cashuA V3 token, full keys (parsing layer)", passed, detail))
    except (json.JSONDecodeError, KeyError):
        passed = "invalid-token" not in body and "invalid V3" not in body
        results.append(TestResult("POST cashuA V3 token, full keys (parsing layer)", passed, f"HTTP {code}"))

    # ── Test 3: POST proper V4 CBOR token (cashuB + CBOR) ─────
    if HAS_CBOR:
        token_v4 = build_v4_cbor_token(TEST_MINT, TEST_KEYSET_ID)
        code, body = http_post(f"{base}/", token_v4)
        try:
            resp = json.loads(body)
            code_tag = ""
            for tag in resp.get("tags", []):
                if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "code":
                    code_tag = tag[1]
                    break
            passed = code_tag != "payment-error-invalid-token"
            detail = f"HTTP {code}, code={code_tag}" if verbose else ""
            results.append(TestResult("POST cashuB V4 CBOR token (parsing layer)", passed, detail))
        except (json.JSONDecodeError, KeyError):
            passed = "invalid-token" not in body
            results.append(TestResult("POST cashuB V4 CBOR token (parsing layer)", passed, f"HTTP {code}"))
    else:
        results.append(TestResult("POST cashuB V4 CBOR token (parsing layer)", None, "cbor2 not installed — skipped"))

    # ── Test 4: POST buggy cashuB+JSON token (regression) ─────
    token_bug = build_buggy_token(TEST_MINT, TEST_KEYSET_ID)
    code, body = http_post(f"{base}/", token_bug)
    try:
        resp = json.loads(body)
        code_tag = ""
        for tag in resp.get("tags", []):
            if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "code":
                code_tag = tag[1]
                break
        # This MUST fail — it's the bug. Verifies the gateway rejects bad format.
        passed = code_tag == "payment-error-invalid-token"
        detail = f"HTTP {code}, code={code_tag} (expected payment-error-invalid-token)"
        results.append(TestResult("POST buggy cashuB+JSON token is rejected (regression)", passed, detail))
    except (json.JSONDecodeError, KeyError):
        passed = "invalid-token" in body or "invalid V3" in body
        results.append(TestResult("POST buggy cashuB+JSON token is rejected (regression)", passed, f"HTTP {code}"))

    # ── Test 5: POST whitespace token ─────────────────────────
    code, body = http_post(f"{base}/", " ")
    passed = code in (0, 400, 405, 500)
    results.append(TestResult("POST whitespace token returns error", passed, f"HTTP {code}"))

    # ── Test 6: POST garbage token ────────────────────────────
    code, body = http_post(f"{base}/", "notacashutoken")
    try:
        resp = json.loads(body)
        code_tag = ""
        for tag in resp.get("tags", []):
            if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "code":
                code_tag = tag[1]
                break
        passed = code in (400,) or "error" in code_tag
        results.append(TestResult("POST garbage token returns 400", passed, f"HTTP {code}, code={code_tag}"))
    except (json.JSONDecodeError, KeyError):
        passed = code == 400
        results.append(TestResult("POST garbage token returns 400", passed, f"HTTP {code}"))

    # ── Test 7: CORS headers present ──────────────────────────
    code, hdrs = http_options(f"{base}/")
    cors_headers_val = hdrs.get("Access-Control-Allow-Headers", "") or hdrs.get("access-control-allow-headers", "")
    passed = "Content-Type" in cors_headers_val
    results.append(TestResult("CORS headers present (OPTIONS)", passed, f"Allow-Headers: {cors_headers_val[:80]}"))

    # ── Test 8: GET /whoami ───────────────────────────────────
    code, body, _ = http_get(f"{base}/whoami")
    passed = code == 200 and ("mac" in body.lower() or "{" in body)
    results.append(TestResult("GET /whoami returns MAC or JSON", passed, f"HTTP {code}, body={body[:80]}"))

    # ── Test 9: Gateway config includes testnut (added during setup) ──
    # Note: the gateway may not ADVERTISE mints with 0 balance, so we check
    # the config file directly via SSH rather than the live advertisement.
    code_ssh, config_out = ssh_cmd(gateway_ip, f"cat {CONFIG_PATH}")
    passed = TEST_MINT in config_out
    detail = f"testnut in config: {passed}" if verbose else ""
    results.append(TestResult(f"Gateway config includes testnut mint", passed, detail))

    # ── Test 10: V3 token decodes to valid JSON structure ─────
    v3 = build_v3_token(TEST_MINT, TEST_KEYSET_ID)
    payload = v3[6:]  # after cashuA
    padded = payload + "=" * (4 - len(payload) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(padded))
        has_token_key = "token" in decoded
        has_unit = decoded.get("unit") == "sat"
        # Verify full keys are present
        first_mint = decoded["token"][0]
        has_full_mint_key = "mint" in first_mint
        has_full_proofs_key = "proofs" in first_mint
        first_proof = first_mint["proofs"][0]
        has_amount = "amount" in first_proof
        has_secret = "secret" in first_proof
        has_C = "C" in first_proof
        has_id = "id" in first_proof
        passed = all([has_token_key, has_unit, has_full_mint_key, has_full_proofs_key,
                      has_amount, has_secret, has_C, has_id])
        results.append(TestResult("V3 token has full JSON keys + keyset ID", passed, ""))
    except Exception as e:
        results.append(TestResult("V3 token has full JSON keys + keyset ID", False, str(e)))

    return results


def main():
    parser = argparse.ArgumentParser(description="TollGate gateway token format smoke tests")
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY, help=f"Gateway IP (default: {DEFAULT_GATEWAY})")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--no-setup", action="store_true", help="Skip gateway config setup/teardown")
    args = parser.parse_args()

    print(f"\n{BOLD}TollGate Gateway Smoke Tests{RESET}")
    print(f"  Target: http://{args.gateway}:{GATEWAY_PORT}\n")

    # ── Setup: add testnut mint to gateway config ──
    if not args.no_setup:
        setup_ok = setup_gateway(args.gateway, args.verbose)
        if not setup_ok:
            print(f"  {YELLOW}WARNING:{RESET} Setup failed — continuing with existing config")

    # ── Run tests ──
    try:
        results = run_tests(args.gateway, args.verbose)
    finally:
        # ── Teardown: restore original config ──
        if not args.no_setup:
            teardown_gateway(args.gateway, args.verbose)

    print()
    for r in results:
        print(r)

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if r.passed is False)
    skipped = sum(1 for r in results if r.passed is None)
    total = len(results)

    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"  {GREEN}Passed:{RESET} {passed}  {RED}Failed:{RESET} {failed}  {YELLOW}Skipped:{RESET} {skipped}  Total: {total}")
    print(f"{BOLD}{'='*60}{RESET}\n")

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
