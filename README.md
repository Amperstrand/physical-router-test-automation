# physical-router-test-automation

Multi-tier test framework for TollGate backends running against physical OpenWrt routers, local QEMU VMs, or cloud labs. Supports the Go backend ([tollgate-module-basic-go](https://github.com/OpenTollGate/tollgate-module-basic-go)), the Rust 1:1 clone ([tollgate-module-basic-rust](https://github.com/felixfelix-bot/tollgate-module-basic-rust)), and the experimental Rust backend ([tollgate-rs](https://github.com/Amperstrand/tollgate-rs-ai-research-and-experiments)).

## Testing Venues

Three ways to run tests, depending on what you have available:

| Venue | What you need | Best for | Setup time |
|---|---|---|---|
| **Local QEMU VM** | Linux host with KVM | Fast iteration, debugging | 2 min (after first setup) |
| **SHC (cloud VM)** | SHC API key | Fire-and-forget CI-style runs | ~25 min autonomous |
| **Physical router** | GL-MT3000 on LAN | Real hardware validation | Manual setup |

All venues support all backends via `--backend go|rust-basic|rust`.

### Local QEMU VM (fastest iteration)

Run tests against a local OpenWrt 24.10.1 QEMU VM with a fakewallet Cashu mint (free tokens, no Lightning needed). No physical router or cloud account required.

```bash
# Start VMs (OpenWrt + Debian client)
python3 scripts/virtual-lab.py start-poc --host localhost

# Run tests (fakewallet mint starts automatically)
./scripts/run-local-tests.sh tests/api/test_nds_fw4_integration.py

# Clean up
python3 scripts/virtual-lab.py stop-poc --host localhost
```

See [LOCAL-VM-TESTING.md](LOCAL-VM-TESTING.md) for the full guide including troubleshooting, deploying captive portal changes, and NDS/fw4 enforcement testing.

### SHC — Sovereign Hybrid Compute (cloud VM)

Fire-and-forget testing on an SHC cloud VM. Identical to GCP but uses the SHC provider (no GCP credentials needed, uses SHC API key instead).

```bash
# Submit a PR for autonomous testing
./scripts/cloud-lab.py submit --pr 42 --publish

# Use a specific backend
./scripts/cloud-lab.py submit --pr 42 --backend rust-basic --publish

# Test a fork branch
./scripts/cloud-lab.py submit --pr 42 --repo Amperstrand/tollgate-module-basic-go

# Check run status
./scripts/cloud-lab.py status-run --run-id <run-id>

# Clean up stale VMs
./scripts/cloud-lab.py cleanup-stale
```

**Provider selection:** Set `TOLLGATE_VM_PROVIDER` env var: `shc` (default), `gcloud`, `local`, or `physical`.

### GCP Cloud Lab (DEPRECATED — use SHC)

> **Deprecated.** The GCP provider costs ~$0.10/run vs ~$0.01/run on SHC and its runner snapshot is no longer baked (the last one was deleted). Submit paths fail fast with a "snapshot not found" error pointing at SHC. Everything below is kept for reference; to revive it, run `scripts/bake-snapshot.py`, update `SNAPSHOT_NAME` in `lib/cloud_lab/constants.py`, and remove the `ensure_runner_snapshot` guard notes.

Same workflow as SHC but uses Google Cloud Platform. Requires `gcloud` CLI authenticated and a GCP runner snapshot.

```bash
# Identical commands — just set the provider
TOLLGATE_VM_PROVIDER=gcloud ./scripts/cloud-lab.py submit --pr 42 --publish
```

See the [detailed GCP section](#cloud-lab-gcp-fire-and-forget) below for snapshot baking and architecture.

### Physical Router (real hardware)

For testing against real GL-MT3000 routers on your LAN:

```bash
# Test a PR against your physical router
./scripts/test-pr.sh --pr 42

# Specify backend and router
./scripts/test-pr.sh --pr 42 --backend go --router lab-router-a
```

### Backend Selection

All venues support testing different TollGate implementations:

| Backend | `--backend` flag | Repository |
|---|---|---|
| Go (production) | `go` (default) | `OpenTollGate/tollgate-module-basic-go` |
| Go with CDK | `go-cdk` | `OpenTollGate/tollgate-module-basic-go` |
| Rust 1:1 clone | `rust-basic` | `felixfelix-bot/tollgate-module-basic-rust` |
| Experimental Rust | `rust` | `Amperstrand/tollgate-rs-ai-research-and-experiments` |

Backend types are defined in [`lib/backend.py`](lib/backend.py) (`BACKEND_TYPES` / `BACKEND_CHOICES_CLI`) — the single source of truth used by all scripts.

## Prerequisites

- macOS or Linux host on the same LAN as the router
- Python 3.12 (for pytest and cashu CLI)
- Node.js 18+ (for Playwright)
- `sshpass` (`brew install sshpass` or `apt install sshpass`)
- `gh` CLI (for `test-pr.sh` PR resolution)
- ADB (for phone tests, optional)
- Go 1.24+ (only for local `deploy.sh` builds)

## Setup

```bash
# 1. Copy environment template and fill in your values
cp .env.example .env
# edit .env with your router IP, password, etc.

# 2. Set up Python venv with pytest and dependencies
./scripts/setup-python.sh
source ~/.tollgate-test-venv/bin/activate

# 3. Install the cashu CLI (needed for fund/drain token tests)
./scripts/setup-cashu.sh

# 4. Install Playwright
npm install
npx playwright install

# 5. Install git hooks (shellcheck pre-commit)
./scripts/setup-hooks.sh
```

## Quick Start: Test a PR

The primary workflow for testing a pull request:

```bash
# Activate Python venv
source ~/.tollgate-test-venv/bin/activate

# Test PR #42 against your router (API tests only)
./scripts/test-pr.sh --pr 42

# Test a branch, reset router first, run all tests
./scripts/test-pr.sh --branch feat/luci-admin-ui --reset --test all

# Test and publish report to gh-pages
./scripts/test-pr.sh --pr 42 --test api --publish

# Specify a particular router
./scripts/test-pr.sh --pr 42 --router lab-router-a
```

`test-pr.sh` handles the full workflow: resolve PR to branch/commit, verify router connectivity, factory reset (if `--reset`), deploy, run tests, parse JUnit results, generate HTML report, and optionally publish.

## Cloud lab (GCP, fire-and-forget) — DEPRECATED

> Superseded by the [SHC provider](#shc--sovereign-hybrid-compute-cloud-vm) (~10x cheaper, ~$0.01/run). The GCP runner snapshot was deleted; `submit`/`up` now fail fast via `ensure_runner_snapshot()` in `lib/cloud_lab/gcp.py`. Content below is reference for a future re-bake.

Run API + container E2E + virtual WiFi tests in a nested-virt GCP VM (OpenWrt + Debian client). No physical router required.

**Prerequisites:** `gcloud` CLI (authenticated), `gh` CLI (authenticated), `GH_TOKEN` or `gh auth login`, and a GCP runner snapshot. `tollgate-runner-baked-v9` is the current snapshot (local mints, WiFi packages, vwifi binaries, management network, ~25min total).

```bash
# Wait for upstream CI x86_64 artifact, spawn autonomous VM, exit immediately
./scripts/cloud-lab.py submit --pr 42 --publish

# Test against a different repo's PR
./scripts/cloud-lab.py submit --pr 42 --repo OpenTollGate/tollgate-module-basic-go --publish

# By commit (use --branch if not on an open PR)
./scripts/cloud-lab.py submit --commit abc1234 --branch feat/foo --publish

# By branch name
./scripts/cloud-lab.py submit --branch feat/foo --publish

# Block until VM self-deletes
./scripts/cloud-lab.py submit --pr 42 --publish --wait

# Check run status / tail logs
./scripts/cloud-lab.py status-run --run-id 20260519T120000Z-abc1234

# Remove orphaned run VMs (older than 2h)
./scripts/cloud-lab.py cleanup-stale
```

The VM clones this test framework, boots OpenWrt + Debian QEMU VMs, starts 3 local Cashu mints (CDK V2, Nutshell V1+V2), deploys the TollGate `.ipk`, runs pytest (~94 tests, ~25min), publishes to [tests.tollgate.me](https://tests.tollgate.me/), posts a PR comment, and deletes itself.

`submit` waits for an **in-progress or completed** upstream CI build with a downloadable `x86_64` artifact. It does not trigger new CI builds.

Cloud runs are designed to be fire-and-forget and parallelizable: each run gets its own GCP VM, run directory, and report URL. Publishing to `gh-pages` retries up to 10 times without force-push, pulling/rebasing and waiting a random 0-60 seconds between attempts to avoid races between concurrent runs.

### Architecture

```
GCP Host VM (n2-standard-2)
  ├── tg-poc-br (10.99.99.0/24) — test LAN
  │     ├── host: 10.99.99.2 (mints, NAT, syslog capture)
  │     ├── alpha: 10.99.99.1 (OpenWrt QEMU — TollGate under test)
  │     └── debian: 10.99.99.100 (Debian QEMU — Playwright, cashu)
  ├── mgmt-br (10.99.97.0/24) — management (SSH independent of test bridges)
  │     ├── host: 10.99.97.2
  │     ├── alpha: 10.99.97.1
  │     └── debian: 10.99.97.100
  ├── Local mints: CDK V2 (:8383), Nutshell V2 (:8384), Nutshell V1 (:8385)
  ├── mac80211_hwsim: virtual WiFi radios for AP/STA testing
  └── vwifi: cross-VM 802.11 frame relay (TCP, port 8212)
```

### Baking a GCP runner snapshot

```bash
./scripts/bake-snapshot.py bake
```

The baker installs `gh`, `gcloud`, `/opt/tollgate-venv`, `/opt/cashu-venv`, `/opt/cdk-mintd`, WiFi packages (`kmod-mac80211-hwsim`, `wpad-basic`, `iw-full`, `iwinfo`), vwifi binaries (`/opt/vwifi/bin/`), and pre-provisioned OpenWrt/Debian base images. It runs with `HOME=/root`, matching the GCP startup worker. After baking, verify the snapshot with a throwaway run before updating `SNAPSHOT_NAME` in `lib/cloud_lab/constants.py`.

## Deploy to Router

Two deployment paths are available:

### Option 1: CI-built artifact (recommended)

Download a production-grade `.ipk` from GitHub Actions and deploy it:

```bash
TOLLGATE_LUCI_PASSWORD=<password> ./scripts/deploy-ci.sh <branch> [run-id] [router-ip]
```

This downloads the artifact built by the same CI pipeline used for releases, copies it to the router, installs via `opkg`, restarts services, and verifies.

```bash
TOLLGATE_LUCI_PASSWORD=secretpass ./scripts/deploy-ci.sh feat/luci-admin-ui
```

Use `download-ci-artifact.sh` for the download step only (no deploy).

### Option 2: Local build

Build the ipk from source and deploy it:

```bash
TOLLGATE_LUCI_PASSWORD=<password> ./scripts/deploy.sh <git-hash>
```

Takes a branch name, tag, or commit hash. Defaults to router at `192.168.13.112`.

## Run Tests

### Individual test tiers

Every runner produces a canonical run directory under `results/`:

```bash
# Playwright LuCI UI tests
TOLLGATE_LUCI_PASSWORD=<password> ./scripts/run-tests.sh [tollgate-commit] [desktop|mobile] [router-id]

# pytest API tier only (no phone needed)
./scripts/run-api.sh

# pytest phone tier only (requires Android device via ADB)
./scripts/run-phone.sh

# All tiers: Playwright + pytest API + pytest phone
./scripts/run-all.sh
```

The Playwright `run-tests.sh` commit hash is optional. If omitted, it falls back to `TOLLGATE_BRANCH` then `HEAD`. The script verifies SSH connectivity before running. Defaults to `desktop` viewport.

Each runner accepts `--no-render` (skip report generation) and `--run-dir` (reuse an existing run directory).

### Run directory layout

Every run produces this structure:

```
results/<run_id>/                # e.g. 20260516T172600Z-abc1234
  run.json                       # Canonical metadata (schema_version 1)
  summary.json                   # Per-test outcomes for rendering
  report/
    index.html                   # Unified static HTML report
  raw/
    api/
      junit.xml                  # pytest JUnit output
      report.html                # pytest-html output
      output.log                 # Captured stdout/stderr
    phone/
      junit.xml
      report.html
      output.log
    playwright/
      results.json               # Playwright JSON report
      report/                    # Playwright HTML report
      output.log
  artifacts/
    logs/
    screenshots/
    traces/
```

Runner subdirectories that weren't run are simply absent. The unified `report/index.html` links to native framework reports.

### Makefile targets (pytest / CI / reports)

| Target | What it runs |
|---|---|
| `make pytest-smoke` | Smoke tests (~15s, API-only) |
| `make pytest-critical` | Core functionality (~2min) |
| `make pytest-extended` | Full suite including edge cases (~10min) |
| `make pytest-api` | All API-marked tests |
| `make pytest-phone` | All phone-marked tests |
| `make pytest-test` | All pytest tests (api + phone + scenarios) |
| `make pytest-scenarios` | Hardware scenario tests only (`-m hardware`) |
| `make pymake-help` | List targets migrated to pytest |
| `./scripts/pymake.py <target> --router alpha` | Run a migrated Makefile target via pytest |
| `make luci` | Playwright LuCI UI tests |
| `make run-api` | Run API tier with canonical run dir |
| `make run-phone` | Run phone tier with canonical run dir |
| `make run-luci` | Run Playwright tests with canonical run dir |
| `make run-all` | Run all tiers into one run directory |
| `make collect` | Collect results from latest run dir |
| `make render-report` | Render report from latest run dir |
| `make pytest-smoke-mac` / `make pytest-api-mac` / `make pytest-test-mac` | Same as above, using macOS WiFi client instead of ADB |
| `make pytest-smoke-linux` / `make pytest-api-linux` / `make pytest-test-linux` | Same, using Linux NetworkManager client |
| `make deploy-ci` | Deploy CI-built `.ipk` from GitHub Actions |
| `make setup` | Install all dependencies (npm + playwright + serial) |
| `make setup-python` | Create Python venv |
| `make sanitize` | Sanitize latest result set |
| `make publish` | Publish latest result set to gh-pages |
| `make clean` | Remove results and caches |

See "Hardware Makefile Target Reference" below for physical hardware test targets.

### Pytest tiers and markers

Markers are defined in `pytest.ini`. Tiers are hierarchical: `smoke` is a subset of `critical`, which is a subset of `extended`.

| Marker | Description |
|---|---|
| `smoke` | Quick sanity check (~15s, API-only) |
| `critical` | Core functionality (~2min) |
| `extended` | Full suite including edge cases (~10min) |
| `api` | API-only test, no phone needed |
| `phone` | Requires physical phone connected via ADB |
| `config` | Modifies router configuration (pricing/metric) |
| `slow` | Takes more than 30s |
| `android_only` | Requires Android device |
| `publish_screenshot` | Screenshot from this test is safe for published reports |
| `pr(N)` | PR-gated test — only runs with `--expected-pr=N` (see below) |

Phone-marked tests automatically get `flaky(reruns=1)` and `timeout(300s)`.

### Test gating strategies

Tests use runtime feature detection to decide whether they should run against the deployed firmware. The `pr(N)` marker mechanism still exists in `conftest.py` for CI use, but **all test files use feature detection or unconditional execution**.

#### 1. Feature-detected (skip if feature absent)

Use when a test verifies behavior that **may or may not exist** in the deployed firmware. The test calls a skip helper at runtime that probes the router for the required capability:

```python
def _skip_if_no_degraded_support(router):
    resp = router.get_tollgate_status()
    if resp.get("success") is not True:
        pytest.skip("status command not available")
    raw = json.dumps(resp).lower()
    if not any(kw in raw for kw in ["degraded", "reachable", "mint_health"]):
        pytest.skip("no degraded mode support detected")
```

These tests run against **any** firmware that implements the feature. They skip cleanly when the feature is absent.

#### 2. Bug regression (xfail if fix absent)

Use `gate_bug_fix()` from `lib/helpers` for tests that verify a **known bug has been fixed**. When the fix is absent, the test is marked xfail ("known issue" in reports). When the fix is present, the test runs normally — a failure means the fix doesn't work (regression).

```python
from lib.helpers import gate_bug_fix

def test_profit_share_boot_with_invalid_config(router, profit_share_config_guard):
    gate_bug_fix(
        _has_profit_share_validation(router),
        bug_id="profit-share-no-validation",
        fix_pr="PR #86",
    )
    # ... test body runs only if fix is present ...
```

For bug cross-references, link to the incident in the knowledgebase:

```
See: https://github.com/OpenTollGate/tollgate-knowledgebase/tree/main/incidents/YYYY-MM-DD_slug.md
```

#### 3. Unconditional (baseline behavior)

No gating at all. Runs against every firmware. Use for baseline API tests (health endpoints, config format, etc.).

#### When to use which

| Situation | Strategy |
|---|---|
| Feature may or may not be present | Feature detection (`_skip_if_no_*`) |
| Known bug, fix in flight | `gate_bug_fix()` (xfail = warning, fail = regression) |
| Baseline behavior that always works | No gating — runs unconditionally |

## Project Structure

```
physical-router-test-automation/
  .env                          # Local environment variables (gitignored)
  .env.example                  # Template for .env
  AGENTS.md                     # Operational knowledge for agents/humans
  Makefile                      # Common targets
  pytest.ini                    # Pytest configuration and markers
  requirements.txt              # Python dependencies
  package.json                  # Node/Playwright dependencies
  config/
    routers.example.json        # Router inventory template
    routers.json                # Private router inventory (gitignored)
  credentials/                  # Router credentials (gitignored)
  findings/                     # Per-PR test findings with campaign summary
  lib/                          # Shared Python library
    cashu.py                    # CashuMint helper
    router.py                   # SSH/router interaction
    helpers.py                  # Shared test helpers
    nostr.py                    # Nostr event helpers
    deploy.py                   # Deploy and factory reset logic
    constants.py                # Shared constants
    clients/
      adb.py                    # ADB device control
      desktop.py                # macOS/Linux WiFi clients
      wifi.py                   # WiFi connection management
  plans/                        # YAML test plans
    *.yaml                      # Test plan definitions
  scripts/                      # 26+ scripts (see below)
  tests/
    conftest.py                 # Shared pytest fixtures (router, adb, cashu, wifi, deploy)
    api/                        # 38 pytest API test files
    phone/                      # 20 pytest phone test files
    scenarios/                  # Hardware scenario tests (boot hygiene, two-router, etc.)
    unit/                       # Unit tests for framework scripts
      test_collect_results.py   # Tests for collect-results.py
    web/                        # Playwright LuCI UI tests
    destructive/                # Playwright destructive tests (reboot, firmware)
    protocol/                   # Playwright protocol tests (payment, data allotment)
    helpers/                    # Shared Playwright helpers
    report/                     # Generated reports
  mint-health/                  # Physical router mint health tests (Makefile-driven)
    Makefile                    # ~3000 lines of SSH/serial/hybrid test targets
    docs/                       # Test plans, mutex documentation
    routers.env.example         # Router connection template
  upstream-wifi/                # Physical router upstream WiFi tests (Makefile-driven)
    Makefile                    # ~600 lines of upstream WiFi test targets
    docs/                       # Test reports, incident notes
    routers.env.example         # Router connection template
  esp32/                        # ESP32 multi-board firmware tests
    Makefile                    # Per-board locks, flashing, CVM, relay, arch targets
    boards.env                  # Board serial port and AP mappings
```

## Scripts

All 26 scripts in `scripts/`:

### Test execution

| Script | Purpose |
|---|---|
| `test-pr.sh` | **Primary workflow script.** Unified PR testing: deploy, run tests, generate report. Usage: `--pr N` or `--branch NAME`, `--reset`, `--test api\|all`, `--publish`, `--router ID` |
| `run-tests.sh` | Run Playwright LuCI UI tests against physical router |
| `run-api.sh` | Run pytest API tier (no phone needed). Writes JUnit XML + HTML report |
| `run-phone.sh` | Run pytest phone tier (requires ADB device). Longer timeout (300s per test) |
| `run-all.sh` | Run everything: Playwright LuCI + pytest API + pytest phone |
| `run-browser-tests.sh` | Run Playwright tests on a remote browser host (TOLLGATE_BROWSER_HOST). Syncs tests via rsync, runs via SSH |

### Deployment

| Script | Purpose |
|---|---|
| `deploy-ci.sh` | Download CI-built `.ipk` from GitHub Actions and deploy to router (recommended) |
| `deploy.sh` | Build `.ipk` from source and deploy to router |
| `download-ci-artifact.sh` | Download CI artifact only (no deploy step) |
| `deploy-rust-ci.sh` | Download Rust v1 CI-built `.ipk` from GitHub Actions and deploy to router |
| `download-rust-ci-artifact.sh` | Download Rust v1 CI artifact only (no deploy step) |
| `provision-router.sh` | Bootstrap/provision a fresh router with required dependencies and configuration |

### Firmware and recovery

| Script | Purpose |
|---|---|
| `build-firmware.py` | Build clean OpenWrt firmware images via ASU API with embedded SSH key and random root password. Reads from `config/routers.json`. Usage: `--router ID`, `--flash`, `--key <path>` |
| `uboot-recover.py` | Automated U-Boot recovery for bricked routers. Supports GL.iNet GL-MT3000 and D-Link COVR-X1860. Uses pcap monitoring and event-driven state machine. Voice guidance on macOS |
| `flash-routers.mjs` | Bulk ethernet hotplug sysupgrade flashing. Disabled unless `TOLLGATE_ENABLE_SYSUPGRADE_FLASHING=true` |

### Reporting and publishing

| Script | Purpose |
|---|---|
| `generate-pr-comment.py` | Generate markdown PR comment from test results, grouped by upstream PR |
| `generate-run-metadata.py` | Generate `run.json` metadata by querying router version info over SSH |
| `strip-screenshots.sh` | Strip non-whitelisted screenshots from Playwright HTML reports (keeps only `publish_screenshot` annotated) |
| `sanitize-results.sh` | Redact sensitive data (IPs, passwords, tokens, MACs, phone serials) from test results for public publication |
| `publish-report.sh` | Publish test report to gh-pages with dashboard index. Purges old runs beyond `TOLLGATE_GH_PAGES_KEEP` |
| `collect-results.py` | Canonical result parser — reads JUnit XML + Playwright JSON, writes `run.json` + `summary.json` into run directory |
| `render-report.py` | Self-contained HTML report generator from canonical `run.json`/`summary.json`. No external dependencies |
| `record-demo.py` | Synchronized demo recorder — runs a clientd session with router logs + status bar on one clock, renders a scrubbable `player.html` and optional `.webm` (make target: record-demo-clientd). See [docs/demo-recording.md](docs/demo-recording.md) |

### Setup

| Script | Purpose |
|---|---|
| `setup-cashu.sh` | Install and patch cashu CLI for testnet token minting |
| `setup-python.sh` | Create Python venv at `~/.tollgate-test-venv` with pytest and dependencies |
| `setup-hooks.sh` | Install shared git hooks from `.githooks/` (shellcheck pre-commit). Run once after cloning |

### Lab management

| Script | Purpose |
|---|---|
| `cloud-lab.py` | GCP/SHC cloud lab — `submit` (fire-and-forget PR/commit tests), `status-run`, `cleanup-stale`. Supports `--backend go\|rust-basic\|rust` |
| `cloud-lab-worker.sh` | Autonomous worker entrypoint (runs on GCP/SHC VM via startup script) |
| `cost-status.py` | Audit all billable resources (SHC + GCP VMs, snapshots, backups, images, disks, IPs). Classifies APPROVED vs UNAPPROVED against `config/approved-resources.yaml`, warns on stopped-but-billable VMs, shows spend over 24h/7d/30d + burn rate. Exit 1 = unapproved resources. `--export-reaper-env` feeds the allowlist to the VM reaper |
| `virtual-lab.py` | Manage local TollGate virtual lab — `start-poc`, `stop-poc`, `doctor`, `debug-poc`. Supports `--backend go\|rust-basic\|rust` |
| `run-local-tests.sh` | Local QEMU test runner — starts fakewallet CDK mint, configures OpenWrt, runs pytest. Results stay local |
| `deploy-local-vm.sh` | Deploy TollGate + NDS + captive portal to a local QEMU OpenWrt VM (idempotent) |

## Test Directories

### `tests/api/` (38 test files, API-only, no phone needed)

SSH directly to the router. No physical device required. Covers:

- Health endpoint, info endpoint, hostname
- Captive portal API, session endpoint, session expiry and scan
- Concurrent payments, pay response structure
- Crypto random password generation
- Degraded mode, discovery mints, dual mint
- Edge tokens, minimum token, pending token CGI
- Log beacon CGI, notice event
- LuCI admin UI
- Mint 502 handling, mint health, mint payout, mint URL normalization, wrong mint
- Netbird firewall
- NUT-24, profit share validation
- Post-payment redirect (NDS redirecturl config, runtime override, setup script)
- CLI version, CLI wallet

### `tests/phone/` (20 test files, requires Android device via ADB)

End-to-end through the captive portal on a real Android phone. Covers:

- Auto connect, paste URL, URL param
- Backend restart, edge cases
- Camera captive portal
- Data metering, time metering
- Expiry kick, extend session, short session
- Post-payment redirect (browser opens after payment)
- Session persistence
- Token formats

### `tests/web/` (Playwright LuCI tests)

`tollgate.spec.mjs` - LuCI admin UI tests:
- **Tab loading** - all 5 tabs render without errors
- **Dashboard** - restart modal, fund warning, drain modal
- **Network** - show password, rename SSID round-trip, change password, enable/disable
- **Configuration** - profit share sliders, add/remove mint/share/identity, save round-trip, proportional squeeze
- **Advanced** - JSON validation, reload files, identity editor
- **Fund/Drain** - real testnut.cashu.exchange tokens, SSH file verification, lifecycle round-trips, drain-twice zero check

### `tests/destructive/` (Playwright, 2 tests)

- `reboot-recovery.spec.mjs` - router comes back online after reboot with settings intact, network connectivity restored
- `firmware-upgrade.spec.mjs` - safe `.ipk` package install by default; full sysupgrade intentionally skipped unless `TOLLGATE_ENABLE_SYSUPGRADE_TESTS` is set

### `tests/protocol/` (Playwright, 4 tests, require additional hardware or opt-in)

- `payment-lifecycle.spec.mjs` - payment flow from discovery to connectivity
- `data-allotment.spec.mjs` - bandwidth consumption until paid data allotment closes connectivity ([detailed docs](docs/data-allotment-testing.md))
- `router-network-config.spec.mjs` - OpenWrt `wwan`/station-mode UCI configuration and network restart verification
- `tollgate-payment-protocol.spec.mjs` - TollGate discovery event, Cashu payment token, Nostr payment event signing via `nak`, client connectivity verification

### `tests/helpers/` (shared Playwright modules)

| Module | Purpose |
|---|---|
| `command.mjs` | Run commands via SSH |
| `inventory.mjs` | Router inventory loading |
| `network.mjs` | Network configuration helpers |
| `payment-protocol.mjs` | Payment protocol test helpers |
| `router-config.mjs` | Router configuration management |
| `router-files.mjs` | Router file operations |
| `router-packages.mjs` | Package management (opkg) |
| `router-wallet.mjs` | Wallet fund/drain operations |
| `ssh.mjs` | SSH connection management |

### `tests/conftest.py` (shared pytest fixtures)

Session-scoped fixtures: `router` (SSH), `adb` (phone or desktop client), `cashu` (mint helper), `wifi` (WiFi management), `deploy_session` (auto-deploy before tests). Per-test fixtures: `connected_wifi`, `test_pricing`, `screenshot_portal`, `screenshot_raw`. Handles auto-screenshot on failure, phone UI XML capture, and debug logging.

## Shared Library (`lib/`)

| Module | Purpose |
|---|---|
| `cashu.py` | `CashuMint` helper for minting/burning testnet tokens |
| `router.py` | `Router` class for SSH interaction, state management, log collection |
| `helpers.py` | Shared test utility functions |
| `nostr.py` | Nostr event creation and signing helpers |
| `deploy.py` | Deploy branch from CI, factory reset, health checks |
| `constants.py` | Shared constants (step sizes, defaults) |
| `clients/adb.py` | `ADBDevice` for Android phone control (tap, swipe, screenshot, UI dump) |
| `clients/desktop.py` | `MacWiFiClient`, `LinuxWiFiClient` and adapters for desktop testing without a phone |
| `clients/wifi.py` | `WiFi` connection management (connect, disconnect, reconnect) |

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `TOLLGATE_LUCI_PASSWORD` | Yes | - | Router SSH and LuCI password |
| `TOLLGATE_LUCI_URL` | No | `http://192.168.13.112:8080` | LuCI admin URL |
| `TOLLGATE_LUCI_USER` | No | `root` | LuCI/SSH username |
| `TOLLGATE_SSH_HOST` | No | derived from `TOLLGATE_LUCI_URL` | Router IP for SSH |
| `TOLLGATE_SSH_PASSWORD` | No | falls back to `TOLLGATE_LUCI_PASSWORD` | Separate SSH password |
| `TOLLGATE_SSH_USER` | No | `root` | SSH username |
| `TOLLGATE_SSH_KEY` | No | - | SSH key path used by pytest fixtures and `uboot-recover.py` |
| `TOLLGATE_ROUTER_ID` | No | - | Router ID from `config/routers.json` |
| `TOLLGATE_ROUTER_INVENTORY` | No | `config/routers.json` | Path to router inventory file |
| `TOLLGATE_ROUTER_MODEL` | No | `unknown` | Router model identifier |
| `TOLLGATE_ROUTER_ARCH` | No | `aarch64_cortex-a53` | Router architecture for ipk builds |
| `TOLLGATE_BACKEND` | No | `go` | Backend type: `go`, `go-cdk`, `rust-basic`, or `rust`. See [Backend Selection](#backend-selection) |
| `TOLLGATE_CLIENT_TYPE` | No | `adb` | Client type for metadata: `adb`, `mac`, `linux`, or `container` |
| `TOLLGATE_VIEWPORT` | No | `desktop` | Viewport: `desktop` or `mobile` |
| `TOLLGATE_SSID` | No | `TollGate` | TollGate WiFi SSID |
| `TOLLGATE_SSID_PREFIX` | No | `TollGate-` | Prefix for TollGate WiFi SSIDs |
| `TOLLGATE_WIFI_INTERFACE` | No | - | Host WiFi interface for client tests |
| `TOLLGATE_UPSTREAM_SSID` | No | - | Upstream WiFi SSID for station-mode tests |
| `TOLLGATE_UPSTREAM_WIFI_PASSWORD` | No | - | Upstream WiFi password |
| `TOLLGATE_DOMAIN` | No | - | Router domain for DNS-based tests |
| `TOLLGATE_CLIENT_IP` | No | auto-detected | Client IP for phone/desktop tests |
| `TOLLGATE_CLIENT_MAC` | No | auto-detected | Client MAC address |
| `TOLLGATE_ENABLE_WIFI_CLIENT_TESTS` | No | `false` | Enable tests that change host WiFi |
| `TOLLGATE_ENABLE_DATA_ALLOTMENT_TESTS` | No | `false` | Enable bandwidth consumption tests |
| `TOLLGATE_PAYMENT_STEPS` | No | `100` | Number of payment steps for protocol tests |
| `TOLLGATE_CONNECTIVITY_HOST` | No | `8.8.8.8` | Host to ping for connectivity checks |
| `TOLLGATE_TEST_MINT_URL` | No | `https://testnut.cashu.exchange` | Cashu mint URL for test tokens |
| `TOLLGATE_DATA_TEST_URL` | No | `https://nbg1-speed.hetzner.com/100MB.bin` | URL for data allotment download |
| `TOLLGATE_DATA_TEST_TIMEOUT` | No | `300` | Timeout in seconds for data test |
| `TOLLGATE_PACKAGE_PATH` | No | - | Path to `.ipk` for safe package-upgrade test |
| `TOLLGATE_ETHERNET_INTERFACES` | No | - | Comma-separated ethernet interfaces for sysupgrade flashing |
| `TOLLGATE_FIRMWARE_IMAGE` | No | - | Path to firmware image for sysupgrade test/flashing |
| `TOLLGATE_ENABLE_SYSUPGRADE_TESTS` | No | `false` | Enable full firmware sysupgrade test (requires external recovery) |
| `TOLLGATE_ENABLE_SYSUPGRADE_FLASHING` | No | `false` | Enable `scripts/flash-routers.mjs` sysupgrade flashing |
| `TOLLGATE_SYSUPGRADE_WIPE_CONFIG` | No | `false` | Add `sysupgrade -n`; dangerous, wipes SSH/network/LuCI config |
| `TOLLGATE_FIRMWARE_PASSWORD` | No | random | Override firmware root password in `build-firmware.py` |
| `TOLLGATE_EXPECTED_MAC` | No | - | Expected router MAC for factory reset guard |
| `TOLLGATE_CASHU_VENV` | No | `/opt/cashu-venv` | Path to cashu CLI venv |
| `TOLLGATE_PYTHON_VENV` | No | `~/.tollgate-test-venv` | Path to Python test venv |
| `PHONE_SERIAL` | No | - | Android device serial for ADB (required for phone tests) |
| `PHONE_PIN` | No | - | Android device PIN for unlock |
| `TOLLGATE_PUBLISH` | No | `false` | Publish test report to gh-pages |
| `TOLLGATE_BRANCH` | No | - | Branch name for report metadata |
| `TOLLGATE_PR` | No | - | PR number for report metadata |
| `TOLLGATE_GH_PAGES_KEEP` | No | `10` | Number of report runs to keep on gh-pages |

## cashu CLI Notes

The test suite uses [cashu](https://github.com/cashubtc/cashu) to mint testnet tokens from `testnut.cashu.exchange` (a FakeWallet mint that auto-pays invoices). The `setup-cashu.sh` script applies a one-line patch to cashu's `models.py` to handle a version mismatch with the testnut mint's API (missing `active` field on keysets).

## Migrated Physical-Router Coverage

The framework keeps the Playwright LuCI UI suite and adds opt-in physical-router coverage extracted from the old `tollgate-module-basic-go/tests` directory. These now live in `tests/protocol/` and `tests/destructive/`:

- `tests/protocol/router-network-config.spec.mjs` - OpenWrt `wwan`/station-mode UCI configuration and network restart verification.
- `tests/protocol/tollgate-payment-protocol.spec.mjs` - TollGate discovery event, Cashu payment token, Nostr payment event signing via `nak`, and client connectivity verification.
- `tests/protocol/data-allotment.spec.mjs` - bandwidth consumption until the paid data allotment closes connectivity ([detailed docs](docs/data-allotment-testing.md)).
- `tests/protocol/payment-lifecycle.spec.mjs` - end-to-end payment flow from discovery to connectivity.
- `tests/destructive/firmware-upgrade.spec.mjs` - safe `.ipk` package install by default; full sysupgrade is intentionally skipped unless explicitly enabled.
- `scripts/flash-routers.mjs` - disabled-by-default ethernet hotplug sysupgrade utility for physical routers.

Routine development should prefer `TOLLGATE_PACKAGE_PATH=<package.ipk> npm test`: `opkg install` updates TollGate without changing Dropbear, firewall, LuCI/uhttpd, LAN/WAN, or wallet/config state. Full `sysupgrade` is reserved for release/image QA because stock firmware may not include this lab router's custom recovery-critical state (authorized SSH key, SSH from WAN, 8080 LuCI listener, custom LAN addressing). Only enable sysupgrade tests or flashing when you have a separate recovery path.

Network-changing tests are opt-in and skip unless their required `TOLLGATE_*` environment variables are set. No router passwords, upstream WiFi credentials, firmware image paths, reports, screenshots, or generated results belong in git.

## Multi-Router Inventory

For multiple router models, copy `config/routers.example.json` to `config/routers.json` and set `TOLLGATE_ROUTER_ID`. The private inventory file is ignored by git.

```bash
cp config/routers.example.json config/routers.json
TOLLGATE_ROUTER_ID=lab-router-a ./scripts/run-tests.sh <tollgate-commit>
```

---

## Physical Hardware Test Suites (pytest + pymake)

Hardware lab tests are **pytest-first**. Root Makefile targets (`make smoke-degraded`, etc.) are thin stubs that print a migration notice and run [`scripts/pymake.py`](scripts/pymake.py). Mapping lives in [`config/make-pytest-map.yaml`](config/make-pytest-map.yaml).

All hardware test targets require a hardware lock (`make lock PHASE="description"`) to prevent concurrent access by multiple sessions.

### Quick Start

```bash
# 1. Install dependencies
make setup

# 2. Configure routers
cp mint-health/routers.env.example mint-health/routers.env     # edit with real IPs/passwords
cp upstream-wifi/routers.env.example upstream-wifi/routers.env

# 3. Acquire hardware lock
make lock PHASE="testing degraded mode"

# 4. Run tests (equivalent: make smoke-degraded ROUTER=alpha)
./scripts/pymake.py smoke-degraded --router alpha
./scripts/pymake.py smoke-upstream --router alpha    # needs two routers in routers.env
make pytest-scenarios ROUTER=alpha                   # all hardware scenarios

# Playwright (still JS, orchestrated by pymake)
./scripts/pymake.py test-captive-portal --router alpha

# 5. Release lock when done
make unlock
```

Legacy `mint-health/Makefile` shell implementations remain for unmigrated targets; see [`docs/make-to-pytest-migration.md`](docs/make-to-pytest-migration.md).

### Hardware Directory Structure

```
Makefile              # Top-level runner — make <target> ROUTER=alpha
mint-health/          # Mint health tests (degraded mode, offline payment, recovery)
  Makefile            # ~3000 lines of SSH/serial/hybrid router test targets
  docs/
    router-test-plan.md   # Full test plan for degraded merchant mode
    router-mutex.md       # Hardware mutex documentation
  routers.env.example
upstream-wifi/        # Upstream WiFi daemon tests (scan, connect, switch, reseller mode)
  Makefile            # ~600 lines of upstream WiFi test targets
  docs/
    upstream-wifi-test-report.md
    router-b-incident-2026-04-30.md
  routers.env.example
esp32/                # ESP32 multi-board firmware tests
  Makefile            # Per-board locks, flashing, CVM, relay, arch tests
  boards.env          # Board serial port and AP mappings
```

### Mint Health Test Quick Reference

```bash
# Single-router degraded mode lifecycle (~3 min)
make smoke-degraded ROUTER=alpha

# Two-router combined test (~5 min)
make smoke-upstream                          # Scenario A: connect online, then degrade
make smoke-degraded-connect                  # Scenario B: connect while degraded (RISKY)

# Dynamic rebuild test (~10 min)
make smoke-dynamic-rebuild ROUTER=alpha

# STA health check
make check-sta-health ROUTER=alpha
```

See `mint-health/docs/router-test-plan.md` for the full test plan.

### Upstream WiFi Test Quick Reference

```bash
# Smoke test (~5 min)
make smoke-upstream-full ROUTER=alpha SSID=MyNet PASS=secret

# Full test suite (~30 min)
make full-upstream ROUTER=alpha SSID=MyNet PASS=secret

# STA health check
make check-sta-health ROUTER=alpha
```

See `upstream-wifi/docs/upstream-wifi-test-report.md` for test results and `docs/router-b-incident-2026-04-30.md` for incident notes.

### Hardware Makefile Target Reference

| Target | What it does |
|---|---|
| `make hw-deploy ROUTER=alpha` | Cross-compile and deploy daemon + CLI to router |
| `make deploy-develop ROUTER=alpha` | Deploy from develop worktree |
| `make smoke-degraded ROUTER=alpha` | Single-router degraded mode lifecycle (~3 min) |
| `make smoke-upstream` | Two-router degraded upstream payment (~5 min) |
| `make smoke-dynamic-rebuild ROUTER=alpha` | Full→degraded→full lifecycle |
| `make full-all ROUTER=alpha` | All hardware test suites combined |
| `make test-captive-portal ROUTER=alpha` | Playwright captive portal browser tests |
| `make test-cashu-payment ROUTER=alpha` | Playwright e2e cashu payment |
| `make test-ssl-full ROUTER=alpha` | Full SSL lifecycle test |
| `make test-hostname ROUTER=alpha` | Verify hostname configuration |
| `make lock PHASE="desc"` | Acquire router hardware lock |
| `make unlock` | Release router hardware lock |
| `make lock-status` | Show all lock statuses (routers + ESP32 boards) |

### Pytest Makefile Target Reference

| Target | What it runs |
|---|---|
| `make pytest-smoke` | Smoke tests (~15s, API-only) |
| `make pytest-critical` | Core functionality (~2min) |
| `make pytest-extended` | Full suite including edge cases (~10min) |
| `make pytest-api` | All API-marked tests |
| `make pytest-phone` | All phone-marked tests |
| `make pytest-test` | All pytest tests |
| `make run-api` | Run API tier with canonical run dir |
| `make run-phone` | Run phone tier with canonical run dir |
| `make run-all` | Run all tiers into one run directory |
| `make collect` | Collect results from latest run dir |
| `make render-report` | Render report from latest run dir |
| `make deploy-ci` | Deploy CI-built `.ipk` to router |
| `make sanitize` | Sanitize latest result set |
| `make publish` | Publish latest result set to gh-pages |

## Serial Console Integration

In addition to SSH, routers can be managed via USB-TTL serial connections. Serial works even when the router has no network connectivity (during cold boot, WiFi reload, or NetBird outages).

### Setup

```bash
# Install serial dependencies
pip3 install -r scripts/requirements-serial.txt

# Add serial port config to routers.env
# See mint-health/routers.env.example for the SERIAL fields
```

See `docs/serial-integration-plan.md` for the full hardware setup guide (USB-TTL adapters, udev rules, mini PC orchestrator).

### Target Prefix Convention

| Prefix | Transport | Use Case |
|--------|-----------|----------|
| `r-`   | SSH       | Normal operations (existing) |
| `s-`   | Serial    | No-network scenarios: cold boot, recovery, monitoring |
| `h-`   | Hybrid    | Tries SSH first, falls back to serial if unreachable |

### Serial Quick Reference

```bash
# Interactive serial console
make serial-shell ROUTER=alpha

# Watch full boot output (reboot or power-cycle the router)
make serial-cold-boot ROUTER=alpha

# Emergency recovery on a stranded router
make serial-recovery ROUTER=alpha CMD="sed -i '/nofee.testnut/d' /etc/hosts && /etc/init.d/tollgate-wrt restart"

# Hybrid: SSH first, serial fallback
make hybrid-status ROUTER=alpha
make hybrid-cleanup ROUTER=alpha
```

## ESP32 Board Testing

The framework also supports testing ESP32-based TollGate hardware (tollgate_core firmware, multi-mint boards, ContextVM, local Nostr relay).

### Quick Reference

```bash
# Flash firmware to Board A
make esp32-lock-a PHASE="testing multi-mint"
make esp32-flash-a

# Run multi-mint tests
make esp32-test-multi-mint-a

# ContextVM (MCP over Nostr) tests
make esp32-test-cvm-a              # CVM announcement test
make esp32-test-cvm-mcp-a          # MCP tools/call end-to-end

# Local relay tests (Board B)
make relay-build
make relay-flash-b
make relay-test-full
```

### Arch (tollgate_core) Component Tests

```bash
make arch-build                   # Build tollgate_core firmware
make arch-flash-a                 # Flash to Board A
make arch-generate-spiffs         # Generate SPIFFS with auto-detected WPA mode
make arch-flash-spiffs-a          # Flash SPIFFS to Board A
make arch-test-full               # Run all arch E2E tests (~4min)
```

Each board has its own lock (`esp32-lock-a`, `esp32-lock-b`, `esp32-lock-c`) separate from the router hardware lock. Use `make lock-status` to see all locks.

## Safety Notes

- `TOLLGATE_SYSUPGRADE_WIPE_CONFIG=true` will wipe all router config (SSH keys, network, LuCI). Only use when you have physical recovery access.
- `scripts/flash-routers.mjs` writes firmware over ethernet. Ensure the correct interface is specified.
- `scripts/uboot-recover.py` involves power cycling and firmware flashing. Follow the voice/prompt instructions carefully.
- Test results may contain router IPs, passwords, and phone serials. Always run `sanitize-results.sh` before publishing.
- The `credentials/` directory contains router passwords and is gitignored. Never commit it.
