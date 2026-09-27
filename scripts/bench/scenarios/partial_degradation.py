#!/usr/bin/env python3
"""Partial mint degradation scenario (PRTA #142) — written on Bench.

Migrated from scripts/mint-zoo/partial-degradation.sh as the reference
pattern: scenarios use Bench for every rail operation, verdicts are loud,
and the contamination guards (deauth, pin, full-response) live in the
library where they cannot be forgotten.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench import Bench, PayResult, Verdicts  # noqa: E402

M1 = "http://10.99.99.2:33210"  # nutshell 0.21.0
M2 = "http://10.99.99.2:33381"  # cdk 0.18.1
M3 = "http://10.99.99.2:33376"  # cdk 0.17.6 (minibits-class)
P3, P2 = 33376, 33381


def wait_ad(predicate, bench: Bench, v: Verdicts, what: str, timeout: int = 420) -> set[str] | None:
    deadline = time.monotonic() + timeout
    last: set[str] | None = None
    while time.monotonic() < deadline:
        try:
            last = bench.ad_mints()
            if predicate(last):
                return last
        except Exception:
            pass  # transient unparseable ad during merchant rebuild
        time.sleep(10)
    v.bad(f"{what}: advertisement never reached expected state (last={last})")
    return None


def main() -> int:
    out = Path.home() / "upgrade-test" / f"bench-partial-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    out.mkdir(parents=True, exist_ok=True)
    v = Verdicts(out / "run.log")
    b = Bench()

    with b.lock("partial-degradation"):
        v.ok(f"bench lock acquired (router={b.router})")
        cfg0 = b.config_sha()

        b.pin_mints([M1, M2, M3])
        base = b.ad_mints()
        if base == {M1, M2, M3}:
            v.ok(f"baseline advertisement carries all 3 mints: {sorted(base)}")
        else:
            v.bad(f"baseline advertisement: {sorted(base)}")

        # phase 1: block one mint
        b.block_mint(P3)
        now = wait_ad(lambda m: M3 not in m and M1 in m and M2 in m, b, v,
                      "block cdk-0176 -> ad drops exactly that mint")
        if now is not None:
            v.ok(f"blocked mint dropped, healthy two remain: {sorted(now)}")

        r = b.pay(M1)
        if r.ok:
            v.ok("payment via healthy mint during partial outage (kind:1022)")
        else:
            v.bad(f"healthy-mint payment during outage: kind={r.kind} code={r.code} content={r.content[:120]}")

        r = b.pay(M3)
        if r.kind == 21023:
            v.ok("blocked-mint payment fails gracefully (kind:21023)")
        else:
            v.bad(f"blocked-mint payment: kind={r.kind} code={r.code}")

        b.unblock_mint(P3)
        rec = wait_ad(lambda m: m == {M1, M2, M3}, b, v, "recovery -> ad restores all 3")
        if rec is not None:
            v.ok("advertisement restored all 3 mints after recovery")

        # phase 2: payment-triggered degradation (tmbg #401 surface)
        b.block_mint(P2)
        r = b.pay(M2)
        if r.kind == 21023:
            v.ok("dead-mint payment rejected (kind:21023)")
        else:
            v.bad(f"dead-mint payment: kind={r.kind}")
        r = b.pay(M1)
        if r.ok:
            v.ok("healthy-mint payment survives payment-path degradation trigger")
        else:
            v.bad(f"payment-path trigger poisoned healthy mint: kind={r.kind} code={r.code}")
        b.unblock_mint(P2)

        if b.config_sha() == cfg0:
            v.ok("config.json unchanged across all scenarios")
        else:
            v.bad(f"CONFIG CHURN: {cfg0} -> {b.config_sha()}")

    return v.summary()


if __name__ == "__main__":
    raise SystemExit(main())
