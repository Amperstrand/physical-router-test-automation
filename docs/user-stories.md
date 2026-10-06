# User Stories — TollGate under test

The personas and stories this suite serves. Every story maps to the tests that
prove it (or marks the gap). Status: **Covered** (a test owns it), **Partial**
(some coverage, known gaps), **Gap** (no test yet), **HW-gated** (needs the
bench / live router).

Personas: **Guest** (pays for WiFi) · **Owner** (runs the TollGate router) ·
**Merchant** (sells access / shares profit) · **Operator** (QA / bench, PRTA's
direct user) · **Maintainer** (release + upstream).

---

## Guest — "I just paid, let me online"

| ID | Story | Proof | Status |
|----|-------|-------|--------|
| G1 | As a guest, when I join a TollGate AP I get a captive portal (not a dead browser), so I know how to get online. | `tests/api/test_captive_api.py`, `test_portal_verify.py` | Covered |
| G2 | As a guest, I can pay with a Cashu token and get internet within seconds, without an account. | `tests/api/test_e2e_portal_payment.py`, `test_local_payment.py` | Covered (fakewallet); live run HW-gated |
| G3 | As a guest on a v0.6 portal generation, the same flows work — detection must not regress across portal generations. | `tests/unit/test_portal_markers.py` (PR #17) | Partial — v0.6 markers land with #17 |
| G4 | As a guest with an expired session, I can re-purchase from the portal (0.6 session-state), and the renewal affordance is honest. | `tests/api/test_session_state*.py`, cashud-side expired-gateway rails | Partial |
| G5 | As a guest on a phone, the portal works on a real Android browser — not just curl. | `tests/phone/test_rig_phone_payment.py` (fresh_session reset, PR #8) | Partial — rig-dependent |
| G6 | As a guest, my balance/remaining time is visible on the portal while my session is live. | `tests/api/test_balance_page_reachable.py` | Covered |
| G7 | As a guest, a hung backend gives me a clear failure instead of an endless spinner. | Live finding 2026-10-05: backend DOWN/HUNG on :2121 surfaced as opaque timeouts | **Gap** — needs a portal-side failure-copy story after #18 |
| G8 | As a guest using the CLI/bash client, I get the same answers as the portal. | `tests/api/test_bash_client.py` | Covered |

## Owner — "My router earns while I sleep"

| ID | Story | Proof | Status |
|----|-------|-------|--------|
| O1 | As an owner, my router boots and serves even when all mints are unreachable (degraded mode), and recovers when they return. | `tests/api/test_degraded_mode.py`, `test_degraded_portal.py`, `test_cli_degraded_operations.py` | Covered |
| O2 | As an owner, a temporarily-down mint never blocks purchases — other mints serve, and the dead one recovers automatically. | `tests/api/test_dual_mint.py`, cloud-lab `test_mint_failure.py` | Covered |
| O3 | As an owner, a crashed or stray service instance can't zombie-serve stale state on :2121 after a restart. | Live evidence 2026-10-05 (:2121 hang); fix rides tollgate-module-basic-go#98 `stop_service` sweep | Partial — fix in review, regression test to follow |
| O4 | As an owner, my config survives edits without silently resetting to defaults (the x280 un-brick class). | tollgate-go#102 min-steps parser rails; PRTA#13 tracks the physical restore | Partial — loader fail-loud verification outstanding |
| O5 | As an owner, I can flash firmware upgrades on the bench with confidence the wizard completes or fails loudly. | `installer/test_installer_e2e.py` (PR #2) | Partial — PR in review |
| O6 | As an owner, router-to-router use cases (firewall, MPTCP bonding, SQM) apply and verify reproducibly via UCI. | `conwrt/test_use_cases.py` (hardened in PR #3) | Covered |
| O7 | As an owner, the device-mint (on-router fakewallet) runs entirely in RAM and never wears my flash. | `tests/api/test_m5_hardware_mint.py` (passed on real HW Oct 1) | HW-gated — ESP32s reserved for nucleo testing |
| O8 | As an owner, admin surfaces (LuCI ports, admin portal) are reachable only where intended. | `tests/api/test_admin_luci_ports.py`, `test_admin_portal_visual.py` | Covered |

## Merchant — "Payouts are correct and provable"

| ID | Story | Proof | Status |
|----|-------|-------|--------|
| M1 | As a merchant, every payment is charged in the advertised currency/denomination — access-denominated pricing is enforced. | `tests/api/test_access_denominated.py` | Covered |
| M2 | As a merchant, a double-spent token never grants a second session. | `tests/api/test_double_spend.py` | Covered |
| M3 | As a merchant, concurrent purchases are serialized correctly — no interleaving grants. | `tests/api/test_concurrent_payments.py` | Covered |
| M4 | As a merchant, swap fees are accounted so my payout matches what the mint actually charged. | `tests/api/test_edge_tokens.py` (token flow, fee shape) | Partial |
| M5 | As a merchant, errors returned to guests never leak internals (sanitized wire copy). | `tests/api/test_error_sanitization.py` | Covered |
| M6 | As a reseller, my profit-share identity setup round-trips through config without corruption. | `tests/api/test_config_save_identities.py`, cloud-lab reseller configs | Partial |

## Operator — "The bench tells me the truth"

| ID | Story | Proof | Status |
|----|-------|-------|--------|
| P1 | As an operator, the commissioned read-only API battery runs against a live router without touching its state. | `tests/api/test_discovery_mints.py` + 3 (runbook Oct-1 rev; executed Oct 4-5) | Partial — probes parked on USB-eth flakiness |
| P2 | As an operator, mint advertisements are validated (kind-10021, pricing present) and unreachable mints are filtered before I test them. | `tests/api/test_discovery_mints.py` | Covered |
| P3 | As an operator, "backend not ready" vs "backend dead" are distinguishable failures, not opaque timeouts. | `tests/unit/test_backend_readiness.py` (PR #18) | Partial — lands with #18 |
| P4 | As an operator, tests cannot accidentally mutate a live DUT — read-only Router guards exist and are enforced. | `abandoned/read-only-lane` branch (mutator guards, `--read-only` wiring) | **Gap** — branch parked, needs review/landing |
| P5 | As an operator, test runs leave evidence films I can review later (stills, demos, publishing pipeline). | `installer/film_portal_still.mjs`, `make_film.py`, `publish_film.py`, `record_demo.mjs` | Partial — PR #2 (nostr opt-in question open) |
| P6 | As an operator, destructive steps are gated on device identity — the wrong router can never be flashed. | `bench-device-identity/`, `scripts/bench/device-identity.sh` (fail-closed pin) | Covered |
| P7 | As an operator, mock and live routers present the same interface so suites run in both modes. | `lib/mock_router.py` parity tests; conformance suite | Covered |
| P8 | As an operator, replacing the mint set is an explicit, forced decision — never a silent default. | `tests/unit/test_replace_mints_guard.py` (PR #19) | Partial — lands with #19 |
| P9 | As an operator, two-router cloud scenarios (autopay across a chain) run without bench hardware. | `tests/cloud-lab/` (docker-compose: client/mint/tollgate) | Covered |
| P10 | As an operator, NDS auth state is never perturbed while a paid session exists (deauth exclusion). | Commission exclusions (runbook "Excluded on purpose") | Covered by policy — needs a guard test if ndsctl ever enters scope |

## Maintainer — "Releases and parity hold"

| ID | Story | Proof | Status |
|----|-------|-------|--------|
| R1 | As a maintainer, the Go and Rust basic modules behave identically where both exist. | `tests/api/test_go_rust_basic_parity.py` | Covered |
| R2 | As a maintainer, the Cashu/NUT compatibility matrix is enforced, not aspirational. | `tests/api/test_cashu_compat_matrix.py`, `docs/cashu-compatibility-matrix.md` | Covered |
| R3 | As a maintainer, token/gateway wire formats stay stable across releases. | `tests/api/test_gateway_token_format.py`, `test_quotes_wireformat*` (rust side) | Covered |
| R4 | As a maintainer, the FIPS exit-node path is exercised so compliance claims are backed. | `tests/api/test_fips_exit_node.py`, `fips-exit-e2e/` | Covered |
| R5 | As a maintainer, releases have a green conformance re-run before publish (rc gate). | `conformance/`; rc1 verdict 2026-10-05 (fork-tag handoff pending) | Partial — process gate |
| R6 | As a maintainer, cryptography is real RNG, never a stub (passwords, keys). | `tests/api/test_crypto_rand_password.py` | Covered |
| R7 | As a maintainer, the embedded-portal build stays equivalent to the served portal. | `tests/api/test_embedded_portal.py` | Covered |

---

## Gap register (stories worth tests that don't exist yet)

1. **G7** — portal-side failure copy when the backend is unreachable (depends on #18 landing).
2. **P4** — land the read-only lane: the guards exist on `abandoned/read-only-lane`; a review + merge decision turns the policy into a regression suite.
3. **O3-regression** — a bench test that starts a stray `:2121` holder and asserts the service restart sweeps it (pairs with tollgate-go#98).
4. **O4** — config-loader fail-loud verification (bad JSON → error, not defaults) at the PRTA level once the tollgate-go fix lands.
5. **M4** — swap-fee reconciliation against real mint receipts (fakewallet can't see true fees).
