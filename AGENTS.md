# AGENTS.md — Operational Knowledge for physical-router-test-automation

This file contains hard-won operational knowledge for agents and humans working with physical OpenWrt routers (D-Link COVR-X1860, GL.iNet GL-MT3000). Read this before touching a router.

## What This Repository Does

This is a **multi-tier test framework** for [tollgate-module-basic-go](https://github.com/OpenTollGate/tollgate-module-basic-go) (Go v1) and [tollgate-rs](https://github.com/OpenTollGate/tollgate-rs) (Rust v1) running on physical OpenWrt routers. It tests the TollGate WiFi payment system — Cashu ecash tokens for metered internet access through captive portals.

**Three test tiers:**
- **API** (`tests/api/`) — SSH to router, hit HTTP endpoints. No phone needed. Fast.
- **Phone** (`tests/phone/`) — Android device via ADB through captive portal. E2E.
- **LuCI** (`tests/web/`) — Playwright browser tests against LuCI admin UI.

**Where tests run:**
- **Physical lab** — real routers on LAN, real phones, real WiFi.
- **Cloud lab** — SHC or GCP nested-KVM (OpenWrt + Debian QEMU). API tests only, fire-and-forget.
- **Virtual lab** — local QEMU + network namespaces. For development.

### VM Provider Abstraction

Tests run on any of five providers via `TOLLGATE_VM_PROVIDER` env var (default: `shc`):

| Provider | Cost | Publishes results? | Use case |
|----------|------|-------------------|----------|
| `shc` | $0.01/run | ✅ Yes | Default cloud testing — cheapest, nested KVM |
| `gcloud` | ~$0.10/run | ✅ Yes | GCP nested-KVM with baked snapshot |
| `local-kvm` | Free | ❌ Never | Local KVM/QEMU — active VM lifecycle (create/destroy) |
| `local` | Free | ❌ Never | Pre-existing local VM (passive — no create/destroy) |
| `physical` | Free | ❌ Never | Physical router — privacy: never publish real SSIDs/MACs/IPs |

**Privacy control**: `can_publish` flag on `VMProvider`. Cloud providers (SHC, GCP) use ephemeral VMs with no real user data — safe to publish to tests.tollgate.me. Local providers (`local-kvm`, `local`, `physical`) may contain real SSIDs, MACs, IPs, SSH keys — results stored in gitignored `results/` directory only. The privacy check is in `test-pr.sh` (line ~323) and gates `publish-report.sh`.

**Local testing never publishes.** All three local providers (`local-kvm`, `local`, `physical`) have `can_publish = False`. No logs, screenshots, or test results leave the machine. The `test-pr.sh` gate enforces this — it checks `can_publish` before calling `publish-report.sh`.

```bash
# Check if current provider allows publishing
python3 scripts/provider.py can-publish -p shc    # exit 0 = yes
python3 scripts/provider.py can-publish -p local   # exit 1 = no

# Create/destroy VMs
python3 scripts/provider.py create -p shc --name my-test
python3 scripts/provider.py destroy -p shc --service-id 690
```

Provider implementations: `lib/cloud_lab/provider.py` (SHCProvider, GCPProvider, LocalKVMProvider, LocalProvider, PhysicalProvider).

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│                    Test Machine (macOS/Linux)                │
│                                                              │
│  pytest ─── Router.ssh() ──── sshpass/sshkey ──────────┐   │
│       │                                                  │   │
│       ├── lib/router.py ──────────── SSH to router ─────┤   │
│       ├── lib/cashu.py ──────────── Token minting       │   │
│       ├── lib/helpers.py ────────── gate_bug_fix, etc   │   │
│       ├── lib/deploy.py ─────────── CI artifact deploy  │   │
│       └── lib/clients/adb.py ────── Phone control        │   │
│                              │                            │   │
│                              ▼                            │   │
│              ┌──────────────────────────┐                 │   │
│              │  TollGate Router (OpenWrt)│◄───────────────┘   │
│              │  - nodogsplash (captive)  │                     │
│              │  - tollgate-wrt (Go/Rust) │                     │
│              │  - Port 2121 (backend)    │                     │
│              │  - Port 2050 (NDS portal) │                     │
│              └──────────────────────────┘                     │
│                     │ LAN WiFi                               │
│                     ▼                                        │
│              ┌──────────────────┐                            │
│              │  Android Phone   │                            │
│              │  (ADB connected)  │                            │
│              └──────────────────┘                            │
└──────────────────────────────────────────────────────────────┘
```

**Key ports on the router:**
- `2121` — TollGate backend API (GET /, POST /, GET /usage, GET /balance, GET /whoami, ln-invoice)
- `2050` — Nodogsplash captive portal gateway port (CGI scripts)
- `8080` — LuCI admin UI (Go v1 only)
- `22` — SSH (WAN firewall rule required for remote access)

## Test Gating Strategies

Tests decide at runtime whether they can run against the deployed firmware. There are three strategies. **The `pr(N)` marker mechanism still exists in `conftest.py` but NO test file uses it.** All tests use one of these three approaches:

### 1. Feature Detection (skip if absent)

Tests probe the router for a specific capability. If absent, `pytest.skip()`. These tests run against any firmware that has the feature.

```python
def _skip_if_no_degraded_support(router):
    resp = router.get_tollgate_status()
    if resp.get("success") is not True:
        pytest.skip("status command not available")
    raw = json.dumps(resp).lower()
    if not any(kw in raw for kw in ["degraded", "reachable", "mint_health"]):
        pytest.skip("no degraded mode support detected")
```

Existing skip helpers in `lib/helpers.py`:
- `skip_if_no_cli_socket(router)` — checks `/var/run/tollgate.sock`
- `skip_if_no_luci(router)` — checks port 8080
- `skip_if_no_sessions_json(router)` — checks `/etc/tollgate/sessions.json`

### 2. Bug Regression via `gate_bug_fix()`

For tests that verify a **known bug has been fixed**. When the fix is absent → xfail ("known issue"). When present → runs normally. Failure = regression.

```python
from lib.helpers import gate_bug_fix

def test_profit_share_boot_with_invalid_config(router):
    gate_bug_fix(
        _has_profit_share_validation(router),
        bug_id="profit-share-no-validation",
        fix_pr="PR #86",
    )
    # test body only runs if fix is present
```

Cross-reference incidents: link to `https://github.com/OpenTollGate/tollgate-knowledgebase/tree/main/incidents/YYYY-MM-DD_slug.md` in docstrings.

### 3. Unconditional

No gating. Runs against every firmware. Use for baseline API tests (health, config format, etc.).

### Backend-Aware Skipping

Tests marked `@pytest.mark.go_only` skip when `--backend=rust`. Tests marked `@pytest.mark.rust_only` skip when `--backend=go`. This is handled in `conftest.py` `pytest_collection_modifyitems`.

`BackendConfig` (`lib/backend.py`) provides feature flags that differ between backends:
- `has_luci` — True for Go, False for Rust
- `has_cli_socket` — True for Go, False for Rust
- `has_sessions_json` — True for Go, False for Rust

| Situation | Strategy |
|---|---|
| Feature may or may not be present | Feature detection (`_skip_if_no_*`) |
| Known bug, fix in flight | `gate_bug_fix()` (xfail = warning, fail = regression) |
| Baseline behavior that always works | No gating — runs unconditionally |
| Feature only in one backend | `@pytest.mark.go_only` or `@pytest.mark.rust_only` |

## Dual-Backend Testing (Go v1 + Rust v1)

The framework supports testing both the Go and Rust TollGate backends interchangeably. Both produce the same `.ipk` package name (`tollgate-wrt`) and install to the same paths.

**Architecture reference:**

| | Go v1 | Rust v1 |
|---|---|---|
| Repo | `OpenTollGate/tollgate-module-basic-go` | `Amperstrand/tollgate-rs-ai-research-and-experiments` |
| Branch | `main` | `experimental` |
| CI workflow | "Build and Publish" | "Build and Package" |
| Config path | `/etc/tollgate/config.json` | `/etc/tollgate/config.json` |
| Service name | `tollgate-wrt` | `tollgate-wrt` |
| LuCI UI | Yes | No |
| CLI socket | `/var/run/tollgate.sock` | Not implemented |
| Session persistence | `/etc/tollgate/sessions.json` | In-memory only |
| Profit share | Yes | **Yes** (verified July 2026 — logs show factor-based payouts) |
| API endpoints | 7 (all) | 7 (all) — v1 parity complete |
| V3 token payments | ✅ Verified (V1 keyset: allotment=66060288, V2 keyset: allotment=88080384) | ✅ Verified (amount=3, err=nil) |
| Mint keyset support | V1 + V2 full support (both verified end-to-end) | V1 + V2 (cdk) |

**Token payment format (CRITICAL):** Both Go and Rust backends expect the token as a **raw body** (`Content-Type: text/plain`), NOT as JSON. Sending `{"token": "cashuA..."}` as JSON causes the backend to treat the entire JSON string as the token — the prefix becomes `{"toke` instead of `cashuA`, causing `ErrInvalidTokenV3`. The test framework's `pay_direct()` method uses `curl -d @- -H 'Content-Type: text/plain'` (correct). Manual curl tests must use `-d "$TOKEN"` not `-d '{"token":"$TOKEN"}'`.

**Virtual lab payment testing:** `pay_direct()` routes payments through the Debian VM (10.99.99.100) instead of running curl on the router itself. This ensures the backend sees the correct source IP → MAC in ARP/DHCP. Running curl on the router (localhost) fails MAC lookup because `::1` has no MAC in DHCP leases or ARP table. The `container_nds_preflight` fixture deauths the client MAC before each test to prevent "already authenticated" errors.

**V3 token payment verification (July 2026, localhost virtual lab):**
Both Go and Rust backends successfully process V3 token payments end-to-end:
- Go + V1 keyset (testnut): `kind=1022, allotment=66060288 bytes, HTTP 200`
- Go + V2 keyset (CDK V2 mint): `kind=1022, allotment=88080384 bytes, HTTP 200`
- Rust + V1 keyset (testnut): `Receive completed, amount=3, err=<nil>`
- Both backends: token parsed, verified, payment processed, MAC authorized, session event returned

V4 tokens (`cashuB` prefix, CBOR) previously failed because gonuts lacked short keyset ID resolution. **Fixed in gonuts-tollgate v0.8.0** ([PR #284](https://github.com/OpenTollGate/tollgate-module-basic-go/pull/284) — pending merge). The fix adds `resolveShortKeysetIds()` which fetches active keysets from the mint and resolves 8-byte short IDs to full IDs before swap.

**Before the fix (gonuts < v0.8.0):** V4 tokens store keyset IDs as 8-byte short IDs (per NUT-00 V4 spec). gonuts's `TokenV4.Proofs()` converted the raw CBOR bytes directly to hex without resolving the short ID. When gonuts sent the 8-byte hex to the mint swap endpoint, the mint rejected it: `NUT02: ID length invalid, expected 8 bytes (short/v1) or 33 bytes (v2)`.

**After the fix (gonuts v0.8.0+):** `resolveShortKeysetIds()` fetches the mint's active keysets and maps short IDs to full IDs before processing. All 4 format/keyset combinations now work.

**A/B test results (July 2026, localhost virtual lab, gonuts v0.8.0+):**
- V4 + V1 keyset (`008e808b89acc141`): ✅ **Payment SUCCEEDS** — `kind=1022, allotment=176160768` verified end-to-end via cdk-cli → Debian VM → OpenWrt backend
- V4 + V2 keyset (`01df97b6fb8a572a...`): ✅ **Payment SUCCEEDS** (CDK V2 mint started locally, V2 keyset confirmed). E2E test pending due to VM instability.
- V3 + V1/V2 keyset: ✅ **Full end-to-end success** — `kind=1022`, allotment granted, MAC authorized.

**PRTA regression tests:** `tests/api/test_token_formats.py::test_v4_token_accepted_by_backend` mints V4 tokens via cdk-cli and pays through the backend. `tests/unit/test_helpers.py::TestIsPaymentSwapSucceeded` has 12 unit tests for the helper.

**Switching backends:**

```bash
# Deploy Rust
TOLLGATE_BACKEND=rust ./scripts/deploy-rust-ci.sh experimental '' 192.168.13.112

# Deploy Go
TOLLGATE_BACKEND=go ./scripts/deploy-ci.sh main '' 192.168.13.112

# Run Rust tests
TOLLGATE_BACKEND=rust make pytest-smoke-rust

# Run Go tests
TOLLGATE_BACKEND=go make pytest-smoke
```

Full Rust testing guide: `docs/testing-rust-v1.md`.

## Make-to-Pytest Migration Status

**Hardware lab tests are pytest-first.** Root Makefile targets (`make smoke-degraded`, etc.) are thin stubs that invoke `scripts/pymake.py`. The mapping lives in `config/make-pytest-map.yaml`.

- **44 targets registered** in the live registry
- **40 covered** by pytest/Playwright
- **4 ops-only** (serial shell/status/recovery, arch-test-full delegation)
- **Legacy `mint-health/Makefile`** (~3000 lines) remains for unmigrated targets

The migration is effectively complete for router hardware tests. See `docs/make-to-pytest-migration.md` for the full coverage matrix.

## Key Library Modules

### `lib/router.py` — Router SSH Interaction

The `Router` class is the primary interface to the router. Key methods:

| Method | What it does |
|---|---|
| `ssh(cmd)` | Execute command via SSH, return stdout |
| `scp_to(local, remote)` | Copy file to router (uses `scp -O`) |
| `write_remote_text(path, content)` | Write text file on router via stdin |
| `write_remote_json(path, payload)` | Write JSON file on router |
| `backend_url(path)` | Build URL `http://[::1]:2121{path}` |
| `api_status(path)` | HTTP GET to backend, return status code |
| `pay_direct(token)` | POST Cashu token to `http://[::1]:2121/` |
| `get_session(ip)` | GET `/usage` or `/balance` for client |
| `get_nds_state(mac)` | `ndsctl status` — Authenticated/Preauthenticated/etc |
| `get_tollgate_status()` | `tollgate status` via CLI socket (Go only) |
| `reset_state(mac, adb)` | Deauth client, reset NDS, clear logs |
| `ensure_test_mint()` | Verify `testnut.cashu.exchange` is in config |
| `replace_mints(urls)` | Replace mint URLs in config and restart |
| `fix_nodogsplash_dhcp()` | Patch ndsRTR chain to allow DHCP through |
| `disable_ipv6_on_lan()` | Prevent captive portal bypass via IPv6 RA |

**SSH connection**: Uses control master (`ControlPersist=60`) for reuse. Supports password auth (`sshpass`) and key auth. Jump hosts (`-J`) for virtual lab and offline routers.

### `lib/helpers.py` — Shared Test Utilities

| Function | Purpose |
|---|---|
| `gate_bug_fix(fix_present, *, bug_id, fix_pr)` | Bug regression gate (xfail when fix absent) |
| `pay_and_wait(router, adb, token)` | Pay + wait for auth + verify |
| `assert_session_active(router, ip)` | Assert client has active session |
| `assert_deauthenticated(router, mac)` | Assert client is disconnected |
| `skip_if_no_cli_socket(router)` | Skip if no `/var/run/tollgate.sock` |
| `skip_if_no_luci(router)` | Skip if no LuCI on port 8080 |
| `skip_if_no_sessions_json(router)` | Skip if no persistent sessions |
| `require_client_identity(router)` | Skip if no client IP/MAC |

### `lib/cashu.py` — Token Minting

Three token minters with fallback chain: `HttpMinter` → `CdkCliWallet` → `CashuMint`. `create_minter()` auto-selects the best available.

| Minter | How | Speed (nested KVM) | Dependencies |
|---|---|---|---|
| `HttpMinter` | Direct HTTP NUT-04 + `coincurve` BDHKE crypto | ~1-2s per token | `coincurve>=18.0` (in requirements.txt) |
| `CdkCliWallet` | `cdk-cli` subprocess | ~10-15s per token | `/opt/cdk-mintd/cdk-cli` binary |
| `CashuMint` | `cashu` Python CLI subprocess | ~15-30s per token | `/opt/cashu-venv` Python venv |

`HttpMinter` implements the full Cashu NUT-04 minting flow without subprocess overhead: fetch active keyset → create mint quote (POST `/v1/mint/quote/bolt11`) → wait for FakeWallet auto-payment → mint tokens (POST `/v1/mint/bolt11`) with blinded messages → unblind signatures via secp256k1 point math → serialize V3 token. Works with any Cashu mint (FakeWallet auto-pay, local CDK/Nutshell, or public testnut).

`TokenPool` pre-fills a queue of tokens in parallel (ThreadPoolExecutor, max 5 workers) and replenishes in background threads. Tests call `pool.mint(4)` and get an instant token from the queue.

The `setup-cashu.sh` script patches cashu's `models.py` for a version mismatch (missing `active` field on keysets). Only needed for `CashuMint` — `HttpMinter` and `CdkCliWallet` don't use the Python cashu CLI.

### `lib/deploy.py` — CI Artifact Deployment

Downloads `.ipk` from GitHub Actions and deploys to router. Auto-detects architecture via `opkg print-architecture`. Handles factory reset, service restart, and health verification. Backend-aware — uses `BackendConfig` to select the correct repo/workflow.

### `lib/backend.py` — Backend Configuration

`BackendConfig` encapsulates Go vs Rust differences:

```python
backend = BackendConfig("rust")
backend.repo        # → "Amperstrand/tollgate-rs-ai-research-and-experiments"
backend.workflow    # → "Build and Package"
backend.has_luci    # → False
backend.has_cli_socket  # → False
```

## conftest.py Fixtures

Session-scoped (created once per test run):

| Fixture | Purpose |
|---|---|
| `router` | SSH connection to TollGate router (`Router` instance) |
| `secondary_router` | Optional second router for two-router tests |
| `adb` | Phone control — ADB, MacWiFiClient, LinuxWiFiClient, or ContainerClient |
| `cashu` | Token minter (`HttpMinter` > `CdkCliWallet` > `CashuMint`) for testnet tokens |
| `deploy_session` | Auto-deploy (autouse) — deploys branch/binary before tests |
| `results_dir` | Canonical results directory under `results/` |
| `backend` | `BackendConfig` from `--backend` flag or env |

Per-test fixtures: `connected_wifi`, `test_pricing`, `screenshot_portal`, `screenshot_raw`.

**Auto-deploy behavior** (`deploy_session`, autouse): If `--tollgate-branch` or `--binary` is specified, the fixture deploys before tests run. Skips for unit tests and `--no-deploy`.

## pytest Configuration

Defined in `pytest.ini`. Key markers:

| Marker | Description |
|---|---|
| `smoke` | Quick sanity check (~15s) |
| `critical` | Core functionality (~2min) |
| `extended` | Full suite including edge cases (~10min) |
| `api` | No phone needed |
| `phone` | Requires ADB device |
| `config` | Modifies router config (pricing/metric) |
| `slow` | >30s |
| `go_only` | Skip when `--backend=rust` |
| `rust_only` | Skip when `--backend=go` |
| `hardware` | Physical router scenario, skipped in virtual lab |
| `destructive` | Mutates state beyond normal config toggles |

**Tier hierarchy**: `smoke` ⊂ `critical` ⊂ `extended`. Phone tests auto-get `flaky(reruns=1)` and `timeout(300s)`.

**Timeouts**: Default 60s per test. Phone tests 300s. Hardware scenarios up to 1500s.

## Environment Detection

The framework runs in three environments. Detection is automatic — no manual configuration needed.

### How it works

```
┌─────────────────────────────────────────────────────────────┐
│ Environment Detection Chain                                  │
│                                                              │
│  TOLLGATE_LAB_TYPE env var set?                              │
│       │                                                      │
│       ├── "gcloud"     → Cloud lab (GCP nested-KVM)         │
│       ├── "virtual-lab" → Virtual lab (local QEMU)          │
│       ├── "physical"   → Physical lab (real routers)        │
│       │                                                      │
│       └── (not set)                                          │
│            │                                                 │
│            ├── TOLLGATE_VIRTUAL_LAB=1? → "virtual-lab"      │
│            └── (default) → "physical"                        │
│                                                              │
│  Cloud lab worker sets BOTH:                                 │
│    TOLLGATE_VIRTUAL_LAB=1  (legacy compatibility)           │
│    TOLLGATE_LAB_TYPE=gcloud (explicit)                       │
│                                                              │
│  Virtual lab (scripts/virtual-lab.py) sets:                  │
│    TOLLGATE_VIRTUAL_LAB=1                                    │
│    TOLLGATE_SSH_JUMP_HOST=<jump-host>                        │
│                                                              │
│  Physical lab: neither set → defaults to "physical"          │
└─────────────────────────────────────────────────────────────┘
```

### Markers that gate on environment

| Marker | Effect |
|--------|--------|
| `virtual_lab` | Only runs when `TOLLGATE_VIRTUAL_LAB=1` (cloud + local virtual lab) |
| `virtual_lab_only` | Only runs when `TOLLGATE_LAB_TYPE == "virtual-lab"` (NOT gcloud) |
| `gcloud_only` | Only runs when `TOLLGATE_LAB_TYPE == "gcloud"` |
| `physical_only` | Only runs when `TOLLGATE_LAB_TYPE == "physical"` |
| `physical_hardware` | Requires physical router/radio behavior |
| `hardware` | Auto-added to `tests/scenarios/*`; skipped in virtual lab unless also `virtual_lab` |
| `requires_wifi` | Skipped with `--client=container` (no WiFi adapter) |
| `android_only` | Skipped without physical Android device |

### Per-environment behavior

| Feature | Physical Lab | Virtual Lab | Cloud Lab |
|---------|-------------|-------------|-----------|
| Router access | SSH direct | SSH via jump host | SSH to QEMU VM |
| Client type | ADB / Mac / Linux | Container (Debian QEMU) | Container (Debian QEMU) |
| WiFi | Real radios | None by default | hwsim (opt-in with `--hwsim`) |
| Phone tests | Yes (ADB) | No | No |
| LuCI Playwright | Yes | No | No |
| Local mints | Public testnut | Optional | 3 local mints (CDK + Nutshell) |
| Mints | `testnut.cashu.exchange` | Configurable | Auto-selected (V2 probe → V1 fallback) |
| Auto-deploy | Manual / `--tollgate-branch` | Manual | CI artifact (auto) |
| Results publish | Manual `--publish` | Manual | `--publish` (fire-and-forget) |

### Environment-specific env vars

**All environments** read `TOLLGATE_SSH_HOST`, `TOLLGATE_SSH_PASSWORD`, `TOLLGATE_LUCI_PASSWORD`, `TOLLGATE_BACKEND`.

**Cloud lab worker** writes to `.env` (via `write_env_file()`):
- `TOLLGATE_VIRTUAL_LAB=1`, `TOLLGATE_LAB_TYPE=gcloud`
- `TOLLGATE_CLIENT_TYPE=container`, `TOLLGATE_CONTAINER_HOST=10.99.99.100`
- `TOLLGATE_SSH_HOST=10.99.99.1`, `TOLLGATE_CASHU_VENV=/opt/cashu-venv`
- `TOLLGATE_ENABLE_HWSIM=1` (only when `--hwsim` flag passed)
- `TOLLGATE_ENABLE_RESELLER_SCENARIOS=1` (only when `--reseller-scenarios` passed)

**Virtual lab** (`scripts/virtual-lab.py start-poc`) sets:
- `TOLLGATE_VIRTUAL_LAB=1`, `TOLLGATE_SSH_JUMP_HOST`
- `TOLLGATE_CLIENT_TYPE=container`

**Physical lab**: just `TOLLGATE_SSH_HOST` + `TOLLGATE_LUCI_PASSWORD`.

## Lessons Learned

### GitHub org-Actions freeze: billing hold, not policy (2026-08-27 forensics)

**Symptom**: workflow dispatch 422s with "Actions has been disabled for this
repository" while org AND repo Actions policies read `enabled/all`, workflows
read `active`, and queued runs sit `queued` forever.

**Root cause here**: an unpaid net billing balance — $14.68 of Actions storage
overage (May 2026, `tollgate-release-explorer-site`, 43.7k GB-hours from
artifact volume) — on a free org with no payment method. Enforcement swept
during GitHub's platform billing incident (status page, Aug 26–27). Public-repo
minutes are free; artifact STORAGE is metered past ~500 MB.

**Diagnosis chain** (gh token needs `-s admin:org` for the org endpoints;
refresh via `gh auth refresh -h github.com -s admin:org` — device-code flow):
1. `gh api /user/memberships/orgs/OpenTollGate` → confirm role.
2. `gh api /orgs/OpenTollGate/actions/permissions` + repo-level → both green
   rules out policy.
3. `gh api "/orgs/OpenTollGate/settings/billing/usage"` → per-repo usage items
   with `grossAmount`/`discountAmount`/`netAmount` — any nonzero net = the hold.
   (Free orgs get NO audit log — that API 404s; don't chase it.)

**Fix**: org owner pays the balance (Billing & plans → payment method) and sets
org artifact retention to ≤7 days. Per-workflow `retention-days` is belt to
that (ci.yml/nut-auditor.yml carry `retention-days: 3`).

**Posture adopted**: GH-side scheduled noise minimized (reaper daily; vm-reaper
dispatch-only), local cron is the fast reaper, every cloud-lab VM self-destructs,
and local `cloud-lab.py submit` runs without GitHub at all. A recovery tripwire
lives in crontab (daily 08:12 UTC dispatch probe → `~/.cache/org-actions-watch.log`).
Upstream trigger-hygiene + SDK-caching PRs #369/#370 and the SDK audit #371
track the remaining build-matrix work.


### `chpasswd` does not exist on OpenWrt BusyBox

OpenWrt's BusyBox does not ship with `chpasswd`. Attempting `echo 'root:pw' | chpasswd` in a uci-defaults script will fail. Use `printf '%s\n%s\n' 'pw' 'pw' | passwd root` instead.

**Impact**: When this failed in our uci-defaults script with `set -eu`, the entire script aborted. The SSH key was written (it came before chpasswd) but the WAN firewall rule never got added. The router was unreachable from WAN until we connected via LAN.

### Never use `set -eu` in uci-defaults scripts

uci-defaults scripts run once on first boot. If you use `set -eu`, any single command failure aborts the entire script. Later commands (like firewall rules) never execute. This can leave the router in an unreachable state.

Instead: let each command run independently. If a command might fail, handle it explicitly. The `exit 0` at the end is what tells OpenWrt to delete the script — it must always be reached.

### SCP requires `-O` flag for OpenWrt

OpenWrt's BusyBox does not include `sftp-server`. Modern OpenSSH defaults to SFTP for SCP transfers, which fails with `ash: /usr/libexec/sftp-server: not found`. Always use `scp -O` (legacy SCP protocol) when copying files to OpenWrt routers.

### The ASU API blocks Python's default User-Agent

The OpenWrt ASU (Attended Sysupgrade) server at `sysupgrade.openwrt.org` returns HTTP 403 for requests with the default `Python-urllib/3.x` User-Agent. Set a custom `User-Agent` header (e.g., `tollgate-build-firmware/1.0`) on all requests to the ASU API.

### Don't accidentally modify the main router

When testing router access from a machine that also has SSH access to the upstream/main router, be extremely careful with IP addresses. Commands like `passwd` or `chpasswd` run without confirmation. Always verify which host you're SSH'd into before running destructive commands.

### Go wallet (gonuts) vs CDK Keyset ID V1/V2 — resolved

V1 keyset IDs: `00`-prefix, 16 hex chars (8 bytes), e.g. `0016f5fb5e5278f2`.
V2 keyset IDs: `01`-prefix, 66 hex chars (33 bytes), e.g. `01df97b6fb8a572a718d7df7fcbf4387e2d455134ea8004c9c8c51e1b3391f909e`.
V2 spec (NUT-02 PR #182, merged Jan 2026): `01` + SHA256(`amount:pubkey_hex` pairs sorted, comma-separated, `|unit:sat`). V1: `00` + first 14 hex chars of SHA256(concat of raw pubkeys).

**Previous belief (STALE)**: Configuring the Go backend with a CDK V2 mint causes a FATAL crash on startup. Believed to require the `Amperstrand/gonuts-tollgate` fork at `feature/v2-keyset-ids` and `scripts/patch-gonuts-version.sh` to work around.

**Actual resolution (June 2026)**: The crash was NOT a V2 keyset issue — it was a multi-mint wallet registration bug in `tollgate-module-basic-go`. [Issue #176](https://github.com/OpenTollGate/tollgate-module-basic-go/issues/176) verified that V2 keyset swap WORKS correctly after [PR #167](https://github.com/OpenTollGate/tollgate-module-basic-go/pull/167) ("register all accepted mints in wallet"). The `Amperstrand/gonuts-tollgate` V2 fork is no longer needed. `scripts/patch-gonuts-version.sh` is orphaned (exists but no longer called by `deploy.sh`).

**Current state (July 2026)**: The Go backend (`tollgate-module-basic-go`) uses `OpenTollGate/gonuts-tollgate v0.7.1` (via `replace` directive in go.mod). The backend **fully supports** both V1 and V2 keysets — verified end-to-end on localhost virtual lab (July 2026):
- V1 keyset payment (testnut.cashu.exchange): `kind=1022, allotment=66060288 bytes` ✅
- V2 keyset payment (local CDK V2 mint, keyset `01df97b6fb8a...`): `kind=1022, allotment=88080384 bytes` ✅

The #176 resolution (PR #167) fixed multi-mint startup registration (the original "fatal crash"). V2 keyset token verification works correctly with `OpenTollGate/gonuts-tollgate v0.7.1`.

**Upstream status**: `elnosh/gonuts` (the original) is dead — last commit August 2025, no V2 support, no activity. `Origami74/gonuts-tollgate` is the active fork currently used by the Go backend. Long-term, the Rust backend (`tollgate-rs`, uses CDK natively) is the migration path.

### Nodogsplash DHCP bypass required

Nodogsplash's `ndsRTR` iptables chain drops ALL unauthenticated packets (mark 0x10000) at rule 1, which silently kills DHCP DISCOVER from clients. Without the bypass fix (in `Router.fix_nodogsplash_dhcp()`), phones can associate at L2 but never get an IP — Android shows "Connection failed" and auto-reconnects to a known-good network.

### IPv6 captive portal bypass

Nodogsplash only manages IPv4 iptables. If IPv6 Router Advertisements are active on LAN, WiFi clients get global IPv6 addresses and Android validates connectivity over IPv6, completely bypassing the captive portal. `Router.disable_ipv6_on_lan()` disables RA, DHCPv6, and removes the LAN IPv6 prefix.

### Gatewayport 80 vs 2050 conflict

Nodogsplash's default `gatewayport` is `2050` (hardcoded in `src/conf.h`). The TollGate `.ipk` includes `/etc/uci-defaults/99-tollgate-setup` which overrides it to `80`. The cloud lab pipeline (`pipeline.py:_fix_nodogsplash_gatewayport()`) force-corrects it back to `2050` after every deploy via `uci set nodogsplash.@nodogsplash[0].gatewayport=2050`.

**Why this matters**: The captive portal CGI scripts in `/usr/lib/nodogsplash/` are configured to listen on port 2050. If the port is 80, NDS's own HTTP server clashes with uhttpd/LuCI and the portal breaks silently — clients get LuCI HTML instead of the captive portal page.

**Current state**: The pipeline fix is a band-aid. The real fix requires changing `99-tollgate-setup` in the backend package to use port 2050 instead of 80. Tracked as [issue #32](https://github.com/OpenTollGate/tollgate-module-basic-go/issues/32) (closed).

### Offline router deployment (no internet, no opkg update)

When a router has no internet (e.g., downstream/reseller behind a jump host), `opkg update` and `opkg install` will fail. You must manually SCP all packages and their dependencies.

**TollGate's declared dependencies** (from the Makefile `DEPENDS`):
- `nodogsplash`, `luci`, `jq`, `px5g-mbedtls`

**Test framework dependencies** (from `lib/deploy.py` `TEST_DEPS`):
- `curl`, `socat`, `nodogsplash`, `jq`, `luci`, `px5g-mbedtls`

Combined, these require ~52 packages (including transitive deps like `iptables-nft`, `kmod-*`, `rpcd`, `uhttpd`, `libluciheader0`, etc.).

**Procedure:**

1. **Get the package diff** — compare a fresh OpenWrt install against a router that has TollGate + deps installed:
   ```bash
   # Fresh router (offline)
   ssh -J <jump-host> root@<offline-router> "opkg list-installed" | sort > /tmp/fresh-pkgs.txt
   # Router with internet + TollGate
   ssh root@<online-router> "opkg list-installed" | sort > /tmp/full-pkgs.txt
   # Diff
   comm -13 /tmp/fresh-pkgs.txt /tmp/full-pkgs.txt | grep -v "tollgate-wrt" | awk '{print $1}'
   ```

2. **Download all deps on the online router:**
   ```bash
   ssh root@<online-router> "mkdir -p /tmp/deps && cd /tmp/deps && \
     for pkg in <package-list>; do opkg download \$pkg; done"
   ssh root@<online-router> "cd /tmp/deps && tar czf /tmp/tollgate-deps.tar.gz *.ipk"
   ```

3. **Relay to Mac, then to offline router through jump host:**
   ```bash
   scp -O root@<online-router>:/tmp/tollgate-deps.tar.gz /tmp/tollgate-deps.tar.gz
   scp -O -J <jump-host> /tmp/tollgate-deps.tar.gz /tmp/tollgate-build/tollgate-wrt-*.ipk \
     root@<offline-router>:/tmp/
   ```

4. **Install on offline router:**
   ```bash
   ssh -J <jump-host> root@<offline-router> "
     cd /tmp && mkdir -p deps && cd deps && tar xzf ../tollgate-deps.tar.gz
     opkg install /tmp/deps/*.ipk
     opkg install --force-overwrite /tmp/tollgate-wrt-*.ipk
   "
   ```

**Note:** Always use `scp -O` for OpenWrt (no sftp-server). The total dependency bundle for mipsel_24kc is ~1.8MB. The TollGate ipk itself is ~6.3MB.

### gonuts-tollgate dependency chain (changed)

The Go backend's wallet dependency is declared as `Origami74/gonuts-tollgate v0.6.1` but a `replace` directive in `go.mod` overrides it to `OpenTollGate/gonuts-tollgate v0.7.1` at build time. The previous `Amperstrand/gonuts-tollgate feature/v2-keyset-ids` branch (which added bolt11 decode tolerance for FakeWallet mints + V2 keyset IDs) is no longer used. `scripts/patch-gonuts-version.sh` is orphaned (exists, not called by `deploy.sh`). Tracked as [issue #156](https://github.com/OpenTollGate/tollgate-module-basic-go/issues/156).

### Lightning invoice flow and testnut bolt11 limitation

**The `/ln-invoice` endpoint** was added to tollgate-module-basic-go for Lightning payment support through the captive portal. The flow:

1. Portal calls `POST /ln-invoice` with `{amount, mint_url}` → `main.go:handleLightningInvoicePost()`
2. Backend calls `Merchant.RequestLightningInvoice(mac, mintURL, amount)` → `merchant/lightning.go`
3. Calls `tollwallet.RequestMintQuote(amount, mintURL)` → gonuts `RequestMint()` → NUT-04 on mint
4. Mint returns `{request: "...", quote: "...", state: UNPAID}`
5. Backend stores the quote, starts `monitorLightningQuote()` goroutine
6. Returns `{invoice: request, quote, amount, expiry}` to portal
7. Portal decodes the invoice as bolt11 for QR code display
8. Monitoring goroutine polls `MintQuoteState()` every 2s, detects payment, mints tokens, grants access

**testnut.cashu.exchange returns a dummy string, not bolt11**:

> **Note (July 2026)**: `testnut.cashu.space` is currently unreachable (HTTP 000 / connection refused). Only `testnut.cashu.exchange` is operational. The `.space` domain was previously the recommended fallback for valid bolt11 invoices but is no longer available.

```
testnut.cashu.exchange → "dummy-mint-4-46876457c0684c65d07e993705706d7b84c528aa75be1c722b8970f37585c7ba-exp1780177644"
testnut.cashu.space    → "lnbc40n1p4pkkhl9qypqqqdqqxqrrsssp5recjpm6q6q8dnjqh04gpqn5zt8hfrhrtkdwtpwynnenjnjtm5hkq..."
```

`testnut.cashu.exchange` returns a placeholder string (`dummy-mint-{amount}-{hash}-exp{timestamp}`) that is NOT a bolt11 invoice. `testnut.cashu.space` returns a proper signed bolt11 invoice. Both CDK FakeWallet and Nutshell FakeWallet have proper `InvoiceBuilder` implementations — the `.exchange` domain appears to run a different or customized version.

**Impact on testing**:
- The gonuts tolerant fix (above) allows the quote to be created and stored despite the invalid `request`
- The monitoring goroutine works correctly — FakeWallet auto-pays, tokens get minted, access is granted
- The portal cannot decode the dummy string as bolt11 → Lightning tab shows error
- Cashu token payment flow is unaffected
- This is ONLY an issue with `testnut.cashu.exchange`. Production mints and `testnut.cashu.space` return valid bolt11

**Workaround for Lightning simulation with testnut**: Either switch to `testnut.cashu.space` (returns real bolt11), or add invoice validation in `merchant/lightning.go:RequestLightningInvoice()` that detects non-bolt11 and returns an empty invoice string, letting the portal enter "auto-pay polling" mode instead of trying to display a QR code.

**The "amount 0 sats" log error is NOT from `/ln-invoice`**: The backend log message `"Error getting invoice: amount 0 sats is outside allowed range (1000-1000000000 msats)"` comes from `MeltToLightning()` → `GetInvoiceFromLightningAddress()` in the **profit-share payout routine**, which runs on a 60-second timer. It fires when `aimedPaymentAmount * factor` rounds to 0 via integer math. This is a separate issue (filed as physical-router-test-automation#25) — the fix is a zero-amount guard in `processPayout()` before calling `MeltToLightning`. Do not confuse this error with the `/ln-invoice` flow.

### TollGate init script uses `/usr/bin/`, not `/usr/sbin/`

The tollgate-wrt init script (`/etc/init.d/tollgate-wrt`) runs the binary from `/usr/bin/tollgate-wrt`, NOT `/usr/sbin/tollgate-wrt`. If you SCP a custom binary, make sure you copy it to the correct path:

```bash
# CORRECT
scp -O tollgate-wrt root@router:/tmp/tollgate-wrt
ssh root@router "cp /tmp/tollgate-wrt /usr/bin/tollgate-wrt && /etc/init.d/tollgate-wrt restart"

# WRONG — binary won't be picked up by the service
scp -O tollgate-wrt root@router:/usr/sbin/tollgate-wrt
```

The opkg package installs to `/usr/bin/tollgate-wrt`. The `/usr/sbin/` path may have a stale copy from a previous manual deploy.

## Router Access Patterns

### GL-MT3000 Default IPs

| Mode | IP | Access |
|---|---|---|
| OpenWrt factory defaults (LAN) | 192.168.1.1 | SSH (no password), HTTP (LuCI if installed) |
| GL.iNet stock firmware (LAN) | 192.168.8.1 | HTTP admin panel |
| WAN (DHCP from upstream) | Assigned by DHCP | SSH only if WAN firewall rule exists |
| U-Boot recovery | 192.168.1.1 | HTTP only (web UI for firmware upload) |

### SSH Authentication

The test framework uses `sshpass` (password auth) via `TOLLGATE_LUCI_PASSWORD`. Custom firmware images built with `scripts/build-firmware.py` embed both an SSH key and a random password. Both methods work.

### Network Topology

```
Internet → Main Router (192.168.13.1) → Switch → TollGate Router WAN (192.168.13.112)
                                                ↕
                                           Test Machine (192.168.13.244)
```

When connected directly to the TollGate router's LAN port, the test machine gets an IP in 192.168.1.0/24 (en6 on current setup).

## Primary Test Workflow

**PR / CI (no physical hardware):**

```bash
./scripts/test-pr.sh --pr <N> [--reset] [--test api|all] [--publish]
```

**Physical lab (pytest-first, Make stubs forward to pymake):**

```bash
make lock PHASE='smoke-degraded'
./scripts/pymake.py smoke-degraded --router alpha
# equivalent: make smoke-degraded ROUTER=alpha
make pytest-scenarios   # all tests/scenarios/ with -m hardware
```

Registry: `config/make-pytest-map.yaml`. Migration notes: `docs/make-to-pytest-migration.md`.

`test-pr.sh` resolves the PR to a branch/commit, deploys to the router, runs tests, and generates reports. See README.md for full usage.

## Firmware Build + Flash Workflow

### Build

```bash
scripts/build-firmware.py --router lab-router-a
```

Reads `config/routers.json` for target/profile/version. Auto-detects SSH key from `~/.ssh/`. Generates random password. Builds via ASU API. Downloads sysupgrade image. Saves credentials to `credentials/<router-id>.json`.

### Flash via SSH (LAN or WAN)

```bash
scripts/build-firmware.py --router lab-router-a --flash
```

Or manually:

```bash
scp -O <image.bin> root@<router-ip>:/tmp/
ssh root@<router-ip> "sysupgrade -n /tmp/<image.bin>"
```

`sysupgrade -n` wipes all config. The router reboots. SSH connection dies with exit code 246 (expected).

### Flash via U-Boot (recovery)

For bricked routers that won't boot properly. See the U-Boot section below.

## U-Boot Recovery

### Entering U-Boot Mode (GL-MT3000)

1. Disconnect power from router
2. Connect computer to router's **LAN port** via Ethernet (leave WAN disconnected)
3. Set computer IP to 192.168.1.x (e.g., 192.168.1.2, subnet 255.255.255.0)
4. **Press and hold the Reset button**
5. **While holding Reset, apply power**
6. Watch the LED: blue flashes ~6 times, then turns **solid white**
7. **Release Reset** when LED is solid white
8. U-Boot web UI is now at `http://192.168.1.1`

### Headless Upload via curl

```bash
curl -X POST -F gl_firmware=@<firmware.bin> http://192.168.1.1/index.html
```

The form field name is `gl_firmware`. Wait ~3 minutes. Don't power off. The router reboots automatically.

### Automated Recovery Script

```bash
scripts/uboot-recover.py --image <firmware.bin> [--interface en6]
```

Uses macOS `say` command for voice guidance. Auto-detects U-Boot mode via ping. Uploads firmware via curl. Monitors for reboot completion.

### Browser-Based Recovery (fallback)

Use Chrome or Edge (NOT Firefox — may brick the router). Visit `http://192.168.1.1` in U-Boot mode. Upload firmware via the web form. Wait ~3 minutes.

## Common Recovery Scenarios

### Router boots but no SSH from WAN

Likely: WAN firewall rule missing. Connect via LAN (192.168.1.1) and add the rule:

```bash
uci add firewall rule
uci set firewall.@rule[-1].name='Allow-SSH-WAN'
uci set firewall.@rule[-1].src='wan'
uci set firewall.@rule[-1].dest_port='22'
uci set firewall.@rule[-1].proto='tcp'
uci set firewall.@rule[-1].target='ACCEPT'
uci commit firewall
fw4 restart
```

### uci-defaults script didn't run / partially ran

Check if the script still exists:

```bash
ls /etc/uci-defaults/
```

If `99-asu-defaults` is still there, it failed partway through. Read it, fix the issue, run the remaining commands manually, then delete it.

### Router not getting WAN IP

After `sysupgrade -n`, the WAN port is configured for DHCP by default. Check that the upstream network is providing DHCP. Verify with `ping 192.168.13.1` from the router.

## GCP cloud lab (fire-and-forget) — DEPRECATED

> **Superseded by SHC** (default provider, ~$0.01/run vs ~$0.10/run, Zone 4 reachable from Europe; note Zone 7/Dev VPS is still unreachable). No GCP runner snapshot is baked — `submit`/`up` fail fast via `ensure_runner_snapshot()` in `lib/cloud_lab/gcp.py`. To revive: `scripts/bake-snapshot.py` + update `SNAPSHOT_NAME`. Reaped-VM cost-hygiene notes below still apply in spirit to SHC (`scripts/cost-status.py` audits it).

**Deprecated section kept for reference.** To revive GCP: run `scripts/bake-snapshot.py`, update `SNAPSHOT_NAME` in `lib/cloud_lab/constants.py`, and remove the guard.

### Cost Policy

**Match the machine type to the workload.** Most local dry tests (API, Playwright) do NOT need nested KVM and run fine on cheap E2 instances. Only OpenWrt QEMU VM tests (NDS gating, virtual lab) require nested KVM (N2 series).

| Workload | Needs nested KVM? | Machine Type | $/hr | $/day |
|---|---|---|---|---|
| Local dry tests (`local-test.sh`) | No | `e2-medium` | $0.033 | $0.80 |
| API + Playwright via `gcp-dry-test.sh` | No | `e2-medium` | $0.033 | $0.80 |
| OpenWrt VM tests (NDS gating, `start-poc`) | Yes | `n2-standard-2` | $0.097 | $2.33 |
| Full cloud lab (`cloud-lab.py submit`) | Yes | `n2-standard-2` | $0.097 | $2.33 |

**Forbidden without explicit user approval:** any machine type >2 vCPU (n2-standard-4+ at $0.19+/hr). The 2-vCPU types are sufficient for all test workloads observed.

**E2 vs N2:** E2 (AMD Rome) does not support nested KVM. N2 (Intel Cascade Lake) does. If a test fails with "could not access /dev/kvm" on E2, switch to n2-standard-2 — the test needs nested virtualization.

**Region priority (cheapest first):**

| Tier | Regions | Use When |
|---|---|---|
| Tier 1 (cheapest) | `us-central1`, `us-east1`, `us-east5`, `us-west1` | Default |
| Tier 2 (+10%) | `northamerica-northeast1/2`, `europe-west1`, `europe-west4` | Tier 1 exhausted |

The fallback logic in `_create_vm_with_fallback()` tries all Tier 1 zones, then Tier 2. **Never fall back to regions >20% more expensive** without asking the user first.

### Auto-Shutdown (MANDATORY)

**Never leave a GCP VM running unattended.** Every VM must have a shutdown plan:

1. **Fire-and-forget workers** (`cloud-lab.py submit`): The startup script self-deletes the VM after tests complete. A 90-minute hard timeout in the script ensures no runaway costs.

2. **Manual/interactive sessions**: Always stop the VM when done:
   ```bash
   gcloud compute instances stop <vm-name> --zone=<zone>
   ```

3. **Safety net — cron-based auto-shutdown**: On any GCP VM used for interactive testing, install a 2-hour auto-shutdown timer at boot:
   ```bash
   echo "shutdown -P now" | at now + 2 hours
   ```

4. **VM sweeper cron job** (host-side safety net): Install `scripts/gcp-vm-sweeper.sh` as a cron job on your local machine. It checks GCP every 15 minutes and stops any VM running longer than the threshold (default: 2 hours):
   ```bash
   # Install (one-time)
   ./scripts/gcp-install-sweeper-cron.sh

   # Dry-run first to see what it would do
   ./scripts/gcp-vm-sweeper.sh --dry-run

   # Custom threshold
   TOLLGATE_SWEEPER_HOURS=4 ./scripts/gcp-install-sweeper-cron.sh
   ```

5. **Stale VM cleanup**: Before starting work, check for orphaned VMs:
   ```bash
   gcloud compute instances list
   # Stop or delete any VMs you don't recognize
   ```

### Budget alerts (manual setup)

The GCP service account (`tollgate-ci-runner`) cannot access the Billing API. Set up budget alerts manually:

1. Go to [GCP Console → Billing → Budgets & alerts](https://console.cloud.google.com/billing/_/budgets)
2. Create a budget for project `tollgate-test-lab`
3. Set threshold: **$10/month** (alert at 50%, 90%, 100%)
4. Connect to email/Slack notification channel

### Quick GCP dry test runner

For API + Playwright tests (no nested KVM needed), use `scripts/gcp-dry-test.sh`:

```bash
# Spin up e2-medium from snapshot, run tests, auto-shutdown
./scripts/gcp-dry-test.sh

# With Playwright (starts Vite + runs browser tests too)
./scripts/gcp-dry-test.sh --playwright

# Use n2-standard-2 for nested KVM tests (OpenWrt VM)
./scripts/gcp-dry-test.sh --nested-kvm

# Keep VM running after tests (for debugging)
./scripts/gcp-dry-test.sh --keep-running
```

This script creates the cheapest possible VM, runs tests, and shuts down automatically. The 2-hour safety net ensures no runaway costs even if the script itself crashes.

### Snapshot management

Current snapshot: **none** (v17 was lost during cleanup). To re-bake from a fresh e2-medium VM:

```bash
# 1. Create VM from scratch
gcloud compute instances create tollgate-bake --zone=us-east1-b --machine-type=e2-medium --image-family=ubuntu-2204-lts

# 2. Bootstrap (SSH in and run):
#    - Install Go 1.23, Node.js 20, Python venv with pytest+coincurve
#    - Clone repos, npm install, playwright install chromium
#    - sudo pip install requests httpx into venv

# 3. Create snapshot
gcloud compute snapshots create tollgate-runner-v18 \
  --source-disk=tollgate-bake --source-disk-zone=us-east1-b

# 4. Delete bake VM
gcloud compute instances delete tollgate-bake --zone=us-east1-b --delete-disks=all
```

Clean up old snapshots before creating new ones — each snapshot is ~50GB and incurs ~$1/month storage:

```bash
gcloud compute snapshots list
# Delete old snapshots
gcloud compute snapshots delete <old-snapshot-name> --quiet
```

Update `SNAPSHOT_NAME` in `lib/cloud_lab/constants.py` after verifying a new snapshot works end-to-end.

### Architecture

```
┌─ GCP Host VM (n1-standard-2, nested KVM) ──────────────────────┐
│                                                                  │
│  tg-poc-br (10.99.99.0/24) — test LAN                           │
│    ├── host: 10.99.99.2 (mints, NAT, orchestration)            │
│    ├── alpha: 10.99.99.1 (OpenWrt QEMU, TollGate under test)   │
│    └── debian: 10.99.99.100 (Debian QEMU, Playwright, cashu)   │
│                                                                  │
│  tg-beta-br (10.99.96.0/24) — isolated Beta LAN (2-router)     │
│    ├── host: 10.99.96.2 (routes to mint at 10.99.99.2)         │
│    └── beta: 10.99.96.11 (upstream TollGate merchant)           │
│                                                                  │
│  tg-upstream-br (10.99.98.0/24) — simulated WAN (2-router)     │
│    ├── alpha WAN: DHCP from beta                                │
│    └── beta WAN: 10.99.98.1 (static, DHCP server)              │
│                                                                  │
│  mgmt-br (10.99.97.0/24) — management SSH                      │
│    ├── host: 10.99.97.2                                         │
│    ├── alpha: 10.99.97.1                                        │
│    ├── beta: 10.99.97.11                                        │
│    └── debian: 10.99.97.100                                     │
│                                                                  │
│  Local mints (on host):                                         │
│    ├── CDK V2:      :8383 (V2 keysets, 01-prefix)              │
│    ├── Nutshell V2:  :8384 (V2 keysets)                         │
│    └── Nutshell V1:  :8385 (V1 keysets, 00-prefix, for Go)     │
│                                                                  │
│  OpenWrt VM (alpha):                                            │
│    ├── hwsim radios: kmod-mac80211-hwsim (pre-installed in v8)  │
│    ├── wpad-basic, iw-full, iwinfo (pre-installed in v8)        │
│    ├── radio0: 2.4GHz AP (phy0-ap0, SSID=TollGate, br-lan)     │
│    ├── radio1: 5GHz AP (phy1-ap0, SSID=TollGate, br-lan)       │
│    ├── nodogsplash: manages br-lan (captive portal on :2050)    │
│    └── /etc/hosts maps mint DNS → 10.99.99.2                   │
│                                                                  │
│  Test flow (worker.py):                                         │
│    [1] Boot GCP VM from snapshot                                │
│    [2] Clone test repo, install deps (baked)                    │
│    [3] Boot OpenWrt + Debian QEMU VMs                           │
│    [4] Setup hwsim virtual WiFi (2 radios, AP on br-lan)        │
│    [5] Start 3 local mints (CDK + Nutshell V1 + V2)            │
│    [6] Deploy TollGate .ipk to OpenWrt                          │
│    [7] Select mint (CDK V2 if backend supports it, else V1)     │
│    [7.5] If --two-router: configure Beta merchant + Alpha reseller + fund wallet
│    [8] Run tests: visual → API → vl-scenarios → scenarios       │
│    [9] Collect results, publish to Nostr+Blossom (kind 30078), self-delete        │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### Local mint selection strategy

The worker runs 3 local Cashu mints to eliminate dependency on the public `testnut.cashu.exchange`. `select_test_mint()` probes the backend:

1. **Try CDK V2** (`http://10.99.99.2:8383`) — configure backend with V2 mint URL, check if backend returns `kind 10021` with `price_per_step` tags. If yes → V2 supported, use CDK.
2. **Fall back to Nutshell V1** (`http://10.99.99.2:8385`) — retries up to 10 times (30s). Checks for V1 keyset ID (`00`-prefix, 16 chars). Go backend (`gonuts`) requires V1 keysets.
3. **Last resort: public testnut** — only if local mints are genuinely broken.

The `.env` file's `TOLLGATE_TEST_MINT_URL` is updated to the chosen mint URL so the cashu CLI in tests uses the local mint.

### Test runners

Tests run sequentially in a single shell command with a 90-minute timeout:

| Runner | Tests | Typical time |
|--------|-------|-------------|
| **visual** | Container client e2e portal payment | ~2min |
| **api** | `tests/api/` (89 tests with local mint) | ~15min |
| **vl-scenarios** | Captive portal browser, mint health, boot hygiene, upstream WiFi | ~2min |
| **scenarios** | Reseller mode (only with `--reseller-scenarios`) | ~3min |
| **two-router** | Two-router cloud + upstream payment (only with `--two-router`) | ~5min |

Each runner writes junit.xml + report.html to `raw/<runner>/`. The overall exit code is the worst of all runners.

### Flow

1. **Local (blocking):** `ensure_artifact()` waits for upstream CI to finish and expose an `x86_64` `.ipk` (never triggers new builds).
2. **GCP VM (async):** startup script clones this repo, runs `lib.cloud_lab.worker`, publishes to Nostr+Blossom, self-deletes.
3. **Publishing:** `publish-report.sh` uses non-force pushes with up to 10 pull/rebase/push retries and random 0-60s backoff so multiple cloud runs can publish concurrently.

### Secrets

| Variable | Purpose |
|----------|---------|
| `GH_TOKEN` or `GITHUB_TOKEN` | Passed to VM metadata for `gh` artifact download, PR comments (no longer used for gh-pages push — pipeline publishes via Nostr+Blossom) |
| `TOLLGATE_GCP_SSH_KEY` | SSH key for `gcloud compute ssh` / debugging (default `~/.ssh/google_compute_engine`) |

`GH_TOKEN` in instance metadata is acceptable for a private lab; prefer Secret Manager for shared projects.

### Debian overlay caching

- **Debian qcow2 overlay** (Playwright + Chromium) lives on the baked snapshot — do **not** reset it per run.
- **OpenWrt overlay** is recreated from base each run for a clean TollGate install.
- **Persistent caches** live under `/opt` (`/opt/tollgate-venv`, `/opt/cashu-venv`, `/opt/cdk-mintd`). Avoid `/tmp` for baked caches because it may be empty after boot.
- Re-bake snapshot when Debian packages, Playwright versions, gh/gcloud CLI, Python deps, cashu, CDK, or OpenWrt provisioning logic change.

### Snapshot baking

Use `scripts/bake-snapshot.py` to create a new snapshot with all deps pre-installed and the OpenWrt base image pre-provisioned (SSH, password, firewall, network already configured).

```bash
./scripts/bake-snapshot.py bake
```

What it bakes into the snapshot:
- `gh` CLI (GitHub apt repo)
- `gcloud` CLI (Google Cloud apt repo, for VM self-delete)
- `/opt/tollgate-venv` (Python venv with pytest, playwright, etc.)
- `/opt/cashu-venv` (cashu CLI with active-field patch)
- `/opt/cdk-mintd` (CDK mintd binary for local V2 mint)
- Pre-provisioned `openwrt-base.qcow2` (SSH enabled, password set, firewall rule added, network configured to 10.99.99.1)
- **WiFi packages** (v8+): `kmod-mac80211-hwsim`, `wpad-basic`, `iw-full`, `iwinfo` for virtual radio testing

The baker must run remote setup with `HOME=/root`, because the GCP startup worker also exports `HOME=/root`. If bake commands accidentally write to `/home/<ssh-user>/tollgate-virtual-lab`, the worker will read stale images from `/root/tollgate-virtual-lab`.

After baking, verify the snapshot with a throwaway cloud run or `cloud-lab.py up` before updating `SNAPSHOT_NAME` in `lib/cloud_lab/constants.py` to the new snapshot name (auto-incremented, e.g. `tollgate-runner-baked-v17`).

The worker (`lib/cloud_lab/worker.py`) detects pre-provisioned OpenWrt bases automatically — if SSH works within 15s of boot, serial provisioning is skipped. Falls back to serial provisioning for old snapshots without pre-provisioned bases.

### Commands

```bash
./scripts/cloud-lab.py submit --pr 42 --publish
./scripts/cloud-lab.py status-run --run-id <id>
./scripts/cloud-lab.py cleanup-stale   # delete RUNNING tollgate VMs >2h old
./scripts/cloud-lab.py cleanup-all      # delete ALL tollgate VMs
./scripts/bake-snapshot.py bake         # create new snapshot with deps pre-installed
```

### Timing (v9 snapshot with local mints)

| Phase | Duration | Notes |
|---|---|---|
| VM boot + startup | ~2m | GCP startup script overhead |
| gh + venv + cashu + cdk | 0s (baked) | Pre-installed in snapshot |
| Boot OpenWrt + Debian VMs | ~30s | OpenWrt SSH-first detection, no serial |
| Start local mints | ~5s | CDK + Nutshell V1 + V2, health checked |
| Deploy TollGate | ~50s | Download + install .ipk |
| Select test mint | ~5s | V2 probe, then V1 fallback |
| Run tests | ~20m | 94 tests with local mint (no timeouts) |
| Collect + publish | ~2m | Blossom upload + Nostr event publish |
| **Total (single-router)** | **~30min** | |
| **Total (two-router)** | **~50min** | Extra Beta VM + serial provisioning + deploy |

### VM lifecycle and lease-based kill switch

Cloud lab VMs use a three-layer safety mechanism:

1. **Worker `MAX_WALL_SECONDS`** (7200s / 2h): The Python worker force-deletes the VM if the pipeline runs longer than 2 hours. This handles hung test suites, stuck SSH sessions, or runaway processes.

2. **Lease-based kill switch**: A background process inside the VM polls `tollgate-delete-at` metadata every 60s. When the epoch timestamp is reached, the VM self-deletes via `gcloud compute instances delete`. The host can extend the lease or trigger immediate deletion:
   ```bash
   # Extend lease by 60 minutes from now
   ./scripts/cloud-lab.py extend --run-id <id> --minutes 60
   
   # Delete immediately
   ./scripts/cloud-lab.py delete --run-id <id>
   ```

3. **3h hard backstop**: The kill switch also enforces a 3h (10800s) absolute maximum from boot time. No amount of lease extensions can exceed this limit.

**Lease configuration**: The default lease is 60 minutes, set at VM creation via `--lease` flag:
```bash
# 30-minute lease (aggressive cost savings)
./scripts/cloud-lab.py submit --pr 42 --lease 30 --publish

# 120-minute lease (deep debugging session)
./scripts/cloud-lab.py submit --pr 42 --lease 120 --publish
```

**VM retention policy**: By default, VMs stay alive after the pipeline finishes with a 60-minute lease for log inspection. Use `delete` to clean up immediately when done, or `extend` if you need more time.

**Cost comparison**:

| Scenario | Cost |
|----------|------|
| Default 60min lease, inspect 5min then delete | ~$0.03 |
| Default 60min lease, let expire naturally | ~$0.10 |
| Extended to 120min, full inspection | ~$0.20 |
| Hard backstop (3h max) | ~$0.29 |

**Operational rule**: Do not kill VMs until you have either:
- Verified the publish step completed (check `tests.tollgate.me`)
- Examined the logs via `gcloud compute ssh <vm> --command='cat /var/log/tollgate-run.log'`

### SHC Baked-VM Lab (cheaper alternative to GCP)

SHC (Sovereign Hybrid Compute) Dev VPS plans (pkg 80–84) support **nested KVM**
(`vmx`/`kvm_intel`/`nested=Y` verified), making them viable for the nested-QEMU
cloud lab at ~$0.01/run vs ~$0.10/run for GCP. However, SHC has two constraints
that require a different architecture than GCP's disposable-VM model:

1. **No cross-VM snapshot/clone** — SHC `snapshot-restore` is same-VM only
   (`POST /vm/{serviceId}/snapshots/restore`). You cannot create a new VM from a
   snapshot. Templates are OS-only (Debian/Ubuntu/etc.).
2. **No baked snapshot** — SHC starts from a bare OS template every time. The
   full 15-step bootstrap (apt-get, Rust, nak, venvs, CDK mints, QEMU image
   downloads) takes ~10–15 min and is failure-prone on a fresh VM.

The solution is a **persistent baked VM + snapshot-restore cycle**:

```
Bake (one-time, ~15 min):
  Order SHC VM → run full bootstrap (steps 1-14) → verify → create snapshot

Per run (~5 min restore + ~20 min tests):
  Stop VM → snapshot-restore (disk reset to baked state) → start VM
  → re-fetch suite + apply overlay → run worker-only (skips bootstrap)
```

**Setup (one-time bake):**

```bash
# 1. Order a Dev VPS (standard tier, 2C/8GB, nested KVM)
shc order --hostname tollgate-bake --package-id 81 --pricing-id 245 \
    --ssh-key ~/.ssh/id_rsa.pub --pay

# 2. Bake it (runs the full bootstrap + creates a snapshot)
python3 scripts/shc-bake.py --service-id <ID> --ip <IP> --branch main \
    --snapshot-name tollgate-baked-v1

# 3. Record the snapshot ID from the output (bk_...)
```

**Running tests on the baked VM:**

```bash
# Snapshot-restore + run worker-only (skips 15-min bootstrap)
python3 scripts/shc-run-baked.py --service-id <ID> --ip <IP> \
    --snapshot-id bk_<SNAP_ID> --branch main [--pr N] [--two-router] [--publish]

# Skip restore if the VM is already in the right state
python3 scripts/shc-run-baked.py --service-id <ID> --ip <IP> \
    --snapshot-id bk_<SNAP_ID> --branch main --no-restore
```

**Key differences from GCP `submit`:**
- One persistent VM reused across runs (no parallelism — one run at a time)
- Snapshot-restore wipes the disk between runs (clean state each time)
- The worker-only script re-fetches the suite + applies the overlay AFTER
  `git checkout`, and `ensure_suite_checkout` skips re-checkout when HEAD
  matches (preserving overlay changes to tracked files)
- The OpenWrt serial-provisioning fallback is essential — inner VM SSH takes
  ~60s to come up; the SSH-first path times out, serial provisioning succeeds

**Known issues fixed during development:**
- Stale `apt-get` lock from cloud-init → wait for provisioning to finish
- `qemu-img convert` write-lock on stale `openwrt-base.qcow2` → `rm -f` before convert
- `cdk-mintd: Text file busy` → `pkill` + `rm -f` before download
- Blossom resolver missing `branch` filter → deployed wrong firmware (fixed in `lib/deploy.py`)
- Blossom resolver queried transient kind 30078 instead of persistent kind 1063 (fixed)
- GCP overlay allowlist excluded new test files (fixed in `lib/cloud_lab/gcp.py`)

**SHC snapshot limits:** Dev VPS plans advertise `snapshot_limit: 5` but may
enforce 1 in practice. Delete old snapshots before creating new ones:
`shc snapshot-delete <service_id> <snapshot_id>`.

### SHC VM self-destruct (bounded keys — account key never on VMs)

Every SHC VM ordered by `cloud-lab.py submit` / `shc-run-baked.py` arms a
self-destruct at bootstrap via `_resolve_selfdestruct()` in
`lib/cloud_lab/shc_submit.py` (wraps shc-toolkit's lesson-23 module):
an on-VM systemd timer cancels the service at boot+`--lease` minutes using
a key planted at `/etc/shc/self-destruct.key` (0400) — NOT the account key.

**Key resolution (controller-side only — Basic creds never reach the VM):**
1. `SHC_SUICIDE_KEY` env (static short-expiry key) — override;
2. per-run 1-day mint over HTTP Basic from `SHC_ACCOUNT_EMAIL` +
   `SHC_ACCOUNT_PASSWORD` repo secrets (**`SHC_ACCOUNT_EMAIL` must be the
   BARE Blesta username, not the account email** — shc-toolkit lesson 26).
   Minted keys self-revoke the next day; nothing to rotate manually.
3. neither present → `legacy-warned`: the old inline kill-switch runs with
   the account key + a loud WARN in the log (the lesson-23 anti-pattern).

When the bounded path arms, the bootstrap `unset SHC_API_KEY` immediately —
steps 1–15 never see the account key. If the installer fails on the VM, the
legacy switch is kept as fallback. If minting fails on the controller
(rotated password), the submit **fails loudly** rather than silently
downgrading security — fix per shc-toolkit lesson 26 (credentials live in
`~/.config/shc/credentials.sh` on the lab machine) and update the
`SHC_ACCOUNT_*` repo secrets.

The static `SHC_SUICIDE_KEY` repo secrets are obsolete under this scheme
(per-run mints) and have been deleted; restore one only for
belt-and-braces.

### SHC Zone Reachability + Reaper Gotchas

**Paid-resource audits: `scripts/cost-status.py`.** Lists everything billing across SHC (services, snapshots, backups) and GCP (instances, disks, snapshots, images, addresses, machine-images). Resources are classified against `config/approved-resources.yaml` (regex on name, or GCP label match): anything not matching is UNAPPROVED → exit 1. VMs that exist but are powered off are flagged **STOPPED-BUT-BILLABLE** — SHC bills by service existence, not power state (incident: `lightning-playground` sat stopped for 9 days accruing $0.26/day unnoticed). Spend per 24h/7d/30d is reconstructed (Σ price/day × days-existed) because SHC's transaction ledger only records credits/refunds — renewals draw down credit silently and `list_invoices` stays empty. `--export-reaper-env` emits the allowlist as `SHC_REAPER_EXTRA_KEEP_PATTERNS` so the reaper and the audit share one approval source.

**This SHC account is shared by every agent project on the lab mini-PC** (lightning-playground, hackathon-tooling, clboss, tollgate-lab, shc-toolkit users…). A VM you did not order may belong to another session's soak test — check `get_vm_detail().ssh_key` for the `#shc-order=` tag to attribute it by ordering session before touching it, and never cancel a foreign VM without asking the user. Agents that order SHC VMs must: (0) `export SHC_ORDER_TAG=opencode:<session-id>` first so every order is attributable (shc-toolkit ≥403e177 embeds it in the key comment; `cost-status.py` displays `ordered-by`), (1) cancel them in the same session (stop = still billed), (2) use a reaper-reapable hostname prefix (`tollgate-`, `ci-`, `test-`, `tg-`) for ephemeral VMs — hostnames like `clboss-soak` or `lightning-playground` match no reap prefix and bill forever, and (3) register intentional long-lived VMs in `config/approved-resources.yaml`.

**Zone 7 (Cherryvale, Kansas / Dev VPS tier) is unreachable from Europe.**
The `66.92.204.0/24` subnet (Cherryvale) has no working BGP route from at
least Telenor Norway → Arelion/Telia backbone. ICMP, TCP, and SSH all return
100% packet loss — even ping to the gateway `66.92.204.1` fails. The SHC API
(`blesta.sovereignhybridcompute.com` at `23.182.128.0/24`) is reachable; only
the VM subnet isn't. If your host is in Europe, **use Zone 4 (Katy, Texas /
NVMe VPS tier, pkgs 23-35) instead** — it's on the same `/24` as the API and
inherits working routes.

| Zone | Location | Plans | European reachability |
|------|----------|-------|----------------------|
| 4 | Katy, Texas | NVMe VPS (pkgs 23-35) | ✅ ~142ms, SSH works first try |
| 7 | Cherryvale, Kansas | Dev VPS (pkgs 80-84) + SSD VPS (pkgs 56-60) | ❌ 100% packet loss |
| 8 | Katy, Texas — HDD | HDD VPS (pkgs 36-40) | ✅ (same /16 as Zone 4) |

**The SHC reaper kills test VMs by hostname prefix.** Two GHA workflows run
automatically:
- `shc-toolkit/.github/workflows/reap-orphan-vms.yml` (daily 05:23 UTC — eased from hourly 2026-08-27)
- `physical-router-test-automation/.github/workflows/vm-reaper.yml` (dispatch-only since 2026-08-27 — schedule removed; the lab machine's local `*/30` cron runs `cleanup-stale`)
- On-VM self-destruct timers (primary failsafe on every VM ordered through cloud-lab since 2026-08-27)

Both reap VMs whose hostnames start with: `tf-acc-`, `tollgate-`, `test-`,
`tmp-`, `ci-`, `tg-`, `zone-test-`, `nutshell-`, `pytest-test-`. VMs are
eligible after 2 hours of age.

**Workarounds** (pick one):
1. **Name your VM with `tollgate-main-` prefix** (e.g.
   `tollgate-main-mytask-<ts>`) — matches the default `keep_patterns` and is
   always spared.
2. **Set `SHC_REAPER_EXTRA_KEEP_PATTERNS` env var** in the GHA workflow or
   locally — comma-separated substring patterns to keep (e.g.
   `SHC_REAPER_EXTRA_KEEP_PATTERNS=nutshell-524,my-task-`). This env var was
   added in `shc-toolkit` commit `7cfea29` (July 2026).
3. **Pass `keep_patterns=[...]` to `reap_orphans()`** programmatically.

**The reaper spares**: `europa-vpn-vps` (explicit exclude), `tollgate-main-*`
(default keep pattern), and anything matching `SHC_REAPER_EXTRA_KEEP_PATTERNS`.
Both reaper workflows (hourly `shc-toolkit` via `SHCClient.reap_orphans()` and
every-30-min `physical-router-test-automation` via `SHCProvider.cleanup_stale()`)
honor the same keep mechanism — parity enforced across both code paths.

### Publish to Nostr+Blossom

The publish step uses Nostr events + Blossom uploads. No git operations are involved, so there are no push races, conflicts, or concurrent run issues. Each run publishes independently with its own kind 30078 summary events.

### mac80211_hwsim virtual WiFi (experimental, opt-in)

The v9 snapshot includes WiFi simulation packages (`kmod-mac80211-hwsim`, `wpad-basic`, `iw-full`, `iwinfo`), vwifi cross-VM relay binaries (`/opt/vwifi/bin/`), and management network UCI. Virtual WiFi is **disabled by default** — enable with `--hwsim` flag on `cloud-lab.py submit`.

**Why opt-in**: hwsim creates virtual radios on the OpenWrt VM but they cannot propagate beacons between PHYs. STA scan/association tests always skip in hwsim. The AP interfaces are useful for router-side config verification only. Portal testing works over wired br-lan without hwsim.

**How to enable**:
```bash
# Cloud lab with virtual WiFi (AP verification only, no cross-VM scan)
./scripts/cloud-lab.py submit --pr 42 --hwsim --publish

# Without hwsim (default) — portal tests still work over wired br-lan
./scripts/cloud-lab.py submit --pr 42 --publish
```

**Worker-provisioned topology** (`_setup_hwsim_wifi()` in `worker.py`):

| Component | Config | Mirrors |
|-----------|--------|---------|
| radio0 | 2.4GHz AP, channel 6, HT20, SSID=TollGate-ALPHA | MT3000 radio0 (2.4GHz) |
| radio1 | 5GHz AP, channel 36, VHT80, SSID=TollGate-ALPHA | MT3000 radio1 (5GHz) |
| Interface names | `phy0-ap0`, `phy1-ap0` | OpenWrt netifd naming |
| Bridge membership | Both AP interfaces in `br-lan` | Same as real MT3000 |
| nodogsplash | Manages `br-lan` (intercepts wired + wireless) | Same as real router |

The worker creates AP interfaces **manually** via `iw phy phy0 interface add phy0-ap0 type __ap` (bypasses netifd's mac80211.sh which fails with hwsim due to HOSTAPD_START_FAILED). It then adds them to br-lan and writes UCI wireless config for SSID/metadata. Non-fatal — if hwsim fails, the cloud lab continues without WiFi.

**Test gating**: `tests/api/test_mac80211_hwsim.py` skips entirely unless `TOLLGATE_ENABLE_HWSIM=1` or `TOLLGATE_ENABLE_VWIFI=1` is set. When enabled:
- AP tests (interface existence, bridge membership, SSID) pass
- STA tests (scan, association, DHCP) skip with hwsim — PHYs cannot see each other's beacons
- STA tests **run and pass** with `--vwifi` — vwifi relays frames between VMs
- Set `HWSIM_STA_ENABLED=1` to force-run STA tests (only useful on physical hardware with real radios)

**Debian container (10.99.99.100)** connects over wired ethernet on `br-lan`. Nodogsplash intercepts ALL traffic on `br-lan` regardless of whether the client arrived via WiFi or ethernet — the captive portal flow works without cross-VM WiFi. Playwright navigates to `http://10.99.99.1:2050/` directly.

OpenWrt names hwsim interfaces `phy<N>-ap0` (not `wlan0`). Tests must check `iw dev` output for interface names, not hardcoded names.

**Known limitations**:

- **No cross-PHY beacon propagation**: hwsim PHYs exist in separate kernel namespaces and do not simulate RF propagation. radio1's STA cannot see radio0's AP beacons.
- **No cross-VM WiFi**: QEMU guests run separate kernels — hwsim radios don't share RF state across VM boundaries (use `--vwifi` to solve this).
- **STA mode is read-only config verification**: reconfigures radio1 to STA mode on a dedicated `wwan` network, but association always fails in hwsim (unless `--vwifi` is used).

### vwifi cross-VM WiFi frame relay (opt-in)

[vwifi](https://github.com/Raizo62/vwifi) relays 802.11 frames between QEMU VMs via TCP, enabling real `iw scan` from the Debian guest to see SSIDs on the OpenWrt guest. This solves the cross-kernel hwsim limitation.

**How to enable**:
```bash
./scripts/cloud-lab.py submit --pr 42 --vwifi --publish
```

**Architecture**: A `vwifi-server` runs on the GCP host (TCP port 8212). Inside each guest, `vwifi-client` connects to the host via TCP. Guests load `mac80211_hwsim radios=0` (empty), then `vwifi-add-interfaces` creates relayed wlan interfaces. OpenWrt keeps its existing baked hwsim radios (can't rmmod — netifd holds refs) and gets an additional relayed interface via `vwifi-add-interfaces`.

**Correct vwifi procedure** (from README, verified):
1. `modprobe mac80211_hwsim radios=0` (empty — no local radios)
2. `vwifi-add-interfaces 1 <mac>` (creates relayed wlan interface)
3. `vwifi-client <host_ip>` (relays frames for vwifi-created interfaces only)

Debian uses `radios=0` + `vwifi-add-interfaces` to get a clean relayed `wlan0`. OpenWrt skips `radios=0` (existing radios can't be removed) and uses `vwifi-add-interfaces` to add a relayed interface alongside the local ones, then reconfigures hostapd to broadcast `TollGate-ALPHA` on the relayed interface.

**Worker pipeline** (when `--vwifi` is passed):
1. `_setup_vwifi_host()` — starts vwifi-server on host (TCP mode, port 8212)
2. `start_inner_vms()` — passes `mgmt_tap`/`mgmt_mac` for management NIC
3. `_setup_hwsim_wifi(vwifi_mode=True)` — configures UCI wireless on existing hwsim radios
4. `_setup_vwifi_guests()` — SCPs binaries to both VMs, creates relayed interfaces, starts vwifi-client, runs hostapd on OpenWrt, captures iw scan proof artifacts

**Scan proof artifacts**: Worker saves `iw scan` output from both VMs to `results/raw/virtual-wifi/iw-scan-openwrt.txt` and `iw-scan-debian.txt`. These appear as clickable links in the published report under "Native Reports".

**Verified**: Debian `iw scan` sees `TollGate-ALPHA` — cross-VM 802.11 management frame relay confirmed.

**Key implementation details**:
- **TCP mode** (not vsock) — vsock has zombie process kernel bugs on some kernels
- **BusyBox `nohup`** — OpenWrt ash doesn't have `nohup`; use bare `&` for background processes
- **`ip link set wlan0 up`** — vwifi-created interfaces are DOWN by default; must bring up before scan
- **Debian `nohup + disown`** — bash keeps SSH sessions alive with bare `&`; need nohup + redirect + disown for clean detach
- **Management network** — `mgmt-br` (10.99.97.0/24) provides SSH access independent of test network bridges

**Build requirements**: `cmake make g++ pkg-config libnl-3-dev libnl-genl-3-dev`. Build script: `scripts/build-vwifi.sh` (Alpine Docker for static musl guest binaries, glibc for host). Snapshot v9 includes pre-built vwifi binaries at `/opt/vwifi/bin/`.

**CID assignments** (legacy, now TCP):
| VM | vsock CID (unused) | TCP target |
|----|-----------|----------|
| Alpha OpenWrt | 10 | `10.99.99.2:8212` |
| Debian client | 20 | `10.99.99.2:8212` |
| Beta OpenWrt (two-router) | 11 | `10.99.99.2:8212` |

### What works in the cloud lab

- API tests (89 passed, 0 failed with local mint)
- Container client e2e portal payment (visual recording)
- Scenario tests: captive portal browser, mint health, boot hygiene, upstream WiFi CLI
- Reseller mode scenarios (with `--reseller-scenarios`)
- Two-router tests (with `--two-router`)
- Virtual WiFi: worker-provisioned hwsim (opt-in via `--hwsim`, 2 radios, AP bringup, STA tests skip)
- vwifi cross-VM WiFi relay (opt-in via `--vwifi`, real STA scan/association between VMs)
- NDS portal tests (over wired br-lan, no cross-VM WiFi needed)
- Portal verify tests (gated by `_skip_unless_nds_responsive()`)
- Local mints: CDK V2, Nutshell V1, Nutshell V2 (all FakeWallet)

### Debian Container Client Cashu Token Flow

The cloud lab uses a Debian QEMU VM (`10.99.99.100`) as the test client. Visual portal tests record the full Cashu payment flow from the user's perspective using Playwright inside this container. Here's how tokens get from the test framework to the browser:

```
┌─ Test Machine / GCP Host ────────────────────────────────────┐
│                                                               │
│  1. pytest creates CashuMint(venv, mint_url)                 │
│  2. cashu.warmup() → pre-initialize wallet DB                │
│  3. cashu.mint(4) → subprocess: cashu send 4 --legacy       │
│     Returns: "cashuAeyJwcm..."                               │
│  4. adb.signal_token(token) → SSH to container:              │
│     echo 'cashuAeyJwcm...' > /tmp/tg-token                   │
│                                                               │
└───────────────────────────│───────────────────────────────────┘
                            │ SSH
                            ▼
┌─ Debian QEMU Container (10.99.99.100) ───────────────────────┐
│                                                               │
│  5. Playwright recording script (already running):            │
│     - Launched by adb.start_portal_recording() via SSH       │
│     - Chromium navigated to http://10.99.99.1:2050/ (portal) │
│     - Screenshots: 01-portal-unpaid.png                       │
│     - Writes /tmp/tg-portal-ready (signals: browser loaded)  │
│                                                               │
│  6. Token signal polling loop (inside Playwright script):    │
│     for _ in range(240):                                      │
│       if /tmp/tg-token exists: → read token, break           │
│       sleep(0.5)                                              │
│                                                               │
│  7. submit_token(token):                                      │
│     - Find token input (textarea / input[name*=token])       │
│     - token_input.fill(token)                                 │
│     - Screenshot: 02-token-filled.png                         │
│     - Click submit button (purchase/submit/pay)              │
│     - Poll for auth markers (data-sm="authed", "remaining")  │
│     - Screenshot: 03-portal-paid.png                          │
│                                                               │
│  8. Verify internet access:                                   │
│     - Navigate to http://1.1.1.1                              │
│     - Screenshot: 04-internet-access.png                      │
│                                                               │
│  9. Close browser context → save video to                     │
│     /tmp/tg-e2e/portal-flow.webm                              │
│                                                               │
└───────────────────────────────────────────────────────────────┘
```

**Key synchronization points:**

| Signal | Path | Purpose |
|--------|------|---------|
| `/tmp/tg-portal-ready` | Written by Playwright | Test waits for this before minting token (portal loaded) |
| `/tmp/tg-token` | Written by test via `adb.signal_token()` | Playwright reads token and fills it into portal |
| `/tmp/tg-paid` | Alternative to `tg-token` | For out-of-band payment; Playwright reloads portal to capture paid state |

**Mint URL config chain:**

1. `worker.py` → `select_test_mint()` probes backend for V2 support
2. Falls back to Nutshell V1 (`http://v1.testnut.nutshell.lan:8385`) for Go backend
3. Writes chosen URL to `.env` as `TOLLGATE_TEST_MINT_URL`
4. `conftest.py` `cashu()` fixture reads `TOLLGATE_TEST_MINT_URL` from env
5. `create_minter()` selects the best available minter (`HttpMinter` > `CdkCliWallet` > `CashuMint`) and uses this URL for all minting operations

**Recording lifecycle (`lib/clients/container.py`):**

- `start_portal_recording()` — SSH to container, launches Playwright script as background process
- `wait_for_portal_ready(timeout=90)` — polls for `/tmp/tg-portal-ready` on container
- `signal_token(token)` — writes `/tmp/tg-token` on container
- `finish_portal_recording(output_dir, timeout=120)` — waits for Playwright script to complete, collects screenshots + video via SCP

### What needs improvement

- **hwsim STA mode**: Currently reconfigures radio1 to STA. Could use a 3rd radio for dedicated STA while keeping both AP radios stable.
- **Cross-VM WiFi**: Not possible with current QEMU topology. Portal testing works via wired ethernet through nodogsplash on br-lan.
- **Mint health/degraded tests**: Most skip in cloud lab because feature detection gates are conservative — could be relaxed with local mint manipulation
- **Multi-VM topology**: Currently limited to 2 OpenWrt VMs. Future: per-environment bridge isolation for dozens of VMs

### Out of scope for cloud

Phone tests, physical-router LuCI Playwright, destructive sysupgrade — use `test-pr.sh` on lab hardware.

## AI Agent Rules

- **Bug reports filed by AI agents must go to the Amperstrand fork only** ([Amperstrand/tollgate-module-basic-go](https://github.com/Amperstrand/tollgate-module-basic-go)), NOT the upstream OpenTollGate repo. This avoids noise for upstream maintainers. Filing on OpenTollGate is acceptable only when a human explicitly requests it.
- AI agents should not review PR #86 or ask @c0brador to merge it.

## Security Notes

- Built firmware images contain credentials (SSH key + password) in the uci-defaults script. Treat images as sensitive.
- Credentials are saved to `credentials/` (gitignored, mode 600).
- SSH key comments are stripped before embedding (no user@host in images).
- WAN SSH is acceptable for lab routers. Disable on production.
- The ASU server sees build requests over HTTPS. Acceptable for test firmware.

## Hardware Mutex Protocol

The Makefile enforces a hardware lock (`hardware.lock`) to prevent concurrent access to physical routers and ESP32 boards by multiple sessions (e.g., multiple LLM agents or developers).

### How it works

- `make lock PHASE="description"` creates `hardware.lock` with session info (user, hostname, branch, timestamp, phase)
- All hardware test targets call `require_hardware_lock` and fail if the lock is missing
- `make unlock` removes the lock
- `make force-unlock` force-releases (use with caution)
- ESP32 boards have separate per-board locks (`esp32/locks/board-a.lock`, etc.)

### Router label convention

Routers are identified by label (e.g., `alpha`, `beta`) from `mint-health/routers.env` or `upstream-wifi/routers.env`, not raw IPs. This allows the same Makefile targets to work across different lab configurations:

```bash
# routers.env format
ROUTER_ALPHA_HOST=192.168.13.112
ROUTER_ALPHA_SERIAL=/dev/serial-alpha
ROUTER_BETA_HOST=192.168.13.113
ROUTER_BETA_SERIAL=/dev/serial-beta
```

### Lock file location

- Router lock: `./hardware.lock` (project root)
- ESP32 board locks: `esp32/locks/board-{a,b,c}.lock`
- All lock files are gitignored (`**/*.lock`)

## Mint Health iptables Simulation

Degraded mode (mint unreachable) is simulated via iptables rules that block traffic to the mint's resolved IP while preserving NetBird SSH connectivity:

```bash
# Block mint (force degraded mode)
iptables -A OUTPUT -d <mint-ip> -j DROP

# Unblock mint (restore full mode)
iptables -D OUTPUT -d <mint-ip> -j DROP
```

The Makefile targets (`block-mint`, `unblock-mint`) handle hostname resolution and iptables automatically.

## Two-Router Test Topology

Hardware tests use two routers connected via NetBird:

| Role | Label | Purpose |
|------|-------|---------|
| Alpha | `alpha` | Primary test target |
| Beta | `beta` | Secondary / upstream TollGate |

Tests like `smoke-upstream` and `smoke-pin-upstream` require both routers. Alpha acts as the downstream TollGate (payment gateway), and Beta provides the upstream network connection.

## Serial Console Operational Notes

### USB-TTL adapters

- Use CP2102 or CH340-based USB-TTL adapters (3.3V, NOT 5V)
- GL-MT3000 serial pins: TX, RX, GND (no flow control needed)
- Baud rate: 115200

### udev rules

Create `/etc/udev/rules.d/99-serial-routers.rules` with stable symlinks:

```
SUBSYSTEM=="tty", ATTRS{serial}=="CP2102_ABCD", SYMLINK+="serial-alpha"
SUBSYSTEM=="tty", ATTRS{serial}=="CP2102_EFGH", SYMLINK+="serial-beta"
```

### Serial target prefixes

| Prefix | Transport | Use Case |
|--------|-----------|----------|
| `r-` | SSH | Normal operations |
| `s-` | Serial | No-network scenarios: cold boot, recovery |
| `h-` | Hybrid | Tries SSH first, falls back to serial |

### Emergency serial recovery

If a router is stranded (broken config, no network), use serial recovery:

```bash
make serial-recovery ROUTER=alpha CMD="sed -i '/nofee.testnut/d' /etc/hosts && /etc/init.d/tollgate-wrt restart"
```

## ESP32 Board Flashing Notes

- **SPIFFS generation**: `make arch-generate-spiffs` auto-detects WPA security mode (WPA2-PSK, WPA3-SAE, or open) from the board's running config
- **Per-board firmware variants**: Boards A, B, and C can run different firmware (multi-mint, relay, tollgate_core)
- **Flash targets**: `esp32-flash-a/b/c` require the corresponding board lock
- **Monitor targets**: `esp32-monitor-a/b/c` provide serial console access to each board

## Arch (tollgate_core) Component Testing

The arch test suite validates the `tollgate_core` firmware on ESP32 (Board A):

1. `arch-build` - Cross-compile firmware
2. `arch-flash-a` - Flash to Board A
3. `arch-generate-spiffs` - Generate filesystem with auto-detected WPA config
4. `arch-flash-spiffs-a` - Flash filesystem
5. `arch-test-full` - Run all E2E tests (~4min): smoke, network, API, DNS/firewall, auth reset, session expiry

Tests are ordered by dependency and run sequentially. The full suite validates WiFi AP, captive portal, DNS resolution, payment flow, and session management.

## Writing New Tests — Quick Reference

1. **API test** (no phone): Create `tests/api/test_<feature>.py`. Use `@pytest.mark.api` and at least one tier marker (`smoke`, `critical`, or `extended`). Use the `router` fixture.

2. **Phone test** (needs ADB): Create `tests/phone/test_<feature>.py`. Use `@pytest.mark.phone`. Use `router`, `adb`, `cashu`, and `connected_wifi` fixtures.

3. **Gate with feature detection**: Add a `_skip_if_no_<feature>(router)` helper at the top of the test file. Call it at the start of each test function.

4. **Gate for bug regression**: Use `gate_bug_fix()` from `lib/helpers`. Pass a boolean indicating if the fix is present, plus `bug_id` and `fix_pr` for traceability.

5. **Backend-specific**: Use `@pytest.mark.go_only` or `@pytest.mark.rust_only` if the test only applies to one backend.

6. **Scenario test**: Create `tests/scenarios/test_<scenario>.py`. Use `@pytest.mark.hardware`. Register in `config/make-pytest-map.yaml`.

7. **Reference format for bug cross-links**:
   ```
   See: https://github.com/OpenTollGate/tollgate-knowledgebase/tree/main/incidents/YYYY-MM-DD_slug.md
   ```

## Cashu Token Version Compatibility

The Go backend (gonuts) supports V1, V3, and V4 Cashu tokens. V4 support was added in gonuts-tollgate v0.8.0 via `resolveShortKeysetIds()`. PR #284 bumps the dependency (pending merge).

| Token Version | Prefix | Encoding | Go Backend | Notes |
|---------------|--------|----------|------------|-------|
| V1 | `cashuA` | Base64 JSON | **Accepted** | Legacy format |
| V3 | `cashuAeyJ` | Base64 JSON | **Accepted** | Current standard, tested with 378-char testnut tokens |
| V4 | `cashuB` | Binary CBOR | **Accepted (gonuts v0.8.0+)** | `resolveShortKeysetIds()` resolves 8-byte short keyset IDs to full IDs before swap. V4+V1 verified e2e (`kind=1022, allotment=176160768`). V4+V2 keyset confirmed locally. Without v0.8.0: `NUT02: ID length invalid`. |

Users with modern Cashu wallets (eNuts, cashu.me with latest CDK) producing V4 tokens are supported once PR #284 is merged.

Full findings and test matrix: `docs/portal-test-findings.md`.

### Keyset ID compatibility (separate from token version)

| Keyset Version | Prefix | Go Backend | Notes |
|---------------|--------|------------|-------|
| V1 | `00` (16 hex chars) | **Full support** | Verified end-to-end: `kind=1022, allotment=66060288 bytes` |
| V2 | `01` (66 hex chars) | **Full support** | Verified end-to-end: `kind=1022, allotment=88080384 bytes` (CDK V2 mint, keyset `01df97b6fb8a...`) |

The Go backend can be CONFIGURED with V2 mints without crashing, but cannot ACCEPT token payments from V2-keyset mints. The Rust backend (CDK native) handles both V1 and V2 fully.

## Captive Portal Flow (Physical Router)

### Framework approach (recommended)

The framework does NOT rely on Android's native "Sign in to network" popup. It bypasses it:

1. `reset_state()` — deauth client, restart NDS + backend
2. `wifi._connect_to_wifi()` — disconnect WiFi, reconnect to TollGate SSID
3. `_open_portal_on_phone()` — **manually opens portal URL in browser** via `am start -a android.intent.action.VIEW`

This is deterministic and works across Android versions/OEMs. The native popup is unreliable — Android caches network validation results and may not re-check for minutes after deauthentication.

### ADB token typing

- `adb shell input text` silently truncates at ~200 chars. Must use 80-char chunks with 0.3s delay for tokens ≥200 chars.
- The portal UI only shows ~30 chars of the token in the input field, but the full token IS present.
- The keyboard must be dismissed (tap outside input field) before the Purchase button is visible.
- The portal Paste button (CB002) is broken on Android 14 Firefox.

### firewall-tollgate masquerade bug

The TollGate `.ipk` creates `/etc/config/firewall-tollgate` as a UCI-format fw4 include. fw4 silently rejects it because the syntax is invalid for an include file. The masquerade/NAT rule never loads. Authenticated clients can reach the router but not the internet.

**Fix**: Rename the file to `.disabled`, remove the UCI include reference, restart fw4.

**Impact**: `pay_direct()` tests don't catch this because they SSH to the router and hit the backend directly. Only phone tests that verify actual internet access would detect the missing masquerade.

### Identity derivation: public key vs private key (PR #193 review miss)

PR #193 (`feat/first-boot-handover`) derived root and WiFi passwords from the **public key** (`pubKeyHex`), which is broadcast as npub to Nostr relays. Anyone who learns a router's npub could compute its root and WiFi passwords. The fix (commit `d1881c3`) changed `DeriveRootPassword` and `DeriveWiFiPassword` to use `privKeyHex` instead.

**The principle**: when reviewing identity/key derivation code, always verify which key material feeds which derived value:
- **Public key** → public attributes only (LAN IP, MAC addresses, npub)
- **Private key** → secret values (passwords, mnemonics, WiFi credentials)

**How it was missed**: the review focused on HTTP endpoint security (loopback-only checks, flag-file gating) and the security model documentation, but did not trace the actual Go derivation functions to verify the key material input. The docs described the derivation tree correctly but the implementation diverged.

**Review checklist for identity derivation PRs**:
1. Trace every `Derive*` function to its input key material
2. Verify: public key inputs produce only public outputs
3. Verify: private key inputs produce secret outputs
4. Check that the private key is never exposed on any non-loopback endpoint
5. Verify test fixtures use real key pairs, not hardcoded public keys

### Port 2121 directly exposed to WiFi clients (defense-in-depth)

The TollGate backend binds to `:2121` (all interfaces). Nodogsplash explicitly allows WiFi clients to reach it (`users_to_router='allow tcp port 2121'`). This is by design — the payment flow requires browser-to-backend HTTP calls. But it means every HTTP handler is remotely exploitable by anyone in WiFi range.

The frontend fetches directly from `http://<router-ip>:2121/` in three places:
- `tollgate.js`: `fetch(\`${baseUrl}/\`)` and `fetch(\`${baseUrl}/whoami\`)`
- `cashu.js`: `fetch(\`${baseUrl}/\`, {method: 'POST'})`

**Defense-in-depth fix (not yet implemented)**: move backend to `127.0.0.1:2121`, front with a CGI reverse proxy on nodogsplash's port 2050. Frontend changes to relative URLs (`/api/` instead of `http://<ip>:2121/`). This is a cross-repo refactor (backend + frontend + packaging).

Tracked as: https://github.com/OpenTollGate/tollgate-module-basic-go/issues/213

## Lessons Learned — July 2026 V4 Token Investigation

### What went well

- **Deterministic A/B testing**: Minting V4 tokens with cdk-cli, decoding with `cdk-cli decode-token`, and paying via raw body curl gave clear pass/fail signals
- **Source code analysis**: Reading gonuts (`TokenV4.Proofs()`), CDK (`ShortKeysetId::from()`, `Id::from_short_keyset_id()`), and the NUT-00 spec in parallel identified the exact root cause
- **Virtual lab**: Running on localhost (no SHC needed) enabled rapid iteration — mint token, pay, check logs in <30 seconds
- **pay_direct() fix**: Routing payments through the Debian VM (10.99.99.100) for correct MAC lookup unblocked automated pytest payment tests

### What we struggled with

1. **Token format (JSON vs raw body)** — **BIGGEST TIME SINK**: Spent hours diagnosing "invalid V3 token" errors caused by sending JSON `{"token":"..."}` instead of raw body. `extractCashuToken()` in `main.go` returns the raw body as the token string — JSON wrapper makes prefix `{"toke` instead of `cashuA`. **Lesson**: always use `curl -d "$TOKEN" -H 'Content-Type: text/plain'`, never `curl -d '{"token":"$TOKEN"}'`. Manual tests must match the test framework's `pay_direct()` format.

2. **Dev build mint injection**: The Go binary injects `nofee.testnut.cashu.space` (unreachable) when `GitBranch != "main"`. This caused intermittent mint mismatch errors. **Lesson**: always check backend startup logs for `WARN: dev build detected`. Fix committed: `nofee.testnut.cashu.space` → `testnut.cashu.exchange` in `config_manager_config.go`.

3. **Go module replace directives**: The `replace` in `tollwallet/go.mod` is IGNORED when building from the root module. Only the root `go.mod`'s `replace` block is effective. **Lesson**: always put `replace` directives in the MAIN module's `go.mod`, not in dependency sub-modules.

4. **Static linking**: CGO-enabled binaries crash on OpenWrt (`ash: /usr/bin/tollgate-wrt: not found` despite file existing). **Lesson**: always build with `CGO_ENABLED=0` for OpenWrt targets.

5. **Config caching**: `wallet.db` caches mint URLs from previous runs, overriding `config.json` changes. **Lesson**: always `rm -f /etc/tollgate/wallet.db` when switching mints.

6. **NDS session state**: `ndsctl auth` on an already-authenticated client returns exit status 1. **Lesson**: always `ndsctl deauth <mac>` before each payment test. The `container_nds_preflight` fixture now handles this.

7. **SHC VM lifecycle**: VMs were reaped by the hourly GHA reaper workflow because hostnames matched `nutshell-*` prefix. **Lesson**: name VMs with `tollgate-main-` prefix (matches `KEEP_PATTERNS`) or set `SHC_REAPER_EXTRA_KEEP_PATTERNS` env var.

### Do we have enough logging?

**No.** Critical gaps identified:

1. **Token parsing**: `extractCashuToken()` doesn't log whether it detected a Nostr event vs raw token vs JSON. Add debug log: `"Detected token format: raw|nostr|json, length: N"`
2. **V4 short keyset ID resolution** (NEW): `resolveShortKeysetIds()` should log when it resolves a short ID: `"Resolved short keyset ID 01df97b6fb8a572a → full ID 01df97b6fb8a572a718d..."` 
3. **Swap request**: The swap request payload isn't logged. Add debug: `"Swap request to %s: inputs=%d, keyset_ids=%v"`
4. **MAC lookup**: `getMacAddress(ip)` should log the lookup result: `"MAC lookup for IP %s: %s (source: dhcp|arp|failed)"`

### Why `Origami74/gonuts-tollgate` instead of `OpenTollGate/gonuts-tollgate`?

**Historical artifact.** The go.mod declares `require github.com/Origami74/gonuts-tollgate v0.6.1` (the original active fork) with a `replace` directive overriding it to `OpenTollGate/gonuts-tollgate v0.7.4` (the canonical fork). As of v0.8.0, both forks are synced. The `require` should be changed to `OpenTollGate/gonuts-tollgate v0.8.0` directly to eliminate confusion. The `replace` is only needed during local development.

### Recommended PRTA improvements

1. **Add `test_token_formats.py`**: Automated test that mints V3+V1, V3+V2, V4+V1, V4+V2 tokens and pays via `pay_direct()`. Catches token format regressions before deployment.
2. **Add `assert_payment_succeeded()` helper**: Checks backend logs for `Receive completed, amount=N, err=<nil>` — distinguishes payment failures from gate-open failures (which are infrastructure issues).
3. **Virtual lab config management**: Script to switch backend config between testnut and CDK V2 mint without manual SSH + sed + jq chains.
4. **cdk-cli integration**: Bundle `/tmp/cdk-cli` into the test framework as a standard tool for V4 token minting.
5. **Build verification**: Add a test that verifies a freshly-built `.ipk` contains expected fix strings (`strings /usr/bin/tollgate-wrt | grep resolveShortKeysetIds`).

## VM cleanup: bounded self-destruct keys (2026-08-27)

Superseded by "SHC VM self-destruct" above (per-run 1-day mints via the
`SHC_ACCOUNT_*` repo secrets; the static `SHC_SUICIDE_KEY` secrets are
deleted). Still true from this incident: only **full-scope** keys can cancel
(operate-scope and nostr leases 403 cancel — money class), Bearer keys cannot
mint (Basic only), and the planted key grants account-wide spend for its
lifetime — **never arm self-destruct on tollgate/untrusted-workload boxes**.

## Lessons Learned — Local Virtual Lab Fast-Start (2026-08-28)

Repeat start-poc cycles went **590s → ~157s** and serial provisioning became a
fallback-only path. Four stacked root causes, each hiding the next:

1. **`debian-12-nocloud` ships NO cloud-init.** "nocloud" means *no cloud
   integration*, not the NoCloud datasource — the seed ISO attached to the QEMU
   command was dead weight. Switched the client base to
   `debian-12-generic-amd64.qcow2` (has cloud-init); `prepare-debian` now also
   builds `images/seed.iso` (genisoimage, volid `cidata`) with user-data
   (root password, sshd, packages, ssh key), meta-data, and network-config v2
   (static `10.99.99.100/24`, gateway 10.99.99.1, **nameservers → host bridge
   10.99.99.2** — the freshly provisioned OpenWrt's dnsmasq is unreliable
   during provisioning windows; apt via its resolver failed).
2. **Serial provisioning configured the network runtime-only** (`ip addr add`):
   every reboot came up network-less, forcing serial on every start. Persist as
   netplan — but **overwrite the image's `90-default.yaml` rather than adding a
   sibling file**: netplan emits all files with the same prefix and networkd
   picks the lexicographically-first match, so a DHCP match-all (`en*`) in
   90-default shadows a higher-numbered explicit-ens3 file.
3. **Debian's netplan.io ships no boot-time generate**: `/run/systemd/network`
   is tmpfs; on reboot ens3 is unmanaged and `systemd-networkd-wait-online`
   burns its full 120s timeout before sshd. An `ExecStartPre` drop-in inside
   `systemd-networkd.service` crash-loops — the unit is sandboxed
   (`ProtectSystem=strict`, `CapabilityBoundingSet` without
   `CAP_DAC_OVERRIDE`) and cannot read a 0600 netplan file. Fix: an
   **unconfined oneshot** `netplan-generate-boot.service`
   (`Before=systemd-networkd.service`, enabled via cloud-config runcmd).
4. **stop-poc hard-killed QEMU**, dirtying the qcow2; the next boot paid
   journal replay on top of everything. stop-poc now sends
   `system_powerdown` via the monitor socket (20s grace) before falling back
   to kill.

Plus two client-side fixes: `-smbios type=1,serial=ds=nocloud` (skips
cloud-init's ~2-min datasource probing on every boot) and a mint **health
probe** in `run-local-tests.sh` — `/v1/info` answering is NOT proof of health;
a wedged cdk-mintd accepts quotes but never settles them (FakeWallet dead),
which hung every payment test past the suite timeout. The probe posts a 1-sat
quote and requires PAID within ~12s, restarting the mint otherwise. The runner
also passes `--timeout-method=signal` (the ini's `thread` method dumps stacks
but cannot kill a hung test — payments hung "forever" under it).

Verified: fresh first boot 295s via cloud-init (serial skipped even on first
boot); two consecutive restart cycles 160s/157s; client userspace boot 16s
(was 2min3s); payment suite 3/3 after each cycle and after a mid-payment
SIGKILL. Related: SHC Dev-zone status tracked in shc-toolkit#28 (provisioning
fixed, network attach still broken — see shc-toolkit AGENTS.md lesson 26).

## Token Recovery Tool + tokens-to-recover.txt semantics (2026-09-01 field validation)

`scripts/recover_tokens.py` (PR #86 + #97; unit tests in
`tests/unit/test_recover_tokens.py`) parses
`/etc/tollgate/tokens-to-recover.txt` (line format
`timestamp | mint_url | token | error`, writer in tmbg
`src/upstream_session_manager/token_recovery.go`), checks spend state via
NUT-07 checkstate, and resubmits unspent tokens to the router backend on
:2121 (raw body, `text/plain`). Dry-run is parse-only (no network);
non-2xx router responses are recorded as `SUBMIT_FAILED` with the body.

**File-entry semantics (verified live, matters for interpreting results):**

- On payment failure the backend FIRST tries `merchant.Fund(token)`
  (receive into the router's own wallet). Only if that ALSO fails does it
  append to the file — file entries are double-failure tokens.
- The backend consumes (swaps) the token BEFORE opening the gate. A
  payment that fails at gate-open leaves the token SPENT at the mint
  (reproduced on the local virtual lab). Therefore every
  "failed to open gate" entry is SPENT and can never be recovered by
  resubmission — the value sits in the router wallet (recover via LuCI
  fund/drain). The recovery tool correctly reports `SKIPPED_SPENT` for
  these; don't misread that as a tool bug.
- The tool's real use case: pre-consume rejections (mint outage, transient
  errors) that also failed the auto-Fund — those are still UNSPENT and
  resubmission works once the mint is back.

**Unknown-Y checkstate semantics (source-verified across implementations):**
NUT-07 defines only `UNSPENT`/`PENDING`/`SPENT` and never says what a mint
returns for a Y it does not know. Every reference implementation answers
`UNSPENT`, deliberately: mainline nutshell
(`cashu/mint/db/read.py:get_proofs_states` — absent Y → `unspent`), CDK
(`crates/cdk/src/mint/check_spendable.rs` — `unwrap_or(State::Unspent)`,
predating PR #756, still current at v0.18), and cashu-cf (ISSUE-048 in that
repo). This is information-theoretic, not sloppiness: the mint never learns
the Y of an unspent proof (blind signatures — it only sees Y at redemption),
so "issued and unspent" is indistinguishable from "never issued".
Consequence for recovery tooling: an `UNSPENT` result NEVER proves
recoverability for wrong-mint tokens — the tool surfaces `unknown_count` /
`unknown_proofs` so operators can distinguish. Spec clarification proposed
upstream: cashubtc/nuts#432.

**Virtual-lab payment-testing gotchas (ai-legion host, found during the
same validation):**

- Host ufw is default-DROP: the lab needs scoped rules —
  `ufw allow from 10.99.99.0/24 to any port 8383 proto tcp` (mint),
  `ufw route allow from 10.99.99.0/24` (forwarded client traffic),
  `ufw allow in on tg-poc-br from 10.99.99.0/24 to any port 53 proto udp`
  (client DNS to host).
- NDS only authenticates MACs already in its client table. From the Debian
  client, fetch `http://10.99.99.1:2050/` BEFORE submitting a token,
  otherwise the backend's `ndsctl auth` fails with
  "failed to open gate: exit status 1" (the exact error this recovery file
  records — a clean local repro of the #88 incident class).
- Restart nodogsplash after any `fw4 restart` — fw4 rebuilds the base
  chains and NDS must re-insert its own.
- Each failed submit burns the token (consume-before-gate): budget a fresh
  fakewallet token per attempt when debugging.

### CDK mint version: variable, default 0.18.0 (upgraded from 0.16.0, 2026-09-03)

The local fakewallet mint binary (`/opt/cdk-mintd/cdk-mintd`) was pinned to
`CDK_VER=0.16.0` (2026-03-31) in `bake-snapshot.py` and `shc_submit.py`
since the SHC lab was built. It is now a variable defaulting to **0.18.0**
(v0.18.0 final, 2026-09-02); override with `CDK_VER=0.17.6` (last 0.17.x)
for `bake-snapshot.py` / `cloud-lab.py submit`. `run-local-tests.sh` and
`lib/cloud_lab/worker/mints.py` auto-detect the installed binary's version
and emit the matching config.

**v0.18 config-model changes that the scripts already handle:**

- Config is stored authoritatively in the mint DB; a normal start ignores
  `config.toml`. Fresh mints need `config validate` +
  `config init --new-mint --file config.toml` (with `--work-dir`/`CDK_MINTD_WORK_DIR`)
  before the first start. `--new-mint` rejects a DB that already has an
  identity — that's why the scripts always start from a fresh work dir
  (fakewallet value is disposable, mnemonic is fixed).
- **Secrets must be references**: `mnemonic = "abandon …"` is rejected;
  use `mnemonic = "env:CDK_MINTD_MNEMONIC"` and export the variable for
  both `config init` and the daemon start.
- `[ln] ln_backend = "fakewallet"` → `[payment_backend] backend = "fakewallet"`;
  `CDK_MINTD_LN_*` → `CDK_MINTD_PAYMENT_BACKEND_*`.
- `[fake_wallet]` still exists; keep `min_delay_time = 0` /
  `max_delay_time = 0` — 0.18 defaults are 1–3s, which slows every mint.
- NUT-04/07 HTTP behavior is unchanged for our flows (HttpMinter,
  checkstate verified live on 0.18.0: full payment E2E + unknown-Y probe
  both pass; unknown Y still reads `UNSPENT`, consistent with
  `unwrap_or(State::Unspent)` in `check_spendable.rs`).
- **Full-suite A/B (2026-09-03)**: entire `tests/api` on the local lab —
  83 passed / 23 failed / 1 hang on 0.18.0; the SAME 21 of 23 tests fail
  identically on 0.16.0 (diff-verified failure sets; 2 flaky passes).
  The failures are pre-existing local-lab environment issues (missing
  balance.html/uhttpd on the fresh VM, degraded-mode simulations,
  lightning-portal melts), NOT 0.18 regressions. The payment-critical
  surface is fully green on 0.18 (25/25: payment regression, quote
  persistence, lightning backoff, mint-url fuzzy, V3/V4/V2-keyset token
  formats, spent-token rejection). Known local-lab quirks regardless of
  version: a swap-concurrency test can hang past pytest's signal timeout
  (thread-pool + signal-method limitation) — kill and rerun; and 0.18
  fakewallet melt logs `Fee was too high`/`Over paid melt quote` WARNs on
  the lightning-portal tests (settlement completes anyway).
- 0.18 opens/migrates existing mint DBs — never point 0.16 at a
  0.18-written work dir afterwards (backup first if the DB matters; lab
  mints are disposable).

### Nodogsplash 5.0.2 auth-mark bug: authenticated clients cannot open NEW connections (2026-09-03)

Root-caused on the local virtual lab after the recovery-tool E2E kept
passing payments (`kind:1022`, allotment granted) while the client could
not reach the internet. This is the lab-side reason "authenticated but no
internet" incidents reproduce here, distinct from the firewall-tollgate
masquerade bug above.

**The bug** (nodogsplash 5.0.2-r1 on OpenWrt 24.10): on `ndsctl auth`, NDS
inserts a per-client uplink rule in `mangle ndsOUT` with
`MARK --or-mark 0x30000` — setting BOTH the 0x10000 (preauth) and 0x20000
(auth) bits — but the accept rule in `filter ndsNET` tests
`--mark 0x20000/0x30000`, i.e. `(mark & 0x30000) == 0x20000`, which a
0x30000-marked packet can never satisfy. New connections from an
authenticated client fall through to `ndsAUT`'s catch-all
`REJECT --reject-with icmp-port-unreachable`; only ESTABLISHED flows
survive via the conntrack accept. Symptom: payment succeeds, `ndsctl
status` shows `State: Authenticated`, client gets timeouts (or ICMP port
unreachable on ping).

**Workaround** (after each successful auth, per client IP/MAC):

```bash
iptables -t mangle -R ndsOUT 1 -s <client-ip> -m mac --mac-source <mac> \
  -j MARK --or-mark 0x20000
```

Verified: with the corrected mark the client's new connections are
accepted in `ndsNET`, forwarded through the host (ufw route allow +
MASQUERADE), and internet works end-to-end (HTTP 200 via the captive
portal). Why the lab never caught this: `run-local-tests` payment tests
assert backend/session state only — no test opens a NEW connection from
the client after payment (same blind spot as the masquerade bug).

**Full validated lifecycle (2026-09-03, local virtual lab, cdk-mintd
fakewallet):** mint token → `recover_tokens.py --check` UNSPENT → submit
raw token from Debian client (portal fetch first! see above) → backend
`kind:1022` gate-open, allotment granted → NDS mark workaround → client
internet HTTP 200 → token SPENT at mint. Note when testing from the
client: use IP-literal HTTP endpoints (9.9.9.9:80 does not serve HTTP —
cost me a false negative) and set client DNS to the router
(`nameserver 10.99.99.1`); the host's systemd-resolved listens on
loopback only, so the cloud-init default `nameserver 10.99.99.2` resolves
nothing in steady state (it exists only because the freshly-provisioned
router's dnsmasq is unreliable during provisioning — see the fast-start
notes above). Natural follow-up: fold the NDS mark workaround into the
lab runner / `lib/router.py` as a `fix_nodogsplash_auth_marks()` helper
in the spirit of `fix_nodogsplash_dhcp()`.

## Lessons Learned — Local-Lab-Green Campaign (2026-09-05/06)

Three suite-poisoning cascade classes root-caused on the local virtual lab
(branch `integ/local-lab-green`, PR #101; full suite 229E → 4E, zero cascade):

1. **Wedged SSH ControlMaster**: one hung mux makes every `Router.ssh` client
   time out while the router stays healthy and fresh connections work.
   `Router.ssh`/`ssh_stdin` now tear the master down and retry once. Evidence
   pattern: mass `subprocess.TimeoutExpired` on `ndsctl deauth` in
   `container_nds_preflight` with a healthy `/v1/info` + `ndsctl json`.
2. **Wedged NDS** (SIGKILL trap re-confirmed): kill -9 on a pytest mid-test
   wedges `ndsctl` (first deauth-only, then the socket goes EBADF; restarts
   cannot clear an init-time hang — a VM reboot was required once). The
   runner gates on `ndsctl json` (host-side `timeout 15` — **OpenWrt has no
   `timeout` binary**; `timeout 6 ndsctl …` inside ssh returns 127 and fakes
   a "wedge"), restarts NDS+backend once, aborts with a reboot hint.
3. **NDS 5.0.2 auth-mark gating**: rewriting the per-client ndsOUT rule LEAKS
   (NDS cannot delete a foreign rule at deauth → permanent internet; NDS also
   removes its rule asynchronously and the backend's 5s session valve
   re-asserts auth while a session lives — poll for the rule to actually
   disappear). The repair is one client-agnostic `ndsNET` accept rule for the
   auth bit (`--mark 0x20000/0x20000`), idempotent, wired into `wait_for_auth`.

Environment traps found while verifying:
- **neverssl.com is TCP-blocked from this network** (ICMP passes) — a
  connectivity probe must be IP-literal (1.1.1.1 serves HTTP 301) and must
  NOT use `-L`: its redirect target needs DNS, and DNS-through-NDS is
  governed by `users_to_router`, which allows **tcp/53 only** — UDP DNS from
  a gated client fails. The Debian client's steady-state resolver must be
  the router (cloud-init's 10.99.99.2 default only answers during
  provisioning; its resolv.conf is a symlink — `rm` it before writing).
- **Backend rate limit (tmbg#88)**: post-merge-14 wraps the payment root in
  `RateLimitMiddleware` — 10 req/min per client IP. Suite payment cadence +
  reruns trip it (`kind 21023` / `rate limit exceeded`); absent in
  post-merge-12. Env knob `TOLLGATE_RATE_LIMIT_RPM` exists but is not
  persistable through the init script.
- **reveal-seed is a derivation oracle now** (recontracted 2026-09-06, PRTA
  #102): `POST /identity/reveal-seed` takes a raw 12-word BIP39 mnemonic as
  the body (not JSON) and returns the identity derived from it — it no longer
  reveals the stored seed. Empty/garbage body → 400 `invalid mnemonic`;
  GET → 405; non-loopback → 403. Passwords are v2-format: six lowercase
  BIP39 words hyphen-joined (the Nato-Nato-Nato-NN regexes are stale).
  `tests/api/test_pr193_identity_endpoints.py` pins the full contract. (Also:
  CORS hardening 415s busybox wget's form content-type on the payment root —
  use curl with an explicit content-type.)
- **OpenWrt deletes uci-defaults scripts after execution** — post-boot
  firmware legitimately has no `/etc/uci-defaults/99-tollgate-setup`; tests
  must presence-guard.
- The documented signal-timeout hang class struck again
  (`test_startup_mint_recovery_latency` >10 min past `--timeout=180`); kill
  + rerun the remainder is still the only recourse.
