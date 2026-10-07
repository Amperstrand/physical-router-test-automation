# The extensive test — runbook

One command, three maps, every path class. This is the entry point for
the extensive-test effort; the behavioral ground truth it should cover
lives in **`docs/user-journeys.md`** (realistic persona journeys ×
happy/failure/regression paths) and the generated
**`docs/user-stories.generated.md`** (story × test map —
`python3 scripts/story-coverage.py` to regenerate).

## Quick start

```bash
./scripts/extensive-test.sh                        # virtual venue, full tier sweep
STORIES="JG1-mapped G2,G4" ./scripts/extensive-test.sh   # scoped below
PATHS="failure" ./scripts/extensive-test.sh        # failure paths only
TOLLGATE_VENUE=physical TOLLGATE_SSH_HOST=192.168.1.1 \
  TOLLGATE_IPK=packaging/tollgate-wrt_vX_x86_64.ipk ./scripts/extensive-test.sh
```

Artifacts land in `results/extensive-<UTC stamp>/`: per-tier junit +
logs, plus copies of both maps. Exit code = worst tier.

## Venue readiness (checked 2026-10-07 — verify before relying)

| Venue | State | Bring-up |
|---|---|---|
| Virtual lab (ai-legion QEMU) | **Down** (0 QEMU) | script auto-starts via `start-poc` and **refuses politely if another lane is driving the lab** (omarchy/socat markers). Known instability = issue #21. |
| TollGate-326D (live MT3000) | **Dark** — no beacons; uplink key needs the `<hidden>`-mask fix before its STA can ever associate (see lane status docs); commissioned battery = issue #12 | owner power-cycle + key fix, then `TOLLGATE_VENUE=physical` |
| ALPHA/BRAVO (x1860 labgrid) | Free places, but DUT-bay VLANs 104/105.x are **unrouted** from the house LAN | conwrt-bench `bench_net.py` routing or bench-side access |
| NR7101 | Reachable at 192.168.12.124; labgrid place match broken; SSH creds unknown | fix place match + creds (owner) |
| Phone (android-test) | adb shows no devices on ai-legion-small | check rig cabling/phone power |

## Scoping knobs

- `TIERS` — pytest tier markers, default `smoke critical extended`
  (hierarchical: smoke ⊂ critical ⊂ extended).
- `STORIES` — story ids from `docs/story-catalog.yml`, comma/space list
  (`--story` is OR-within; `pytest --story G2 --tip TIP-02` is AND-across).
- `PATHS` — `happy`, `failure`, `regression` path classes (new tests tag
  them; legacy files are classified per-journey in `docs/user-journeys.md`).
- `TIPS` — TIP conformance items (`TIP-01..03` currently exercised).

## What "done" looks like for the extensive test

Per `docs/user-journeys.md`: every journey's happy row green against the
release-candidate build; every failure row green (bounded, loud, honest
failures — not passes, *correct failure behavior*); every regression row
green; and each **GAP** row either gains a test or an explicit
venue-blocked note. Known chases while doing it: **issue #10** (deployed
Go build never recovered from degraded when the mint returned — the
mint-returns assertion is mandatory against the rc build) and the G7
portal-copy UI remainder.
