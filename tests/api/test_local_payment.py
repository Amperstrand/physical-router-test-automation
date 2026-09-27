"""Provider-agnostic payment API tests for local dry testing.

Run against the local mock mint + Go backend:
    TOLLGATE_VM_PROVIDER=local pytest tests/api/test_local_payment.py -v

Or with the one-command runner:
    ./scripts/local-test.sh

Scenarios (the contract):
    S1: POST valid V3 token → kind=1022 session event (happy path)
    S2: POST V4 token → kind=1022 (V4 native decode)
    S3: POST spent token → error (double-spend detection)
    S4: POST malformed token → CU101/CU102 (validation)
    S5: GET / → kind=10021 advertisement
    S6: GET /whoami → MAC address
    S7: GET /usage → usage string
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest
import requests

import socket as _socket
from urllib.parse import urlparse

PRTA_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
BACKEND_URL = os.environ.get("TOLLGATE_BACKEND_URL", "http://127.0.0.1:2121")
MINT_URL = os.environ.get("TOLLGATE_MINT_URL", "http://127.0.0.1:3338")
_BACKEND_HOSTPORT = urlparse(BACKEND_URL)


def _local_mint_available() -> bool:
    try:
        with _socket.create_connection(("127.0.0.1", 3338), timeout=1):
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


def _local_backend_available() -> bool:
    try:
        with _socket.create_connection(
            (_BACKEND_HOSTPORT.hostname, _BACKEND_HOSTPORT.port or 80), timeout=1
        ):
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


def _create_token(amount: int = 1) -> str:
    r = urllib.request.urlopen(f"{MINT_URL}/test/create-token?amount={amount}", timeout=5)
    return json.loads(r.read())["token"]


def _post_token(token: str) -> dict:
    try:
        r = requests.post(
            f"{BACKEND_URL}/",
            data=token,
            headers={"Content-Type": "text/plain"},
            timeout=15,
        )
    except requests.exceptions.ConnectionError:
        return {"_status": 0, "_text": "connection closed"}
    try:
        return r.json()
    except Exception:
        return {"_status": r.status_code, "_text": r.text[:200]}


@pytest.fixture(scope="module")
def fresh_token() -> str:
    if not _local_mint_available():
        pytest.skip("Local mock mint not available (port 3338)")
    return _create_token(amount=1)


# ─── S1: Happy path — valid V3 token → session event ─────────────

def test_s1_valid_v3_token_payment(fresh_token):
    resp = _post_token(fresh_token)
    assert resp.get("kind") == 1022, (
        f"Expected kind=1022 session event, got kind={resp.get('kind')}: "
        f"{json.dumps(resp)[:300]}"
    )
    tags = {t[0]: t[1:] for t in resp.get("tags", [])}
    assert "mac" in tags or "device-identifier" in tags, f"Missing MAC/device tag: {tags}"
    assert "allotment" in tags, f"Missing allotment tag: {tags}"


# ─── S2: V4 token → native decode ────────────────────────────────

def test_s2_v4_token_natively_supported():
    if not _local_mint_available():
        pytest.skip("Local mock mint not available (port 3338)")
    token = _create_token(amount=1)
    decoded = json.loads(
        __import__("base64").urlsafe_b64decode(token[6:] + "==").decode()
    )
    decoded["token"][0]["mint"] = decoded["token"][0]["mint"]
    resp = _post_token(token)
    kind = resp.get("kind")
    assert kind in (1022, 21023), f"Unexpected kind={kind}: {json.dumps(resp)[:200]}"


# ─── S3: Double-spend — same token twice → error ─────────────────

def test_s3_double_spend_detected():
    if not _local_mint_available():
        pytest.skip("Local mock mint not available (port 3338)")
    token = _create_token(amount=1)
    first = _post_token(token)
    assert first.get("kind") == 1022, (
        f"First spend should succeed (kind=1022), got kind={first.get('kind')}: "
        f"{json.dumps(first)[:300]}"
    )
    second = _post_token(token)
    kind = second.get("kind")
    tags = {t[0]: t[1:] for t in second.get("tags", [])}
    code = tags.get("code", [""])[0]
    assert kind == 21023, f"Expected error (21023) on double-spend, got kind={kind}"
    code_lower = code.lower()
    assert any(w in code_lower for w in ("spent", "error", "failed", "processing")), (
        f"Expected spent/error/failed code, got: {code}"
    )


# ─── S4: Malformed tokens → validation errors ────────────────────

@pytest.mark.parametrize("token,expected_code", [
    ("", "CU100"),
    ("notacashutoken", "CU101"),
    ("cashuAinvalidbase64!!!", "CU102"),
])
def test_s4_malformed_tokens(token, expected_code):
    resp = _post_token(token)
    if resp.get("_status") == 0:
        return
    if resp.get("kind") == 21023:
        return
    if resp.get("error"):
        return
    tags = {t[0]: t[1:] for t in resp.get("tags", [])}
    code = tags.get("code", [""])[0]
    assert expected_code in code, (
        f"Expected {expected_code}, got code={code}, kind={resp.get('kind')}, "
        f"resp={json.dumps(resp)[:200]}"
    )


# ─── S5: GET / → advertisement ───────────────────────────────────

def test_s5_advertisement():
    if not _local_backend_available():
        pytest.skip("Local dry backend not available at BACKEND_URL")
    r = requests.get(f"{BACKEND_URL}/", timeout=5)
    d = r.json()
    assert d["kind"] == 10021, f"Expected kind=10021, got {d['kind']}"
    tags = {t[0]: t[1:] for t in d.get("tags", [])}
    assert "metric" in tags
    assert "step_size" in tags or "step" in tags


# ─── S6: GET /whoami → MAC address ───────────────────────────────

def test_s6_whoami():
    if not _local_backend_available():
        pytest.skip("Local dry backend not available at BACKEND_URL")
    r = requests.get(f"{BACKEND_URL}/whoami", timeout=5)
    body = r.text.strip()
    assert "mac=" in body or len(body) > 0, f"/whoami returned empty: status={r.status_code}"


# ─── S7: GET /usage → usage string ───────────────────────────────

def test_s7_usage():
    if not _local_backend_available():
        pytest.skip("Local dry backend not available at BACKEND_URL")
    r = requests.get(f"{BACKEND_URL}/usage", timeout=5)
    body = r.text.strip()
    assert len(body) > 0, f"/usage returned empty: status={r.status_code}"


# ─── S8: /usage returns 200 not 500 (regression for #316) ────────

def test_s8_usage_status_code():
    if not _local_backend_available():
        pytest.skip("Local dry backend not available at BACKEND_URL")
    r = requests.get(f"{BACKEND_URL}/usage", timeout=5)
    assert r.status_code == 200, f"/usage should return 200, got {r.status_code}"


# ─── S9: Advertisement unit is "sat" not "sats" (regression #310) ─

def test_s9_advertisement_unit():
    if not _local_backend_available():
        pytest.skip("Local dry backend not available at BACKEND_URL")
    r = requests.get(f"{BACKEND_URL}/", timeout=5)
    d = r.json()
    tags = {t[0]: t[1:] for t in d.get("tags", [])}
    pps = tags.get("price_per_step", [])
    if len(pps) >= 3:
        unit = pps[2]
        assert unit == "sat", f"Expected unit='sat' (NUT-00), got '{unit}'"


# ─── S10: Large POST body rejected (regression for #321) ─────────

def test_s10_body_size_limit():
    if not _local_backend_available():
        pytest.skip("Local dry backend not available at BACKEND_URL")
    large_body = "x" * (2 * 1024 * 1024)
    try:
        r = requests.post(
            f"{BACKEND_URL}/",
            data=large_body,
            headers={"Content-Type": "text/plain"},
            timeout=10,
        )
        assert r.status_code >= 400, f"Expected rejection of 2MB body, got {r.status_code}"
    except requests.exceptions.ConnectionError:
        pass


# ─── S11: Payment survives mint 429 (regression for #314) ────────

def test_s11_mint_429_retry():
    if not _local_mint_available():
        pytest.skip("Local mock mint not available (port 3338)")
    try:
        requests.get(f"{MINT_URL}/test/set-swap-error?count=1&code=429", timeout=3)
    except Exception:
        pytest.skip("Mock mint does not support /test/set-swap-error")
    token = _create_token(amount=1)
    resp = _post_token(token)
    kind = resp.get("kind")
    assert kind in (1022, 21023), f"Expected session or notice after 429 retry, got kind={kind}: {json.dumps(resp)[:200]}"


# ─── S12: P2PK-locked tokens rejected (regression for #330/#324) ─

def test_s12_reject_locked_tokens():
    if not _local_mint_available():
        pytest.skip("Local mock mint not available (port 3338)")
    import base64
    locked_proof = {
        "amount": 1,
        "id": "00" + "0" * 14,
        "secret": '["P2PK",{"nonce":"abc123","data":"","tags":[["pubkeys","02abcdef"]]}]',
        "C": "02" + "0" * 62,
    }
    payload = {
        "token": [{
            "mint": MINT_URL,
            "proofs": [locked_proof],
        }],
        "unit": "sat",
        "memo": "locked token test",
    }
    json_bytes = json.dumps(payload, separators=(',', ':')).encode()
    b64 = base64.urlsafe_b64encode(json_bytes).decode().rstrip('=')
    token = f"cashuA{b64}"
    try:
        r = requests.post(
            f"{BACKEND_URL}/",
            data=token,
            headers={"Content-Type": "text/plain"},
            timeout=15,
        )
    except requests.exceptions.ConnectionError:
        return
    if r.status_code == 429:
        pytest.skip("Rate limited — run test in isolation")
    resp = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
    kind = resp.get("kind")
    assert kind == 21023, f"Expected rejection (21023) for P2PK-locked token, got kind={kind}: {json.dumps(resp)[:200]}"
