# Bench lanes: keeping PR code off the router

Status: implemented 2026-09-23 (PRTA-BENCH-SECURITY).

## The defect this closes

`.github/workflows/ci.yml` triggered on `pull_request` and carried two jobs,
`test-ui` and `test-physical`, on `runs-on: [self-hosted, tollgate-router]`.
Both were parked at `if: false` — *"Disabled until self-hosted runner is
configured"* — and the header comment claimed you had to uncomment the `runs-on`
lines to enable them. That comment was stale: the `runs-on` lines were already
live YAML, and `if: false` was the only gate.

That is a one-boolean distance from arbitrary code execution on the bench host
for **any contributor's PR**: the machine that flashes routers, holds the repo
secrets, and sits in the bench's L1/L2 neighbourhood. Worse, `test-physical`
force-enabled `TOLLGATE_ENABLE_WIFI_CLIENT_TESTS` and
`TOLLGATE_ENABLE_DATA_ALLOTMENT_TESTS`, which mutate router state and burn paid
traffic — a PR run would have corrupted the bench for the next agent. And
`test-physical` ran the *whole* Playwright config, destructive project included
(`tests/destructive/firmware-upgrade.spec.mjs`,
`reboot-recovery.spec.mjs`), i.e. firmware flash from a PR.

`publish` (`needs: [test-ui]`) could therefore never run: a job whose dependency
is skipped is itself skipped. A dead cascade, with no error to notice.

## The shape now

| workflow | triggers | runners | can a PR reach the bench? |
|---|---|---|---|
| `ci.yml` | `push: main`, `pull_request: main`, `workflow_dispatch` | `ubuntu-latest` only | no |
| `hw-smoke.yml` | `workflow_dispatch` + `schedule` (dormant) | `[self-hosted, tollgate-router]` | no — the workflow has no PR trigger |

`hw-smoke.yml` has three lanes:

| lane | needs | approval | mutating? |
|---|---|---|---|
| `readonly-surface` | — | none | no: no secrets, no credentials, no payment, no writes |
| `mutating-e2e` | `readonly-surface` | `bench-hardware` environment | yes; paid-traffic specs are **off** unless opted into per dispatch |
| `destructive-e2e` | `readonly-surface` | `bench-hardware` environment + `include_destructive` input | yes: firmware flash / reboot |

Every mutating lane runs `scripts/hw-readonly-check.sh` **first**: the read-only
gate is a precondition, not a parallel nicety. `concurrency: hw-bench` serialises
all bench work and never cancels (`cancel-in-progress: false`) — a half-finished
mutating lane leaves the router transitional.

## Enabling hardware CI (operator checklist)

1. Register a self-hosted runner on the bench labelled `tollgate-router`.
   Today `gh api repos/felixfelix-bot/physical-router-test-automation/actions/runners`
   returns `{"total_count":0}` (admin-readable, so authoritative), and upstream
   reads 403 ⇒ *unseen*, not *absent*.
2. Create the `bench-hardware` environment **with required reviewers**. The
   mutating lanes declare `environment: bench-hardware`, so without reviewers
   GitHub auto-creates the environment unprotected — the approval gate is the
   operator's half of the contract.
3. Set repo variable `HW_BENCH_SCHEDULE=true` to arm the weekly read-only run
   (and `HW_BENCH_HOST` if the bench is not at 192.168.1.1). Until then the cron
   self-skips, so there are no queued-forever runs.
4. `make hw-readonly` is the local equivalent of the read-only lane and needs no
   lock, no secrets and no runner.

Two platform facts worth knowing before you try to trigger the lane:

- `workflow_dispatch` only becomes available once the workflow file exists on the
  repository's **default branch**. Until this lands on `main`, `gh workflow run
  hw-smoke.yml` answers `HTTP 404: not found on the default branch` — the local
  `make hw-readonly` path is the way to run the read-only lane in the meantime.
- A `schedule` trigger with no runner queues ghost runs, which is why the cron is
  gated on the `HW_BENCH_SCHEDULE` repo variable instead of being live.

There is no `if: false` anywhere in `.github/workflows/`; enablement is a
dispatch input, a repo variable, or an environment approval instead. That is
deliberate — a disabled job behind a boolean is an invitation to flip it.

## The guard

`scripts/ci/check-workflow-hw-isolation.sh` runs in the `ci.yml` `lint` job on
every PR and fails on:

- **R1** a `pull_request`/`pull_request_target`-triggered workflow that reaches a
  bench runner or a bench-mutating env flag. Bench reach is decided on the
  **label set**, not on the literal string `self-hosted` — every label used by
  the hardware workflow's own runner declaration (plus `self-hosted` and
  anything in `HW_RUNNER_LABELS_EXTRA`) is denied, so a runner target naming
  only the bench label is caught too. An **expression-valued** runner target
  (`${{ vars.RUNNER_LABEL }}`) cannot be verified by reading, so it fails
  closed;
- **R2** `hw-smoke.yml` missing, PR-reachable, or carrying a trigger outside
  `{workflow_dispatch, schedule}`;
- **R3** any falsy job guard in the tree — a capitalised `False`/`FALSE` counts
  as one, as do the YAML-1.1 falsy words `no` and `off`, the numeric zero, a
  quoted empty string, an expression that is literally false, and a folded
  scalar (`>-`) whose body is falsy;
- **R4** a job referencing the bench-mutating env flags without an approval gate
  NAMED `bench-hardware` (default, `HW_ENVIRONMENT_NAME` overrides). The name is
  checked, not just the presence of the key — a typo such as `bench-hadware`
  would silently drop the required-reviewer gate;
- **R5** a job in the hardware workflow using `secrets.` without the same named
  environment gate;
- **R6** a bench-label runner in any *other* workflow file, whatever its
  triggers — the reusable-workflow path (`workflow_call` reachable from a
  `pull_request` caller). A file that legitimately runs on a different
  self-hosted fleet must be named in HW_NON_BENCH_SELF_HOSTED_ALLOW (default
  cloud-lab-runner.yml, the ephemeral GCP `cloud-lab` VM). Two limits keep that
  allowlist from becoming a hole: it never covers a bench-*specific* label such as
  tollgate-router, and it excuses the generic `self-hosted` label only on a
  runner line that also names a genuinely different fleet (HW_NON_BENCH_FLEET_LABELS,
  default cloud-lab). A PR picks an allowlisted file's *name* as freely as its
  contents, and bare self-hosted matches every self-hosted runner, the bench
  included. The exception is printed on every run.

Forms the parser cannot read **fail closed** rather than passing by default: a
flow-mapping `on:`, a non-empty `on:` block that parses to zero triggers
(mis-indented keys), an expression-valued or block-sequence `runs-on` value, a
hardware workflow whose job structure is not parseable at the expected
indentation, and a bench-specific label set that cannot be derived at all (the
denied set is otherwise read out of hw-smoke.yml, which a PR can respell, so
HW_BENCH_LABELS_DEFAULT — default tollgate-router — is a hardcoded floor).

The first round of this guard matched the literal `self-hosted` string and only
checked that an approval-gate key existed; repeated rounds of cold cross-family review
(a different model family each time, each round reviewing the previous round's head)
found each hole in turn — a label-only runner
target, an expression, a capitalised falsy condition, a typo'd environment name, a
block-sequence `runs-on`, a derived label set, a flow-mapping `on:`, a folded kill
switch, and the reusable-workflow path — and the rules above are the fix. Every
one of them has a RED case. 24 self-test cases cover them one regression at a
time.

**Limit, stated rather than hidden:** the guard is an in-repo check, so it runs
from the PR's own checkout — a hostile PR can delete it in the same commit that
adds a bench job. For that PR the mechanical protection is branch protection plus
making the `lint` check *required*, not this script. The guard's job is to stop
the accidental path (someone re-enabling a parked job or adding a lane in good
faith), which is how the original defect appeared.

It scans effective YAML only (comments stripped), so this documentation can name
the anti-patterns it bans.

`scripts/ci/test-check-workflow-hw-isolation.sh` injects one regression at a time
into a copy of the real tree and asserts the guard fails with the right rule —
24 cases, all green. A guard never seen red is decoration, not evidence.

## Read-only vs mutating locally

`scripts/hw-readonly-check.sh` gates, in order:

- **G0** refuse to run at all if `TOLLGATE_ENABLE_WIFI_CLIENT_TESTS` /
  `TOLLGATE_ENABLE_DATA_ALLOTMENT_TESTS` is enabled — a read-only lane that
  silently carries mutating flags is how the bench gets corrupted;
- **G1** `:2121/` answers 200 **and** is in FULL mode (`kind: 10021` with
  `price_per_step`). Degraded mode is a FAIL, never adapted to;
- **G2** `:2121/balance` is parseable; unreadable ⇒ treated as NOT idle (fail
  closed);
- **G3** `scripts/tollgate-port-sweep.sh` (when present) must pass; absence is a
  loud PARTIAL, not a silent pass.

Exit codes: `0` pass, `1` gate failed, `2` refused to run. The last stdout line
is a JSON summary for artifacts.

## Known gaps (not fixed here)

- `publish` is now `needs: [lint]` and gated behind the `PUBLISH_TEST_REPORTS`
  repo variable: the report lane is not yet runnable from a hosted runner.
  `scripts/run-tests.sh` → `scripts/run-profile.sh` requires a Python venv at
  `$HOME/.tollgate-test-venv` that no CI step creates, and `TOLLGATE_LUCI_URL`
  points at a LAN-only bench. It also never ran before, so nothing regressed.
- `package.json`'s `publish-report` script points at `scripts/publish-report.sh`,
  which does not exist in the tree.
- `tests/helpers/inventory.mjs getRouter()` still falls back to a stale
  `192.168.13.112` when neither `TOLLGATE_LUCI_URL` nor `config/routers.json` is
  set; the lanes pin `TOLLGATE_ROUTER_HOST` explicitly to avoid it.
