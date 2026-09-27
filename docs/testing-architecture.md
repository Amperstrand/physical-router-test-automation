# PRTA Testing Architecture — venues, tiers, mints, evidence

Status: 2026-09-19. Written during the v0.6.0 release campaign (#113); reflects
the GCP-venue revival, the QEMU upgrade bench, and the mint-zoo matrix.

## The system under test

`tollgate-wrt` (Go backend, OpenWrt package) + nodogsplash captive portal +
Cashu mints. Under test: package install/upgrade/rollback, payment E2E
(Cashu token → session → gate-open), mint-health state machine (full/degraded),
config/wallet persistence, and the release channel (Nostr/Blossom).

## Venues (where tests run)

| Venue | Host | Cost | Publishes | Use |
|---|---|---|---|---|
| Physical router | GL-MT3000/GL-MT6000 on LAN | free | no | release matrix (#106), feed RC verify (#116) |
| QEMU upgrade bench | ai-legion-small (`scripts/upgrade-emulation/`) | free | no | install/upgrade/rollback matrix, soak rail, **mint-zoo matrix** |
| Local virtual lab | ai-legion / Mac (`scripts/virtual-lab.py`) | free | no | fast iteration |
| GCP cloud lab | n2-standard-2, snapshot `tollgate-runner-v20` | **~$0.28/run full, ~$0.05 quick**, 2.5h full | **yes** (Nostr+Blossom, kind 30078) | full-suite evidence, PR runs |
| SHC | Dev zone — **dead since 2026-08-27** (platform provisioning wedge, 2026-09-19) | $0.01/run when alive | yes | (dormant) |

Reap guarantees (both cloud venues): worker self-delete + lease kill switch +
host sweeper cron (15 min, 2h threshold). GCP full runs need `--lease >= 150`;
suite timeout is 7200s (`lib/cloud_lab/worker/runner.py`).

## Test tiers and runners

- pytest API tier (`tests/api/`, ~50 files): no phone needed; gating via
  feature detection, `gate_bug_fix()` xfail regression gates, or unconditional.
- Phone tier (ADB), LuCI Playwright, protocol/destructive Playwright.
- Cloud worker runners: `visual` (gate) → `api` + `nut18` (parallel) →
  `vl-scenarios` (destructive, sequential — blocks mints, must run last).
- QEMU bench runners: `run-matrix.sh` (upgrade lifecycle),
  `soak-rail.sh` (payment soak), `mint-zoo/matrix.sh` (interop).

## Mints — the zoo

Three layers:

1. **Cloud worker mints** (in-VM, ephemeral): CDK V2 :8383, Nutshell V2 :8384,
   Nutshell V1 :8385 (`lib/cloud_lab/worker/mints.py`). Used by the api suite.
2. **Mint zoo** (docker on the bench host, `scripts/mint-zoo/up.sh`):
   nutshell 0.16.5 / 0.18.2 / 0.19.1 / 0.20.0 / 0.20.3 + cdk-mintd
   0.17.0 / 0.17.6 / 0.18.0, all FakeWallet, bound `10.99.99.2:33xxx`
   (bridge-reachable from the bench router). Mirrors the ai-legion
   mint-battery (#134). Version-dialect notes: cdk 0.17 = env-driven config;
   cdk 0.18 = DB-held config (`config init --new-mint`, `env:` secret refs);
   nutshell containers need the explicit `poetry run mint` command.
3. **Public testnut** (`.space` canonical, `.exchange` fallback; both
   live-probed 2026-09-18). Flaky — always settle-probe before blaming the
   router.

Keyset classes in the zoo: nutshell ≤0.19.x = **V1** (`00…`), nutshell
0.20.x and all cdk = **V2** (`01…`).

## The mint matrix (`scripts/mint-zoo/matrix.sh`)

Per mint × backend build: health + NUT-04 settle probe, keyset class, payment
E2E (HttpMinter token, raw-body POST, kind:1022), outage resilience (block
mint → degraded tick → unblock → recovery; targets tmbg #400/#401), and
config-churn check across the outage cycle (targets #402).

First clean matrix (2026-09-19, v0.6.0-alpha2 @373770a, per-payment deauth):
payments succeed across **all focused mints** — nutshell 0.21.0 / 0.20.3 and
cdk 0.18.1 / 0.18.0 (all V2-keyset), plus V1-keyset nutshell 0.16.5 in the
retired history fleet, plus minibits-class cdk 0.17.6.

**Token-format verification (2026-09-19, clean-room: router pinned to the
token's mint + per-payment deauth + full-response capture): V4 (cashuB)
tokens pay on v0.6.0-alpha2 AND main (a6e01bd) against V2-keyset nutshell
0.21.0 AND cdk 0.17.6 — kind:1022 in all four cells.** V4+V2 is VERIFIED
WORKING (the AGENTS "E2E pending" caveat is resolved). Two earlier
"V4 broken" readings this session were test-rail contamination (NDS
already-authenticated race, then a stale mint pin) — the clean-room
discipline is now baked into the matrix and must stay. Degrade timing is textbook (~310s = one 5-min probe
tick + probe timeout); no config churn across outage cycles. An earlier
"V2-keyset rejection" read was NDS-race contamination (see below). The
recovery-timing metric needs the post-degrade-baseline pattern (the recovery
log line "Reachable mint set changed" fires on both transitions — the same
patterns made the 24h soak's flap rows report recover=FAIL spuriously).

## Evidence pipeline

Canonical run dirs under `results/` (local) → cloud runs publish manifests
(Blossom blobs + Nostr kind 30078 events; ~500 files for a full run).
Requirements learned the hard way: runners must COMPLETE (junit is written at
pytest exit), `collect` runs after all runners, and the summary must carry
failure messages. The suite auto-captures failure-debug bundles (ndsctl state,
sessions, config) into the manifest.

## Known reliability map (what fails and why)

- NDS 5.0.2 auth-mark bug: authenticated clients can't open NEW connections
  (workaround: mangle-rule fix per AGENTS; `ndsctl auth` on already-auth
  MACs exits 1 — always deauth before payment tests).
- Degraded-mode wedge class (tmbg #400/#401): mint-blocking tests can leave
  the backend sick and poison later payment tests (suite-level liveness heal
  shipped for test_local_payment; generalize when needed).
- Lightning-portal melts with fakewallet 0.18 (fees WARN, settle still lands).
- Balance-page/uhttpd gaps on fresh VMs (environment, not product).
- preinst/jq upgrade blocker: fixed upstream (tmbg PR #407); regression gate
  lives in the bench `run-matrix.sh` phase 5.

## Open work

PRTA #134 (wire the zoo into CI lanes + CLN rail), #136 (ipk format
normalization), #138 (conftest pytest.exit softening); tmbg #400-#403
(mint-health + funds-safety reliability bugs the matrix now hunts).
