# Conformance / fault-injection matrix

Shared, backend-agnostic conformance harness for the TollGate Go and Rust
backends. This directory is the **co-owned spec** referenced by:

- Go twin: [tollgate-module-basic-go#503](https://github.com/OpenTollGate/tollgate-module-basic-go/issues/503)
- Rust twin: [tollgate-module-basic-rust#15](https://github.com/Amperstrand/tollgate-module-basic-rust/issues/15)

## What lives here

| File | Purpose |
|------|---------|
| `matrix.yaml` | The single source of truth: 7 invariants + the fault scenarios. Scenario IDs are stable; lanes and reports join on them. |
| `faultproxy.py` | Stdlib-only HTTP fault proxy with per-route rules: `drop` (request-side black-hole), `drop_response` (mint processes, response swallowed — the ambiguous-outcome fault), `delay`, `status` (429/500), `reset`, and `notify` (blocking webhook at `notify_on: request\|response`) — plus blinded-message recording. `notify_on: response` gives lanes a deterministic kill trigger inside the ambiguity window. |

## Ownership rules

1. **Do not fork this matrix into either backend repo.** Backend lanes
   consume it from here, or from a pinned copy plus checksum — never a
   divergent edit.
2. Every change to invariants or scenarios must reference both twin
   issues in the PR that changes `matrix.yaml`.
3. Add scenarios; never rename or recycle IDs. Verdict tables and
   differential reports key on IDs.
4. Scenarios may only rely on **HTTP, the mint, and process/VM control**.
   Backend-internal instrumentation may not be required to *drive* a
   scenario; backend-specific log markers are boundary-detection hints
   only.

## Subsets

- `fast` — the PR-lane subset: duplicate submissions, timeout-retry,
  restart-mid-payment (the `pay-kill-post-receive-pre-session` boundary),
  mint alias spellings.
- `full` — everything; intended for nightly runs.

## How a lane consumes the matrix

1. Parse `matrix.yaml`, select scenarios by `subset`.
2. For each scenario:
   - translate `fault.proxy` rules into `faultproxy.py` rules (POST them
     to `/__fault/control` at runtime, or seed via `--rules`);
   - translate `process_control` / `vm_control` into venue actions
     (container kill, process kill, QEMU reboot);
   - run the `driver` steps against the backend under test;
   - assert the listed invariants on observable state.
3. Emit a verdict row per scenario.

Invariants that a backend cannot assert yet (e.g. `no-fund-loss` fully,
before the Go payment-record store lands) are recorded as `pending` with
the blocking issue — `pending` is a first-class verdict, not a failure.

## Verdict table format (JSON, one file per run)

```json
{
  "backend": "go",
  "matrix_version": 1,
  "generated": "2026-09-22T15:00:00Z",
  "rows": [
    {
      "scenario": "swap-timeout-retry",
      "invariants": {
        "no-fund-loss": "pass",
        "no-output-reuse": "pass",
        "service-or-refund": "pending",
        "restart-converges": "pass"
      },
      "verdict": "pass",
      "evidence": ["logs/swap-timeout-retry.log", "observations.json"]
    }
  ]
}
```

Verdict values: `pass`, `fail`, `pending` (blocked on a tracked issue),
`skip` (venue cannot drive it), `error` (lane bug, does not judge the
backend).

## Differential report

A differential report joins two verdict tables on `scenario`:

| scenario | go | rust-basic | delta |
|---|---|---|---|
| swap-timeout-retry | pass | fail | rust violates no-output-reuse |

Divergences become issues on the responsible repo. Divergence is a review
item, not folklore.

## Proxy observation of blinded messages

`GET /__fault/observations` returns every blinded-message hash the proxy
saw and flags re-sightings (`reused`). A sighting count > 1 for a
deterministically derived output means the backend re-exposed a
derivation range — the brick class of #257/#266/#480 — unless the two
sightings are provably two different legitimate outputs. Lanes assert
`reused == []` for the `no-output-reuse` invariant.

## Host-venue runner (v2) — Rust lane

`run_matrix.py` executes every host-drivable scenario from
`matrix.yaml` against one backend binary with a real cdk-mintd behind
the **shared** `faultproxy.py` (driven via its control endpoint — no
private rule schema), and emits `results/{results.json,results.md}`.

Hardening after the PR #16 Codex round (the v1 runner could pass
vacuously):

- kill boundaries fire from the proxy's `notify_on: response` webhook
  (deterministic, inside the ambiguity window);
  `post-session-pre-gate` / `post-gate-pre-response` use named delay
  approximations (400/900 ms after the held swap response — documented,
  not proxy-observable on the host venue);
- a restarted backend **reuses the crashed instance's config dir** —
  restart scenarios observe reconciliation, not a fresh wallet;
- unparseable observable state is a harness `error`, never a sentinel;
- every declared invariant is accounted for: checked, or `pending` with
  the reason — missing checks cannot hide inside a pass;
- strict single-payment value accounting (proven-moved == wallet
  balance at 0 mint fee);
- duplicate scenarios actually submit the duplicate and assert
  value-level retry safety (a 200 idempotent replay of the same grant
  is correct, not a double grant);
- CLI wire encoding follows the backend (JSON CLIMessage for Go, plain
  text for Rust) per `tests/api/test_go_rust_basic_parity.py`;
- readiness probes are functional (mint keysets answer; backend serves
  its advertisement), not raw TCP connects;
- classes the host venue cannot drive (`vm_control`, `mint_control`,
  `drain`) report `pending-venue` with the reason.

### Known pending adjudication: `no-output-reuse`

The v2 runs observe every backend swap `B_` sighted exactly **twice**
(digests stable across scenarios = fixed-seed deterministic
derivation), while the backend log shows a single `create_swap`, one
journal entry, and correct value state. Source unproven: proxy
double-count vs legitimate CDK saga re-POST. Until
`faultproxy.py` gains request-level logging, the runner **records** I3
sightings with detail and reports the invariant `pending` — an unsound
observer must not auto-fail a backend. Tracked issue: (see PRTA).

### Usage

```
python3 tests/conformance/run_matrix.py --backend rust-basic \
    --binary <tollgate-binary> --mint /opt/cdk-mintd/cdk-mintd \
    --out tests/conformance/results/rust-basic
python3 tests/conformance/run_matrix.py --backend go \
    --binary <tollgate-go> --out tests/conformance/results/go
```
