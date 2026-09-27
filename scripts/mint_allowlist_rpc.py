#!/usr/bin/env python3
"""mint_allowlist_rpc.py — npub-gated issuance / mark-paid RPC for the test mint.

The lab mints free e-cash (fakewallet). This service deliberately exposes an
*authorisation bypass* path that issues / marks a quote paid **only** for a
whitelist of Nostr pubkeys. Callers must present a NIP-98 (kind 27235) signed
request whose pubkey is allowlisted and whose `u`/`method` tags match the call.

Endpoints
---------
GET  /health                      -> {"ok": true, "allow_npubs": N}
POST /allowlist   {npub}          -> {"allowed": bool}
POST /mark-paid   NIP-98 auth      body {"quote_id": "..."} ->
        200 {"issued": true, "npub": ..., "quote_id": ...} when allowlisted
        401 bad/missing proof, 403 pubkey not allowlisted
    When MINT_ADMIN_URL is set the service POSTs {"quote_id": ...} there to
    trigger the actual issuance; otherwise it returns the authorisation result
    (the lab fakewallet backend issues automatically).

Security
--------
* Fail-closed: if the signature cannot be verified (e.g. `nak` missing), reject.
* NIP-98 replay window enforced (created_at within [now-max_age, now+skew]).
* The allowlist lives in a JSON policy file, never in code.

Config JSON: {"allow_npubs": ["npub1..."], "max_age_s": 300, "future_skew_s": 60}
Env: MINT_ALLOWLIST_CONFIG, MINT_ALLOWLIST_PORT, MINT_ADMIN_URL, NAK_BIN
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_POLICY = {"allow_npubs": [], "max_age_s": 300, "future_skew_s": 60}
NIP98_KIND = 27235


# ── pure helpers (unit-tested) ──────────────────────────────────────────────
def load_policy(path: str | os.PathLike | None) -> dict:
    pol = dict(DEFAULT_POLICY)
    if path:
        try:
            pol.update(json.loads(Path(path).read_text()))
        except Exception:
            pass
    pol["allow_npubs"] = list(pol.get("allow_npubs", []))
    return pol


def pubkey_hex_to_npub(pk_hex: str) -> str:
    """Encode a 32-byte hex pubkey as bech32 npub (no external deps)."""
    CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

    def hrp_expand(hrp: str) -> list[int]:
        return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]

    def polymod(values: list[int]) -> int:
        gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
        chk = 1
        for v in values:
            top = chk >> 25
            chk = ((chk & 0x1FFFFFF) << 5) ^ v
            for i in range(5):
                chk ^= gen[i] if ((top >> i) & 1) else 0
        return chk

    data = [int(pk_hex[i:i + 2], 16) for i in range(0, 64, 2)]
    # 8-bit -> 5-bit
    bits = "".join(f"{b:08b}" for b in data)
    bits += "0" * ((5 - len(bits) % 5) % 5)
    five = [int(bits[i:i + 5], 2) for i in range(0, len(bits), 5)]
    hrp = "npub"
    values = hrp_expand(hrp) + five
    pm = polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    checksum = [(pm >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[d] for d in five + checksum)


def nip98_ok(event: dict, url: str, method: str, now: float,
             policy: dict, verify_sig) -> tuple[bool, str, str]:
    """Validate a NIP-98 event. Returns (ok, reason, pubkey_npub)."""
    if not isinstance(event, dict):
        return False, "event not an object", ""
    if event.get("kind") != NIP98_KIND:
        return False, f"kind {event.get('kind')} != {NIP98_KIND}", ""
    pk = event.get("pubkey", "")
    if not isinstance(pk, str) or len(pk) != 64:
        return False, "bad pubkey", ""
    npub = pubkey_hex_to_npub(pk)
    if npub not in policy["allow_npubs"]:
        return False, "pubkey not allowlisted", npub
    ts = event.get("created_at")
    if not isinstance(ts, (int, float)):
        return False, "missing created_at", npub
    if now - ts > float(policy["max_age_s"]):
        return False, "stale (replay window)", npub
    if ts - now > float(policy["future_skew_s"]):
        return False, "created_at in the future", npub
    tags = {t[0]: t[1] for t in event.get("tags", []) if t}
    if tags.get("u") not in (url, None) or tags.get("method", method) != method:
        return False, "u/method tag mismatch", npub
    if not verify_sig(event):
        return False, "bad signature", npub
    return True, "ok", npub


def verify_signature(event: dict, nak_bin: str = "nak") -> bool:
    """Verify a Nostr event signature via `nak verify` (fail-closed)."""
    try:
        r = subprocess.run([nak_bin, "verify"], input=json.dumps(event),
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


# ── HTTP service ────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "mint-allowlist/1"
    policy: dict = DEFAULT_POLICY
    admin_url: str = ""
    nak_bin: str = "nak"

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def _proof(self) -> dict | None:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Nostr "):
            return None
        try:
            return json.loads(base64.b64decode(auth.split(" ", 1)[1]))
        except Exception:
            return None

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._send(200, {"ok": True,
                             "allow_npubs": len(self.policy["allow_npubs"])})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path == "/allowlist":
            npub = self._read_json().get("npub", "")
            self._send(200, {"allowed": npub in self.policy["allow_npubs"]})
            return
        if self.path == "/mark-paid":
            event = self._proof()
            if not event:
                self._send(401, {"error": "missing NIP-98 proof"})
                return
            url = f"http://{self.headers.get('Host', 'localhost')}{self.path}"
            ok, why, npub = nip98_ok(event, url, "POST", time.time(),
                                     self.policy,
                                     lambda e: verify_signature(e, self.nak_bin))
            if not ok:
                self._send(403, {"error": why, "npub": npub})
                return
            quote = self._read_json().get("quote_id", "")
            issued = True
            if self.admin_url:
                issued = self._forward(quote)
            self._send(200, {"issued": issued, "npub": npub, "quote_id": quote})
            return
        self._send(404, {"error": "not found"})

    def _forward(self, quote: str) -> bool:
        try:
            r = subprocess.run(
                ["curl", "-fsS", "-X", "POST", self.admin_url,
                 "-H", "Content-Type: application/json",
                 "-d", json.dumps({"quote_id": quote})],
                capture_output=True, text=True, timeout=15)
            return r.returncode == 0
        except Exception:
            return False

    def log_message(self, *_a):  # quiet
        pass


def main() -> int:
    pol = load_policy(os.environ.get("MINT_ALLOWLIST_CONFIG"))
    Handler.policy = pol
    Handler.admin_url = os.environ.get("MINT_ADMIN_URL", "")
    Handler.nak_bin = os.environ.get("NAK_BIN", "nak")
    port = int(os.environ.get("MINT_ALLOWLIST_PORT", "8391"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"mint-allowlist-rpc on :{port} allow_npubs={len(pol['allow_npubs'])} "
          f"admin={'set' if Handler.admin_url else 'none'}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
