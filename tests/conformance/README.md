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
