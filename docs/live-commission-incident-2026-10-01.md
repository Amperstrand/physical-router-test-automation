# Live-commission read-only violation — postmortem & forward plan (2026-10-01)

Incident during the m5stick-rig lane's commissioned runbook execution against
the live MT3000 (`tollgate-326D` @ 192.168.1.1, Go backend v0.6.0-alpha4-gafc86b3).
Full run evidence: `/tmp/opencode/prta-live-results.md` (relayed to ai-legion),
pytest log `/tmp/prta-live.log`. This document is the durable version: what
happened, why, what it exposed, and the fix/test plan.

## 1. What happened

Commission: 4 "read-only" pytest suites + 4 probe blocks, strictly no NDS
deauth, no payment/e2e, no WiFi/interface changes. Result:

- **Tests: 8 passed / 18 skipped / 1 deselected / 0 failed** (23.5s).
- **Incident: the framework itself mutated the live router.** The session-scoped
  autouse `deploy_session` fixture (`tests/conftest.py:539-556`) ran
  `enable_debug_portal()` → `ensure_test_mint()` → `replace_mints()`, which
  **replaced the live 8-mint zoo `accepted_mints` with
  `[https://testnut.cashu.exchange]`** and restarted the backend **twice**.
- Recovery (same session): pre-run state reconstructed from `logread` boot
  lines + `/etc/tollgate/config.json.m5-backup` per-mint settings, restored,
  backend restarted, verified (`kind:10021` ad again lists all 8 zoo mints;
  config md5 byte-identical to the restore file; `wallet.db` never touched;
  `debug-portal` flag absent). Pre-restore config archived on-router at
  `/etc/tollgate/config_backups/config-testnut-state-20261001-pre-restore.json`.

Timeline (router UTC):

| Time | Event |
|---|---|
| ≤06:34 | Backend pid 3723 running live zoo config; probes show `8333.space` failing (`invalid-or-empty-keysets`) |
| 06:35:24 | fixture `ensure_test_mint()` → append testnut + restart → pid 7445: Accepted mints = zoo-8 + testnut (9) |
| 06:35:31 | fixture `replace_mints()` → **accepted_mints := [testnut]** + restart → pid 7607 |
| 06:35–08:5x | 26 tests run against the mutated state |
| post-run | Restore pushed (zoo-8 mints), restart → pid 7939, discovery `kind:10021` with 8 mints, md5 verified |

## 2. Why it happened (root cause anatomy)

### 2.1 One flag guards three concerns

`deploy_session` conflates:

1. **Deploy** a backend from `--binary`/`--tollgate-branch`/`--tollgate-run-id`
2. **Health-check** the SUT (`:2121` must answer 200)
3. **Prepare live state**: `enable_debug_portal()` + `ensure_test_mint()` +
   `replace_mints()` — i.e. pin the router to the framework's test mint

The early-return guard (line 507) is `no_deploy or (_no_deploy_source and
_unit_only)`. A run of `tests/api/*.py` with **zero deploy flags** still falls
through to the unconditional block at 539 (`if not no_deploy:` → state prep).
The only suppressor is `--no-deploy` — a flag *named after concern (1)*. The
runbook author (reasonably) read AGENTS.md's description — "If --tollgate-branch
or --binary is specified, the fixture deploys before tests run" — and concluded
"no deploy flags → no side effects". The documentation described the deploy
sub-block, not the state-prep block.

### 2.2 `replace_mints()` has a destructive default

`lib/router.py:834` — `replace_mints(mint_urls=None)` defaults to
`[TEST_MINT_URL]`: any caller invoking it bare **replaces the entire accepted
mint list with one test mint**. In disposable venues (virtual lab, cloud lab,
SHC) this is exactly what you want. Against a live router it silently discards
the operator's mint configuration.

### 2.3 The mutation is a feature in test venues — that's why nobody caught it

Every PRTA venue except "physical live commission" wants mint pinning: tests
need a known fakewallet mint. The framework has no concept of "read-only
against state I don't own". The shell lane already solved this problem —
`scripts/hw-readonly-check.sh` is a fail-closed non-destructive bench check
whose G0 gate refuses to run if mutating flags are enabled — but the pytest
tier never got the equivalent.

### 2.4 Detection was accidental

The mutation was visible only because `-v` live-log setup lines printed
`"Replaced accepted mints with: https://testnut.cashu.exchange"` into the tee'd
output. Nothing diffed router state before/after. Recovery was possible only
because (a) `logread` retained boot lines enumerating accepted mints, (b) the
m5 lane had left a config backup, and (c) both mutating methods do
read-modify-write of the whole config (only `accepted_mints` changed — proven
by diffing against the backup). That's a lucky chain, not a safety mechanism.

## 3. Impact

- **Live deployment window (~2h)** during which the router accepted only
  testnut tokens. Any real client paying a zoo-mint token in that window would
  have been rejected (`token for mint X is not accepted` class).
- Two unscheduled backend restarts. Sessions persist via `sessions.json` +
  NDS marks (Go backend), and our probes showed no session for the laptop's
  MAC — but the protected client's experience during the restarts was not
  verified (unknown, owned by the live lane).
- **Results caveat**: the 4 discovery-mint test passes validated the testnut
  window, not the zoo config (structure assertions still meaningful; the zoo
  ad itself was verified post-restore by probe, not by pytest).

## 4. Findings inventory (beyond the incident)

1. **67% skip rate, and the skips hide dead coverage.** 14 of 18 skips are
   net4sats/configwizzard layout tests + 1 SPA-in-htdocs test: PRTA #103 moved
   the portal SPA to `/etc/tollgate/tollgate-captive-portal-site` on uhttpd
   `:2051`, and these tests still assert the OLD layout (`/www/net4sats/`,
   `:8090`). They can never pass on current packaging — **the current portal
   has zero passing pytest coverage** while the dashboard shows "0 failed".
2. **`:2051` answers 403 on bare `GET /`** — unknown whether intended (no
   index? vhost gating? uhttpd config?). Nothing in the repo pins this
   contract.
3. **`session-state` contract unpinned**: query with a valid MAC returns
   `{"status":1,"mac":"","state":"none"}` — empty MAC echo, undocumented
   `status:1`. `config/behavior-contract.json` doesn't cover it.
4. **`/usage` sentinel `-1/-1`** semantics unpinned (same contract gap).
5. **Running-state vs on-disk config drift**: pid 3723 probed `8333.space`
   while on-disk config (loaded by the next boot) lacked it — either the
   backend was started before a config edit and never restarted, or wallet.db
   registration outlives config removal. Which is truth? Unresolved.
6. **Skip reasons are terminal-mangled in tee'd `-v` logs** — commissioned
   runs need `-rs` or `--json-report` for machine-readable reasons.
7. **Coordinator ambiguity**: AGENTS.md says labgrid coordinator
   `192.168.13.208:20408` (ai-legion); `docs/labgrid-rig-exporter-adoption.md`
   says shared coordinator `192.168.13.221:20408` (ai-legion-small). Two
   coordinators or a stale doc — reconcile before adding places.

## 5. Fix queue (with acceptance criteria)

| # | Fix | Where | Acceptance criteria | Status (2026-10-01 PM) |
|---|-----|-------|---------------------|------------------------|
| F1 | `--read-only` flag, fail-closed | `tests/conftest.py` + `lib/router.py` | With flag: `deploy_session` skips state prep AND every `Router` mutator raises `ReadOnlyViolation` | ✅ implemented — `@mutator` decorator guards 27 methods; 27 parametrized unit tests + truth table |
| F2 | Commission-aware default | conftest | `TOLLGATE_LIVE_COMMISSION=1` without `TOLLGATE_ALLOW_STATE_MUTATION=1` implies read-only. (Amended during implementation: `TOLLGATE_LAB_TYPE=physical` would have implied read-only for every BENCH hardware suite too — the bench legitimately mutates its own routers. The discriminator is the commission env, not the lab type.) | ✅ implemented + truth-table tested |
| F3 | Kill `replace_mints` destructive default | `lib/router.py` | `mint_urls` required; loud refusal when shrinking >1→1 without `force=True` | ✅ implemented; conftest passes `force=True` explicitly; cloud-worker callers already explicit |
| F4 | `scripts/live-snapshot.py` snapshot/diff/restore | new script | `snapshot` captures config.json, uci, ndsctl status, ruleset, md5 of wallet.db/sessions.json; `diff` exits 1 only on strict config drift (volatile counters are notes); `restore` reverts config.json with md5 verify | ✅ implemented + 7 unit tests; live run pending router-LAN replug |
| F5 | State provenance in results | conftest + pytest-metadata | Session writes `state-manifest.json` (config hash, accepted_mints, backend version) into results dir | ⬜ queued |
| F6 | Machine-readable commissioned reports | runner wrapper | `--json-report` + `-rs`; per-test table rendered mechanically | ⬜ queued |
| F7 | Portal tests for the PRTA #103 layout | `tests/api/test_portal_verify.py`, `test_balance_page_reachable.py` | New `_portal_layout(router)` detection; tests assert `:2051` serves the SPA; net4sats variants keep their clean skips | ⬜ queued |
| F8 | Pin `session-state`, `/usage`, `:2051`-root contracts | `config/behavior-contract.json` + story test | Contract entries + story assertions; `:2051` root behavior decided and pinned | ⬜ queued |
| F9 | Labgrid exporter on the laptop | new (see §7) | `tollgate-mt3000-326d` place acquirable; commissioned run acquires → runs read-only suite → releases; snapshot diff clean | ⬜ queued |
| F10 | Fix `deploy_session` doc + AGENTS.md | docs | Docs state explicitly: state prep runs on every non-`--no-deploy` run; live commissions use `--read-only` | ✅ done (fixtures table + auto-deploy paragraph + lesson updated) |

**Implementation notes (2026-10-01 PM):** the runtime `Router` resolved by
pytest from this checkout is the LOCAL class in `lib/router.py` (verified via
`inspect.getmodule` — the `tollgate_lab.drivers.router` re-export fails in
that import order). The vendored site-packages `tollgate_lab` copy carries the
same unguarded `replace_mints`/`ensure_test_mint` — the guard must be
mirrored there on its next sync. Full unit suite: 902 passed /
2 failed — both failures pre-exist on HEAD
(`test_selfdestruct_wiring.py`, verified via `git stash` A/B).

**Live validation blocked (not abandoned):** the laptop's USB-LAN dongle
(r8152, `enp0s20f0u4`) was physically unplugged at 11:55:49 local per
`journalctl -k` — mid-gap between the incident session (08:35–08:52) and
implementation. The router was last seen in its verified restored state
(`kind:10021`, 8 zoo mints, config md5 match, backend pid 7939). When the
dongle is replugged, run exactly this to validate F1+F4 end-to-end:

```bash
source ~/.tollgate-test-venv/bin/activate
export TOLLGATE_SSH_HOST=192.168.1.1 TOLLGATE_SSH_KEY=~/.ssh/id_ed25519 TOLLGATE_CLIENT_IP=192.168.1.127
python3 scripts/live-snapshot.py snapshot --out /tmp/live-snap-base
pytest tests/api/test_discovery_mints.py tests/api/test_portal_verify.py \
       tests/api/test_captive_api.py tests/api/test_balance_page_reachable.py \
       --backend go -v --tb=short --read-only 2>&1 | tee /tmp/prta-live-readonly.log
python3 scripts/live-snapshot.py diff --base /tmp/live-snap-base   # strict must be CLEAN
```

Expected: state prep skipped ("READ-ONLY commission: skipping live-state
prep"), `test_bad_mint_handled_gracefully` FAILS fast with
`ReadOnlyViolation` (proof the guard fires — it is a mutating test),
everything else 8 passed / 17 skipped as before, and the diff's strict tier
CLEAN (only volatile counter notes). Commissioned tables should still
`--deselect` the bad-mint test; letting it run is a one-time guard proof.

## 6. How to test the fixes (venue matrix)

| Change | Unit | Virtual lab (QEMU) | Labgrid physical | Live commission |
|---|---|---|---|---|
| F1 read-only guard | mock Router; assert mutators raise with flag | run 4 commissioned files with `--read-only`; `config.json` md5 unchanged before/after; without flag, virtual lab still pins mints (no regression) | same assertion on a rig place | the v2 runbook re-run: snapshot-diff must be clean |
| F3 replace_mints default | direct unit test of the guard | config-mutation tests still pass where mutation is allowed | — | — |
| F4 live-snapshot | parse/diff logic on fixture files | snapshot → mutate (in lab) → diff catches → restore reverts | snapshot → restart backend (allowed op) → diff flags only expected drift | wrap every future commission |
| F7 portal layout | layout-detection against fixture strings | QEMU deployed with tollgate-site layout: new tests pass | MT3000 place: same tests pass against live | read-only |
| F8 contracts | contract-schema validation (exists) | story tests on VM | story tests on place/router | read-only probes only |

Sequencing: F1+F3+F10 first (small, unblock safe re-commissions), F4 next
(audit rail), F6/F5 with the next commission, F7/F8 as the coverage arc, F9
as the infra arc.

## 7. Labgrid integration plan (F9)

Why labgrid for this: the live MT3000 is reachable only from the dual-homed
laptop; today that means hand-relayed runbooks with no exclusivity lock, no
state audit, no place-based attribution. Labgrid gives acquire/release
(exclusivity against other lanes), an exporter story that matches the existing
rig-adoption sketch, and pytest-labgrid (already in the venv) for place-driven
fixtures.

Steps:

1. **Reconcile coordinators first** (finding 7): confirm whether
   `192.168.13.208:20408` (ai-legion) and `192.168.13.221:20408`
   (ai-legion-small) are both live; decide which owns physical-router places;
   update AGENTS.md + the rig-adoption doc. Run
   `$CONWRT_BENCH/scripts/lab_registry.py reconcile` per the lab-hardware
   rules.
2. **Exporter on the laptop** (it owns the route to 192.168.1.x):
   ```yaml
   # exporter-laptop-mt3000.yaml
   coordinator: "<chosen-coordinator>:20408"
   hostname: laptop-mt3000-rig
   resources:
     tollgate-mt3000-326d:
       NetworkService:
         address: "192.168.1.1"
         username: "root"
   # optionally a second place exporting the laptop's enp0s20f0u4 as the
   # client NIC (NetworkService on 192.168.1.127) so client-side probes
   # (:80/:2050/:2051) are also place-driven
   ```
  SSH via `NetworkService` + key auth (`TOLLGATE_SSH_KEY` already works);
   no power resource exists for this router (no smart PDU on the laptop LAN)
   — record that in the place notes; power ops stay manual.
3. **Register in conwrt-lab `lab.yaml`** (single-source-of-truth rule): device
   entry for tollgate-326D (MAC, IP, venue "laptop-LAN live"), the exporter
   hostname, and the place name. Commit per rule 4 of the hardware protocol.
4. **Commissioned-run flow v2** (replaces hand-runbooks):
   ```
   labgrid-client -p tollgate-mt3000-326d acquire
   scripts/live-snapshot.py snapshot
   pytest <commissioned files> --backend go --read-only --json-report ...
   scripts/live-snapshot.py diff        # exit 1 on drift → restore + page
   labgrid-client -p tollgate-mt3000-326d release
   ```
5. **Destructive/queued-fix testing moves to disposable rigs**, never the live
   box — see §8.

Known constraint: two lanes must not drive the exporter host concurrently
(the laptop also drives client-side probes); the place acquire covers the
router, and the client-NIC place (step 2) covers the laptop side.

## 8. Known unknowns → labgrid burn-down routes

| Unknown | Where it can be answered | How |
|---|---|---|
| `:2051` bare `/` → 403 intended? | any rig (or read-only live probe) | enumerate uhttpd config for the 2051 instance; decide contract; F8 pins it; Playwright portal spec (`tests/browser/tollgate-portal-lightning.spec.mjs` pattern) verifies SPA actually loads via its real entry path |
| `session-state` / `/usage` contract | virtual lab + rig | fuzz the endpoints (no MAC, unknown MAC, during session) on fakewallet rig; pin JSON shapes in behavior-contract.json; story test asserts |
| Backend restart vs live paid session | rig with fakewallet mint | pay (free token) → `restart_backend` ×3 → assert session survives (`sessions.json` reload + NDS marks intact + client internet works). This is the experiment the incident made us skip on live |
| Running-config vs on-disk drift (8333.space probe) | rig | edit accepted_mints WITHOUT restart → observe probe set; then restart → observe; compare wallet.db registrations. Decides whether wallet.db or config.json is authoritative on boot; fix + test the winner |
| NDS 5.0.2 auth-mark redesign (606fb23) physical re-verify | MT3000-class rig (OpenWrt 24.10 + NDS) | auth → deauth cycle with `iptables -t mangle -S ndsOUT` diff before/after; assert no leaked per-client rules; new client-agnostic ndsNET accept rule present (bench-proven on VM only) |
| PRTA #103 portal layout coverage | rig + Playwright place | F7 tests + browser specs against `tollgate-site-2051` layout |
| Rate-limit env persistence (tmbg#88) | rig (Go backend) | set `TOLLGATE_RATE_LIMIT_RPM` via init; restart; hammer `:2121/` payment root; assert limit matches. Currently not persistable through the init script — queued upstream fix, needs this test ready |
| Port-2129 exposure / CGI reverse proxy (upstream #213) | rig | when the loopback-bind + `/api/` refactor lands: assert `:2121` loopback-only, portal uses relative URLs, payment works end-to-end |
| S2 RED portal keyset dialect (fakewallet V2 32-hex vs old portal) | virtual lab first, then rig | matched portal+backend pair deploy (option b from the S2 spike); portal-payment e2e story re-enabled once green |
| 403/`-1/-1` etc. sentinel behavior of `/balance` for idle clients | rig | extend `hw-readonly-check.sh` G2 semantics into contract + story test |

## 9. Commissioned-runbook template v2 (the process fix)

Every future live commission must carry, verbatim:

1. **Invariants**: what must NOT change (config files, services, NDS state,
   mints, tokens).
2. **Command block**: pytest with `--read-only` (+ `--json-report`, `-rs`).
3. **Pre/post state audit**: `live-snapshot.py snapshot` … `diff` … restore on
   drift, with paging on drift.
4. **Exclusions with reasons** (deauth: live session; payment: real tokens).
5. **Reporting**: per-test table + probe outputs + the snapshot-diff verdict.

## 10. Immediate follow-ups

- [x] F1/F3/F10 (read-only guard, replace_mints default, doc fix) — done
      2026-10-01 PM; unit-tested (48 tests across the two new files)
- [x] F4 live-snapshot script — done; live validation pending USB-LAN replug
      (command block in §5 implementation notes)
- [ ] Live validation run (see §5) once `enp0s20f0u4` is back
- [ ] Coordinator reconciliation (§7.1)
- [ ] Mirror the `@mutator` guard into the vendored `tollgate_lab` copy on
      its next sync (same unguarded `replace_mints` there)
- [ ] Relayed results acked by m5stick-rig lane; their client's session
      re-verified on their side
- [ ] F5/F6/F7/F8 coverage arc scoped
