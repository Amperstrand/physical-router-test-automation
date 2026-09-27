#!/usr/bin/env python3
"""Bench — the single payment/relay rail for QEMU bench scenarios.

Everything a scenario needs to drive the upgrade-bench router: SSH control,
mint pinning, payments (with the contamination guards baked in), advertisement
parsing, mint block/unblock, health probing, and a host lock. Shell runners
must NOT hand-copy this logic (2026-09-19 lesson: four copies, bugs fixed in
one persisted in the others; two false product-broken verdicts).

Runs on the bench host (ai-legion-small) with the pyenv that has coincurve
(HttpMinter). Shaped as the labgrid seam: a future LabgridBench subclass
swaps SSH/QEMU control for labgrid places; scenarios never touch raw ssh/curl.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

ROUTER = os.environ.get("BENCH_ROUTER", "10.99.99.1")
HOST_IP = os.environ.get("BENCH_HOST_IP", "10.99.99.2")
VMPW = os.environ.get("VMPW", "Upgr4deTest-2026")
PRTA_LIB = os.environ.get("BENCH_PRTA_LIB", str(Path.home() / "upgrade-test/prta"))
LOCK_PATH = Path(os.environ.get("BENCH_LOCK", str(Path.home() / "upgrade-test/bench.lock")))


class BenchError(RuntimeError):
    """Rail-level failure — raised loudly, never a silent sentinel."""


@dataclass
class PayResult:
    kind: int = 0
    code: str = ""
    content: str = ""
    raw: str = ""
    ok: bool = False

    def assert_paid(self, ctx: str = "") -> None:
        if not self.ok:
            raise BenchError(
                f"{ctx}: payment rejected (kind={self.kind} code={self.code!r} "
                f"content={self.content[:200]!r})"
            )

    def assert_rejected(self, ctx: str = "") -> None:
        if self.kind != 21023:
            raise BenchError(f"{ctx}: expected graceful rejection (21023), got kind={self.kind}")


@dataclass
class Verdicts:
    path: Path
    passes: list[str] = field(default_factory=list)
    fails: list[str] = field(default_factory=list)

    def ok(self, msg: str) -> None:
        self.passes.append(msg)
        self._log("PASS", msg)

    def bad(self, msg: str) -> None:
        self.fails.append(msg)
        self._log("FAIL", msg)

    def _log(self, tag: str, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S', time.gmtime())}] {tag}: {msg}"
        print(line, flush=True)
        with self.path.open("a") as f:
            f.write(line + "\n")

    def summary(self) -> int:
        print(f"\npasses={len(self.passes)} failures={len(self.fails)} -> {self.path}")
        return 1 if self.fails else 0


class Bench:
    def __init__(self, router: str = ROUTER, host_ip: str = HOST_IP, password: str = VMPW) -> None:
        self.router = router
        self.host_ip = host_ip
        self.password = password

    # -- router control -------------------------------------------------

    def ssh(self, cmd: str, timeout: int = 30) -> str:
        proc = subprocess.run(
            ["sshpass", "-p", self.password, "ssh",
             "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             f"root@{self.router}", cmd],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if proc.returncode != 0:
            raise BenchError(f"router ssh rc={proc.returncode}: {proc.stderr.strip()[:200]}")
        return proc.stdout

    def config_sha(self) -> str:
        out = self.ssh("sha256sum /etc/tollgate/config.json")
        return out.split()[0]

    def pin_mint(self, mint_url: str, price_per_step: int = 2, wipe_wallet: bool = True) -> None:
        entry = json.dumps({
            "url": mint_url, "min_balance": 0, "balance_tolerance_percent": 0,
            "payout_interval_seconds": 999999, "min_payout_amount": 999999,
            "price_per_step": price_per_step, "price_unit": "sats",
            "min_purchase_steps": 1,
        })
        wipe = "rm -f /etc/tollgate/wallet.db && " if wipe_wallet else ""
        self.ssh(
            f"jq '.accepted_mints = [{shlex.quote(entry)}]' /etc/tollgate/config.json"
            f" > /tmp/c && cp /tmp/c /etc/tollgate/config.json && {wipe}"
            "/etc/init.d/tollgate-wrt restart"
        )
        self.wait_backend()

    def pin_mints(self, mint_urls: list[str], price_per_step: int = 2) -> None:
        entries = ",".join(json.dumps({
            "url": u, "min_balance": 0, "balance_tolerance_percent": 0,
            "payout_interval_seconds": 999999, "min_payout_amount": 999999,
            "price_per_step": price_per_step, "price_unit": "sats",
            "min_purchase_steps": 1,
        }) for u in mint_urls)
        self.ssh(
            f"jq '.accepted_mints = [{entries}]' /etc/tollgate/config.json"
            " > /tmp/c && cp /tmp/c /etc/tollgate/config.json &&"
            " rm -f /etc/tollgate/wallet.db && /etc/init.d/tollgate-wrt restart"
        )
        self.wait_backend()

    def wait_backend(self, timeout: int = 90) -> None:
        """Functional probe: the backend must serve a kind-bearing response —
        ':2121 accepting connections' is not readiness."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                body = self.http_get_backend("/")
                if '"kind"' in body:
                    return
            except Exception:
                pass
            time.sleep(3)
        raise BenchError(f"backend at {self.router}:2121 not answering with kind within {timeout}s")

    def http_get_backend(self, path: str, timeout: int = 8) -> str:
        with urllib.request.urlopen(f"http://{self.router}:2121{path}", timeout=timeout) as r:
            return r.read().decode(errors="replace")

    def ad_mints(self) -> set[str]:
        """Mint URLs from the kind:10021 advertisement. RAISES on garbage —
        an unparseable or mint-less ad is a rail failure, never a silent ''."""
        body = self.http_get_backend("/")
        try:
            d = json.loads(body)
        except json.JSONDecodeError as e:
            raise BenchError(f"advertisement not JSON: {body[:120]!r}") from e
        if d.get("kind") != 10021:
            raise BenchError(f"expected kind:10021, got {d.get('kind')}: {body[:120]!r}")
        mints = set()
        for t in d.get("tags", []):
            # tag shape: [price_per_step, "cashu", N, unit, mint_url, min_steps]
            if t[0] == "price_per_step" and len(t) >= 5 and t[4].startswith("http"):
                mints.add(t[4])
        if not mints:
            raise BenchError(f"advertisement carries no mints: {body[:160]!r}")
        return mints

    def block_mint(self, port: int) -> None:
        self.ssh(f"iptables -I OUTPUT -d {self.host_ip} -p tcp --dport {port} -j DROP")

    def unblock_mint(self, port: int) -> None:
        self.ssh(f"iptables -D OUTPUT -d {self.host_ip} -p tcp --dport {port} -j DROP")

    def log_count(self, pattern: str) -> int:
        out = self.ssh(f"logread | grep -c {shlex.quote(pattern)}")
        try:
            return int(out.strip() or "0")
        except ValueError:
            raise BenchError(f"log count garbage for {pattern!r}: {out!r}") from None

    # -- payments ---------------------------------------------------------

    def mint_token(self, mint_url: str, amount: int = 4) -> str:
        """V3 token via PRTA's HttpMinter (no cdk-cli wallet-state flakiness)."""
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from lib.cashu import HttpMinter\n"
            "print(HttpMinter(sys.argv[1]).mint(int(sys.argv[2])))"
            % (PRTA_LIB,)
        )
        proc = subprocess.run(
            [os.path.expanduser("~/upgrade-test/pyenv/bin/python"), "-c", code,
             mint_url, str(amount)],
            capture_output=True, text=True, timeout=90, check=False,
        )
        token = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        if not token.startswith("cashuA"):
            raise BenchError(
                f"token mint failed for {mint_url}: stdout={proc.stdout[:120]!r} "
                f"stderr={proc.stderr.strip()[:200]!r}"
            )
        return token

    def pay(self, mint_url: str, amount: int = 4, token: str | None = None) -> PayResult:
        """Clean-room payment: deauth the client, prime the portal, POST the
        raw-body token, parse the FULL response. Contamination guards are not
        optional callers' business — they are in here."""
        if token is None:
            token = self.mint_token(mint_url, amount)
        self.ssh(f"ndsctl deauth {self.host_ip} 2>/dev/null; true")
        # prime NDS client table (MAC must be tracked before gate-open)
        with contextlib.suppress(Exception):
            urllib.request.urlopen(f"http://{self.router}:2050/", timeout=8).read(1)
        time.sleep(1)
        req = urllib.request.Request(
            f"http://{self.router}:2121/", data=token.encode(),
            headers={"Content-Type": "text/plain"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                body = r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
        return self._parse_pay(body)

    @staticmethod
    def _parse_pay(body: str) -> PayResult:
        res = PayResult(raw=body)
        try:
            d = json.loads(body)
        except json.JSONDecodeError:
            raise BenchError(f"payment response not JSON: {body[:200]!r}") from None
        res.kind = d.get("kind") or 0
        for t in d.get("tags", []):
            if t[0] == "code" and len(t) > 1:
                res.code = t[1]
        res.content = d.get("content", "")
        res.ok = res.kind == 1022
        return res

    # -- mint health ------------------------------------------------------

    @staticmethod
    def settle_probe(mint_url: str, timeout: int = 12) -> bool:
        """A mint answering /v1/info can still be dead: settle a real quote."""
        try:
            req = urllib.request.Request(
                f"{mint_url}/v1/mint/quote/bolt11",
                data=json.dumps({"unit": "sat", "amount": 1}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                qid = json.load(r)["quote"]
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                with urllib.request.urlopen(f"{mint_url}/v1/mint/quote/bolt11/{qid}", timeout=5) as r:
                    if json.load(r).get("state") == "PAID":
                        return True
                time.sleep(1)
        except Exception:
            pass
        return False

    # -- host lock ---------------------------------------------------------

    @contextlib.contextmanager
    def lock(self, note: str = "") -> Iterator[None]:
        """Advisory host lock so two sessions never drive the same bench."""
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOCK_PATH.open("w") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            f.write(f"{os.environ.get('USER', '?')} {note} {time.strftime('%FT%TZ', time.gmtime())}\n")
            f.flush()
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def verdicts_dir(base: str = "partial-degr") -> Path:
    d = Path.home() / "upgrade-test" / f"{base}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    d.mkdir(parents=True, exist_ok=True)
    return d
