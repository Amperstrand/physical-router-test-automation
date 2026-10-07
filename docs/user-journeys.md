# User journeys — realistic behavior map for TollGate testing

> This is the behavioral ground truth the extensive-test effort maps to:
> how real people actually use TollGate, decomposed into journeys, each
> with its **happy / failure / regression** paths and the tests that own
> them today. Where a path has no test, it is an explicit **GAP** the
> extensive test should cover. The story taxonomy (`docs/user-stories.md`,
> generated map in `docs/user-stories.generated.md`) joins via the
> `pytest.mark.story` marks; path classification joins via
> `pytest.mark.happy|failure|regression` (new tests tag paths; legacy
> files are classified at file level in the tables below).
>
> Realism rule: a journey step is realistic only if a flesh-and-blood
> user could do it with a stock device and a Cashu/Lightning wallet —
> no SSH, no curl, no test-only endpoints. Tests that need the operator
> view belong to the operator journeys, not the guest ones.

## How to read / extend

| Column | Meaning |
|---|---|
| Steps | What the user actually does, in order |
| Happy / Failure / Regression | Path class → owning tests (`—` = GAP) |
| Stories | `user-stories.md` IDs this journey proves |

Run one journey's happy path, e.g.: `pytest --story G2 -m "happy or not failure"`.

---

## Guest — the person who just wants internet

### JG1 · First join & pay (the money path)

Steps: join the open `TollGate-*` AP → OS captive check pops the portal
→ open wallet app → copy token → paste into portal → press Purchase →
see the checkmark → browse.

| Path | Tests | Notes |
|---|---|---|
| Happy | `tests/stories/test_user_pays_and_gets_internet.py`, `tests/api/test_e2e_portal_payment.py`, `tests/scenarios/test_captive_portal_cashu_payment.py` (browser-driven), `tests/phone/test_rig_wallet_payment.py` (real wallet on phone) | The phone test is the most realistic artifact we have |
| Failure | `test_wrong_mint.py` (token from another mint), `test_minimum_token.py`/`test_edge.py` (too-small/garbage token), `test_double_spend.py` (replay), G7 rail `test_portal_failure_copy.py` (backend down = bounded named failure), `test_degraded_portal.py` (mint down = degraded UI, still honest) | Portal-side failure COPY in the UI itself still GAP (G7 pending part) |
| Regression | `test_token_formats.py` + `tests/stories/test_v4_token.py` (modern wallets emit V4), G3 `test_portal_markers.py` (portal generation drift), `test_post_payment_redirect.py` (redirect after pay), `test_pay_response_structure.py` (wire shape) | |
| Stories | G1, G2, G3, M1, M2 | |

### JG2 · Session life & renewal

Steps: paid session → watch balance/time on portal → data or time runs
out → get kicked → portal offers re-purchase → pay again.

| Path | Tests |
|---|---|
| Happy | `test_balance_page_reachable.py`, `tests/stories/test_session_expiry_and_repayment.py`, `tests/phone/test_extend_session.py` |
| Failure | `tests/phone/test_expiry_kick.py`, `test_rust_basic_pay_balance_chain.py` (session lookup by REAL MAC — the phantom-MAC class), `test_sentinel_error.py` |
| Regression | `tests/stories/test_upgrade_preserves_sessions.py`, `tests/phone/test_session_persistence.py`, `test_session_endpoint.py` |
| Stories | G4, G6 |

### JG3 · Lightning payer

Steps: join → portal Lightning tab → sized invoice → pay from LN wallet
→ access granted.

| Path | Tests |
|---|---|
| Happy | `test_lightning_portal.py` |
| Failure | `test_lightning_backoff.py` (mint error → backoff, no hammering), `test_lightning_quote_persistence.py` (restart mid-quote) |
| Regression | `test_quote_persistence.py`, `test_quotes_wireformat*` |
| Stories | G2 (LN flavor), O3 |

### JG4 · Returning device / flaky radio

Steps: same device rejoins after sleep/roam → still authed while session
lives → honest expiry after.

| Path | Tests |
|---|---|
| Happy | `tests/phone/test_session_persistence.py` |
| Failure | — (**GAP**: no test simulates radio-level reassociation mid-session at the API tier; the virtual lab has no real RF) |
| Regression | `tests/stories/test_contract_validation.py` |
| Stories | G4, G5 |

### JG5 · Power user (CLI/bash client)

| Path | Tests |
|---|---|
| Happy | `test_bash_client.py`, `test_cli_wallet.py`, `test_laptop_clientd` lanes |
| Failure | `test_cli_json_config.py` (bad invocations) |
| Regression | `test_cli_version.py`, R1 parity family |
| Stories | G8 |

**Realism gaps (guest):** no journey drives a stock third-party wallet
end-to-end on the bench except the single rig-wallet phone test (G5);
OS-native captive-popup behavior is deliberately bypassed by the
framework (documented in AGENTS.md — popup is unreliable); multi-device
household (two clients, one pays) has no dedicated test; weak-signal /
slow-payment UX (latency beyond `test_payment_latency.py`'s happy
measurement) is untested at the failure boundary.

---

## Owner — the person whose router earns

### JO1 · Boot & earn (including bad days)

Steps: power on → router serves even if mints unreachable → mints come
back → ads/payments resume → payouts accrue.

| Path | Tests |
|---|---|
| Happy | `test_degraded_mode.py`, `test_degraded_portal.py`, `test_cli_degraded_operations.py`, `test_mint_health.py` (scenarios tier) |
| Failure | `test_mint_502_*.py`, `test_mint_recovery.py` (#275 class), `test_try_all_mints.py`, `test_local_mints.py` |
| Regression | O3 rails (`test_stray_2121_sweep.py` — restart leaves no zombie), `test_wgm_startup.py`, `test_boot_hygiene.py` |
| Stories | O1, O2, O3 |

> **Known product bug to chase (issue #10):** a deployed Go build never
> recovered from degraded when the mint returned. The extensive test
> MUST include the mint-returns→ad-recovers assertion against the
> release candidate build; `test_mint_recovery.py` is the owner.

### JO2 · Upgrade without tears

| Path | Tests |
|---|---|
| Happy | `tests/scenarios/test_upgrade.py`, `test_install_paths.py` |
| Failure | `test_ipk_lifecycle.py` (bad package), `test_pr207_ap_setup_reinstall.py` |
| Regression | `tests/stories/test_upgrade_preserves_sessions.py`, O4 rails (config survives edits) |
| Stories | O4, O5 |

### JO3 · Admin & surfaces

| Path | Tests |
|---|---|
| Happy | `test_luci_admin_ui.py`, `test_admin_portal_visual.py`, SSL lifecycle family |
| Failure | `test_rpcd_security.py`, `test_pr198_ssrf_callback.py`, `test_security_fixes.py`, `test_config_permissions.py` |
| Regression | `test_admin_luci_ports.py` |
| Stories | O8 |

### JO4 · Merchant/reseller setup

| Path | Tests |
|---|---|
| Happy | `test_config_save_identities.py`, `test_reseller_mode.py` (scenarios) |
| Failure | `test_profit_share_validation.py` |
| Regression | `test_merchant_provider.py`, `test_mint_payout.py` |
| Stories | M4, M6 |

---

## Operator — the bench tells the truth

| Path | Tests |
|---|---|
| Happy | P1 commissioned battery (`test_discovery_mints.py` + 3; runbook `/tmp`/status docs), `test_lab_health.py` |
| Failure | `test_backend_readiness.py` (not-ready ≠ dead), G7 rail |
| Regression | `test_mock_api_advertisement_format.py` (mock ≡ real), the P11 framework-machinery unit rails |
| Stories | P1, P2, P3, P11 |

**Venue readiness (2026-10-07):** virtual lab DOWN (0 QEMU; contended
lane etiquette — check before start-poc); 326D dark (no beacons —
power-cycled off?); ALPHA/BRAVO labgrid-free but DUT bays unrouted from
house LAN (conwrt-bench needed); NR7101 reachable but labgrid place
match broken + creds unknown; phone adb empty on the rig. The extensive
test should treat venue bring-up as step zero — see
`docs/extensive-test.md`.

---

## Maintainer parity & wire

Covered by the R-family (`test_go_rust_basic_parity.py`,
`test_cashu_compat_matrix.py`, `test_gateway_token_format.py`,
`test_keyset_id_versions.py`, `test_nut18_payment.py`, `test_nut24.py`)
— happy=parity green, failure=one side diverges loudly, regression=wire
formats frozen. R5 (green conformance before publish) is the process
gate this whole effort serves.
