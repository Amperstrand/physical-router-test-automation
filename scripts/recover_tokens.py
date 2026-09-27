#!/usr/bin/env python3
"""Recover stuck Cashu tokens from tollgate-wrt tokens-to-recover.txt.

Usage:
    # Check which tokens are still unspent (no changes)
    python scripts/recover_tokens.py --file tokens-to-recover.txt --check

    # Check + re-submit unspent tokens to router
    python scripts/recover_tokens.py --file tokens-to-recover.txt --recover

    # Dry run (parse + show what would happen, no network calls)
    python scripts/recover_tokens.py --file tokens-to-recover.txt --dry-run

Token file format (pipe-delimited, one per line):
    2026-06-18T16:14:05Z | https://nofee.testnut.cashu.space | cashuBo2F0... | payment rejected: failed to open gate: exit status 1
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests

# NUT-07 checkstate endpoint
CHECKSTATE_PATH = "/v1/checkstate"

# Cashu token prefixes
TOKEN_PREFIX_A = "cashuA"
TOKEN_PREFIX_B = "cashuB"

# Actions reported in ResultRecord
ACTION_CHECK_ONLY = "CHECK_ONLY"
ACTION_SUBMITTED = "SUBMITTED"
ACTION_SUBMIT_FAILED = "SUBMIT_FAILED"
ACTION_SKIPPED_SPENT = "SKIPPED_SPENT"
ACTION_CHECK_FAILED = "CHECK_FAILED"
ACTION_DRY_RUN = "DRY_RUN"


# --------------------------------------------------------------------------- #
# Data records
# --------------------------------------------------------------------------- #

@dataclass
class TokenRecord:
    """One line from tokens-to-recover.txt."""
    timestamp: str
    mint_url: str
    token: str
    error: str = ""


@dataclass
class ResultRecord:
    """Result of processing a single token."""
    timestamp: str
    mint_url: str
    amount: int = 0
    state: str = ""        # UNSPENT / SPENT / ERROR / "" (dry-run: not checked)
    action: str = ""       # CHECK_ONLY / SUBMITTED / SUBMIT_FAILED / SKIPPED_SPENT / CHECK_FAILED / DRY_RUN
    submit_status: int | None = None
    submit_body: str = ""
    error: str = ""
    unknown_count: int = 0  # proofs the mint didn't resolve to UNSPENT/SPENT


# --------------------------------------------------------------------------- #
# Token file parsing
# --------------------------------------------------------------------------- #

def parse_token_file(filepath: str) -> list[TokenRecord]:
    """Parse tokens-to-recover.txt into list of token records.

    Each line: timestamp | mint_url | cashuB_token | rejection_error
    """
    records: list[TokenRecord] = []
    text = Path(filepath).read_text()
    for line in text.strip().split("\n"):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(" | ")]
        if len(parts) < 3:
            continue
        records.append(TokenRecord(
            timestamp=parts[0],
            mint_url=parts[1],
            token=parts[2],
            error=parts[3] if len(parts) > 3 else "",
        ))
    return records


# --------------------------------------------------------------------------- #
# Token decoding
# --------------------------------------------------------------------------- #

def decode_cashu_token(token: str) -> dict:
    """Decode a Cashu token: V1/V3 (cashuA, base64url JSON) or V4
    (cashuB, base64url CBOR).

    Returns the decoded structure with proofs/token/mint info.

    Raises ValueError on unknown prefixes.
    """
    import zlib

    token = token.strip()
    low = token.lower()

    if low.startswith(TOKEN_PREFIX_A.lower()):
        raw = token[len(TOKEN_PREFIX_A):]
        prefix = TOKEN_PREFIX_A
        encoder = "json"
    elif low.startswith(TOKEN_PREFIX_B.lower()):
        raw = token[len(TOKEN_PREFIX_B):]
        prefix = TOKEN_PREFIX_B
        encoder = "cbor"
    else:
        raise ValueError(f"unknown token prefix: {token[:20]}...")

    # base64url decode
    padded = raw + "=" * (4 - len(raw) % 4)
    data = base64.urlsafe_b64decode(padded)

    # Optional gzip (some V3 serializations are gzipped; V4 CBOR is not)
    try:
        data = zlib.decompress(data, wbits=zlib.MAX_WBITS | 16)
    except Exception:
        pass  # not compressed

    if encoder == "json":
        return json.loads(data)
    else:
        # CBOR decode
        try:
            import cbor2
            return cbor2.loads(data)
        except ImportError:
            # Fallback: try treating as JSON after decompress
            try:
                return json.loads(data)
            except Exception:
                raise ValueError("Cannot decode CBOR token without cbor2 library")


_CBOR_KEY_MAP = {"a": "amount", "s": "secret", "c": "C", "id": "id"}


def _normalize_proof(p: dict) -> dict:
    """Normalize CBOR short keys to long names (a→amount, s→secret, c→C)."""
    if not isinstance(p, dict):
        return p
    # If already has long keys, return as-is
    if "amount" in p or "secret" in p:
        return p
    # Map short keys to long
    result = {}
    for k, v in p.items():
        result[_CBOR_KEY_MAP.get(k, k)] = v
    return result


def extract_proofs(decoded: Any) -> list[dict]:
    """Extract proof list from decoded token structure.

    Handles V4 CBOR format (cashuB): {"t": [{"i": keyset_id, "m": mint_url,
    "p": [...]}], "d": unit, "a": memo}
    Handles V3 JSON format (cashuA): {"token": [{"mint": ..., "proofs": [...]}], "unit": "sat"}
    Handles V4-JSON-embedded format: {"token": {"mint": ..., "proofs": [...]}, ...}

    CBOR proofs use short keys (a, s, c); these are normalized to
    long names (amount, secret, C) for consistent access.
    """
    if isinstance(decoded, dict):
        if "t" in decoded:
            token_list = decoded["t"]
            if isinstance(token_list, list):
                proofs = []
                for entry in token_list:
                    proofs.extend(_normalize_proof(p) for p in entry.get("p", []))
                return proofs
        # JSON nested token
        token_obj = decoded.get("token", decoded)
        if isinstance(token_obj, dict):
            return token_obj.get("proofs", [])
        elif isinstance(token_obj, list):
            proofs = []
            for entry in token_obj:
                proofs.extend(entry.get("proofs", []))
            return proofs
    elif isinstance(decoded, list):
        return decoded
    return []


def _entry_mint(entry: Any) -> str:
    """Mint URL of one token entry, in either spelling (`mint` / V4 short `m`)."""
    if not isinstance(entry, dict):
        return ""
    for key in ("mint", "m"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def extract_mint_url(decoded: Any) -> str:
    """Extract mint URL from decoded token if present.

    Handles every layout the kit actually sees:

    * NUT-00 **V4 CBOR** (`cashuB`): the mint is a **top-level** ``m`` next to
      ``t``/``u``/``d`` — this is what the Python ``cashu`` wallet emits, and
      what the bench tokens in ``~/.tg-e2e`` are. It was missed before, so a
      real V4 token reported "no mint" and could not be checkstate-verified.
    * the simplified V4 fixture shape with ``m`` on each ``t`` entry;
    * V3 JSON (``cashuA``): ``{"token": [{"mint": ..., "proofs": [...]}]}``.
    """
    if not isinstance(decoded, dict):
        return ""
    top = decoded.get("m")
    if isinstance(top, str) and top and "t" in decoded:
        return top
    token_obj = decoded.get("token", decoded)
    url = _entry_mint(token_obj)
    if url:
        return url
    if isinstance(token_obj, dict):
        for entry in token_obj.get("t") or []:
            url = _entry_mint(entry)
            if url:
                return url
    elif isinstance(token_obj, list):
        for entry in token_obj:
            url = _entry_mint(entry)
            if url:
                return url
    return ""


# --------------------------------------------------------------------------- #
# Proof Y-value computation (NUT-07)
# --------------------------------------------------------------------------- #

# NUT-00 domain separator, matches gonuts crypto/bdhke.go HashToCurve —
# the derivation the mint keys NUT-07 checkstate on (mint/mint.go:492/783/1607).
_HASH_TO_CURVE_DOMAIN = b"Secp256k1_HashToCurve_Cashu_"


def compute_proof_y(proof: dict) -> str:
    """Y = HashToCurve(secret), serialized compressed (66 hex chars).

    This is the value the mint's NUT-07 checkstate keys on. It is NOT
    the proof's C (blind signature) and NOT PK(sha256(secret)).

    The secret string is hashed verbatim (UTF-8 bytes) — never
    hex-decoded, even when it looks like hex: gonuts hashes
    ``[]byte(proof.Secret)`` as-is. CBOR byte-string secrets hash as
    their raw bytes. Any C/Y/c fields on the (untrusted) proof are
    ignored — a Y field must not be trusted to identify its own spend
    state, and C is simply the wrong value.
    """
    secret = proof.get("secret") or proof.get("s")
    if secret is None:
        return ""
    if isinstance(secret, (bytes, bytearray)):
        secret_bytes = bytes(secret)  # CBOR byte-string secrets
    else:
        secret_bytes = str(secret).encode("utf-8")  # mint hashes the string verbatim
    return _hash_to_curve(secret_bytes).hex()


def _hash_to_curve(message: bytes) -> bytes:
    """NUT-00 HashToCurve — port of gonuts crypto.HashToCurve (bdhke.go).

    sha256(domain || message), then counter-loop (4-byte little-endian)
    hashing until the digest is a valid even-y x-coordinate; returns
    the 33-byte compressed point.
    """
    import coincurve

    msg_hash = hashlib.sha256(_HASH_TO_CURVE_DOMAIN + message).digest()
    for counter in range(2**16):
        c = counter.to_bytes(4, "little")
        h = hashlib.sha256(msg_hash + c).digest()
        try:
            return coincurve.PublicKey(b"\x02" + h).format()  # compressed
        except ValueError:
            continue
    raise ValueError("hash_to_curve: no valid point after 2**16 iterations")


def build_checkstate_request(proofs: list[dict]) -> dict:
    """Build the NUT-07 checkstate request payload.

    Returns {"Ys": [y_hex, ...]} — a top-level list of Y hex strings per NUT-07 spec.
    """
    ys = [compute_proof_y(p) for p in proofs]
    ys = [y for y in ys if y]  # filter empties
    return {"Ys": ys}


# --------------------------------------------------------------------------- #
# Checkstate (network)
# --------------------------------------------------------------------------- #

def check_token_state(mint_url: str, token: str, timeout: int = 10) -> dict:
    """Check if token's proofs are spent at the mint (NUT-07 checkstate).

    Returns dict with 'unspent_count', 'spent_count', 'unknown_count'
    (proofs the mint didn't resolve to UNSPENT/SPENT — e.g. UNKNOWN or
    PENDING states; some mints also answer UNSPENT for completely
    unknown Ys), and 'total_proofs'.
    """
    decoded = decode_cashu_token(token)
    proofs = extract_proofs(decoded)

    if not proofs:
        return {"unspent_count": 0, "spent_count": 0, "unknown_count": 0,
                "total_proofs": 0, "error": "no proofs found"}

    payload = build_checkstate_request(proofs)

    resp = requests.post(
        f"{mint_url}{CHECKSTATE_PATH}",
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    states = data.get("states", [])
    unspent = sum(1 for s in states if s.get("state") == "UNSPENT")
    spent = sum(1 for s in states if s.get("state") == "SPENT")

    return {
        "unspent_count": unspent,
        "spent_count": spent,
        "unknown_count": len(states) - unspent - spent,
        "total_proofs": len(states),
    }


def submit_token_to_router(router_ip: str, token: str, timeout: int = 30):
    """Re-submit a Cashu token to the router's API for processing.

    The router API on :2121 accepts the raw token string as POST body
    (not JSON-wrapped) — same format the captive portal uses.
    """
    resp = requests.post(
        f"http://{router_ip}:2121/",
        data=token,
        headers={"Content-Type": "text/plain"},
        timeout=timeout,
    )
    return resp.status_code, resp.text


def get_token_amount(decoded: Any) -> int:
    """Sum all proof amounts from decoded token.

    Handles CBOR short keys (a=amount) and JSON keys (amount=amount).
    """
    proofs = extract_proofs(decoded)
    return sum(p.get("a", p.get("amount", 0)) for p in proofs)


# --------------------------------------------------------------------------- #
# process_tokens — orchestration
# --------------------------------------------------------------------------- #

def process_tokens(
    filepath: str,
    mode: str = "check",
    router_ip: str = "192.168.8.1",
    delay: float = 1.0,
) -> list[ResultRecord]:
    """Process all tokens in the file.

    Modes:
      "check"   — check state only, no submission
      "recover" — check state, submit unspent tokens to router
      "dry-run" — parse + show what would happen, no network calls at all
                  (spend state is NOT checked)

    Returns a list of ResultRecord.
    """
    records = parse_token_file(filepath)
    results: list[ResultRecord] = []

    for record in records:
        decoded = None
        amount = 0
        try:
            decoded = decode_cashu_token(record.token)
            amount = get_token_amount(decoded)
        except Exception:
            amount = 0

        result = ResultRecord(
            timestamp=record.timestamp,
            mint_url=record.mint_url,
            amount=amount,
        )

        if mode == "dry-run":
            result.action = ACTION_DRY_RUN
            results.append(result)
            continue

        # Check state at mint
        try:
            raw_state = check_token_state(record.mint_url, record.token)
            unspent = raw_state.get("unspent_count", 0)
            result.state = "UNSPENT" if unspent > 0 else "SPENT"
            result.unknown_count = raw_state.get("unknown_count", 0)
        except Exception as e:
            result.state = "ERROR"
            result.action = ACTION_CHECK_FAILED
            result.error = str(e)
            results.append(result)
            if delay and len(results) < len(records):
                time.sleep(delay)
            continue

        if mode == "check":
            result.action = ACTION_CHECK_ONLY
        elif mode == "recover":
            if result.state == "UNSPENT":
                try:
                    status, body = submit_token_to_router(router_ip, record.token)
                    result.submit_status = status
                    result.submit_body = body
                    if status >= 400:
                        result.action = ACTION_SUBMIT_FAILED
                        result.error = f"HTTP {status}: {body[:200]}"
                    else:
                        result.action = ACTION_SUBMITTED
                except Exception as e:
                    result.action = ACTION_SUBMIT_FAILED
                    result.error = str(e)
            else:
                result.action = ACTION_SKIPPED_SPENT

        results.append(result)
        if delay and len(results) < len(records):
            time.sleep(delay)

    return results


def summarize_results(results: list[ResultRecord]) -> dict:
    """Summarize processing results into counts."""
    summary = {
        "total": len(results),
        "unspent": 0,
        "spent": 0,
        "errors": 0,
        "submitted": 0,
        "skipped": 0,
        "unknown_proofs": 0,
    }
    for r in results:
        if r.state == "UNSPENT":
            summary["unspent"] += 1
        elif r.state == "SPENT":
            summary["spent"] += 1
        elif r.state == "ERROR":
            summary["errors"] += 1
        if r.action == ACTION_SUBMITTED:
            summary["submitted"] += 1
        elif r.action == ACTION_SKIPPED_SPENT:
            summary["skipped"] += 1
        summary["unknown_proofs"] += r.unknown_count
    return summary


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Recover stuck Cashu tokens from tokens-to-recover.txt"
    )
    parser.add_argument("--file", required=True, help="Path to tokens-to-recover.txt")
    parser.add_argument("--router", default="192.168.8.1", help="Router IP (default: 192.168.8.1)")
    parser.add_argument("--check", action="store_true", help="Check token states at mint only")
    parser.add_argument("--recover", action="store_true", help="Re-submit unspent tokens to router")
    parser.add_argument("--dry-run", action="store_true", help="Parse and show what would happen")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between tokens in seconds")
    args = parser.parse_args()

    mode = "dry-run" if args.dry_run else ("recover" if args.recover else ("check" if args.check else None))
    if mode is None:
        parser.print_help()
        sys.exit(1)

    records = parse_token_file(args.file)
    print(f"Found {len(records)} tokens to process\n")

    results = process_tokens(args.file, mode=mode, router_ip=args.router, delay=args.delay)

    for i, r in enumerate(results):
        print(f"[{i+1}/{len(results)}] {r.timestamp}")
        print(f"  Mint: {r.mint_url}")
        print(f"  Amount: {r.amount} sats")
        print(f"  State: {r.state or 'NOT_CHECKED'}")
        print(f"  Action: {r.action}")
        if r.unknown_count:
            print(f"  Unknown proofs: {r.unknown_count}")
        if r.submit_status is not None:
            print(f"  Submit: HTTP {r.submit_status}")
        if r.error:
            print(f"  Error: {r.error}")
        print()

    summary = summarize_results(results)
    print("=" * 50)
    print("SUMMARY")
    print(f"  Total tokens:  {summary['total']}")
    print(f"  Unspent:      {summary['unspent']}")
    print(f"  Spent (skip): {summary['spent']}")
    print(f"  Submitted:    {summary['submitted']}")
    print(f"  Skipped:      {summary['skipped']}")
    print(f"  Errors:       {summary['errors']}")
    if summary["unknown_proofs"]:
        print(f"  Unknown proofs (not UNSPENT/SPENT at mint): {summary['unknown_proofs']}")
    print("=" * 50)

    # Write detailed results
    results_path = Path(args.file).parent / "recovery-results.json"
    Path(results_path).write_text(json.dumps([asdict(r) for r in results], indent=2))
    print(f"Detailed results: {results_path}")


if __name__ == "__main__":
    main()
