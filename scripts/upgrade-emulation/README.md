# v0.5.0 → v0.6.0 Upgrade Emulation Bench (QEMU, ai-legion-small)

Simulated, hardware-free install/upgrade/rollback validation for `tollgate-wrt`,
built during the #113 v0.6.0 release campaign (2026-09-17). Complements the
physical #106 release-matrix runner with a reproducible QEMU venue.

## Layout

| File | Purpose |
|---|---|
| `upgrade-bench.sh` | `up` / `provision` / `down` — bridge+tap+NAT, fresh overlay boot, serial provisioning |
| `boot-vm.sh` | Boot the VM (daemonized qemu, serial+monitor sockets, pidfile) |
| `serial.sh` | One-shot serial-console command runner |
| `run-matrix.sh` | One-command full matrix: clean-install → fund → upgrade → assert → rollback → no-jq robustness → multi-mint |
| `mint2-setup.sh` | Second Cashu mint (CDK 0.18.0 fakewallet @ `10.99.99.2:8383`) for multi-mint tests |
| `repack-preinst-probe.py` | Repack a locally-built tar.gz-ipk with an env-logging preinst (opkg maintainer-script env forensics) |

`run-matrix.sh` exits non-zero on any failure and drops evidence under
`~/upgrade-test/results/<ts>/` (logs, payment responses, sha ledger). Current
expected state against the pre-#407 artifact: **all checks pass except the
no-jq robustness upgrade** — that one is the regression gate for
Amperstrand/tollgate-module-basic-go#93 (preinst jq dependency); it flips to
pass with an artifact built from upstream PR 407. Token minting goes through
this repo's `HttpMinter` (venv at `~/upgrade-test/pyenv` with `coincurve`,
repo `lib/` copied to `~/upgrade-test/prta`) — cdk-cli's interactive `send`
is not deterministic across wallet states (shared `~/.cdk-cli` DB, unit-dual
mint listings, and an 0.18 `Empty IN clause for placeholder: ys` send bug on
fresh V2-keyset wallets).

Host: **ai-legion-small** (not ai-legion — that box is reserved by the #110 soak
while it runs). VM: OpenWrt 24.10.1 x86_64 from
`~/tollgate-virtual-lab/images/openwrt-base.qcow2`, 10.99.99.1/24 behind NAT
(host 10.99.99.2). Build with tmbg `packaging/local-build-ipk.sh` + the pinned
toolchain from `packaging/build-inputs.json` (go1.25.8 + upx 5.2.1 via
`scripts/fetch-upx.sh`).

## Validated protocol (2026-09-17 run, evidence in results/upgrade-emulation/)

1. **Fresh v0.5.0 install** (channel artifact) — clean; default config v0.0.8;
   wallet.db/identities 0600, config.json 0644.
2. **User state** — mints→testnut, price 3 sats/step, margin 0.15, step 30 MiB;
   payment from host-as-client (fetch :2050 first, then raw-body POST to :2121)
   funds the wallet (kind:1022, 3 sats).
3. **Upgrade → v0.6.0-alpha2** (local build @373770a, PKG_VERSION=v0.6.0-alpha2)
   — config.json/wallet.db/identities.json **byte-identical** (sha256), no
   migration fires (both versions ship `config_version: v0.0.8`), payment works,
   balance preserved. New fields (`auth_delay_seconds`, `redirect_url`,
   `vendor_ie_discovery`) load as zero-values and are NOT written back to the
   file (all zero-value-safe: valve gated by `RedirectURL != ""`).
4. **Rollback → v0.5.0** (`--force-downgrade`) — clean; v0.6-only files removed;
   wallet/config intact.
5. **Multi-mint** — CDK 0.18 V2-keyset mint added as second accepted mint;
   advertisement carries both `price_per_step` entries; V3 tokens from both
   mints pay; balance aggregates (6 testnut + 3 local = 9 sats).
   **V4 (`cashuB`) + V2-keyset tokens are rejected** (`payment-error-invalid-token`)
   — matches the known-open upstream state (V4+V1 works; V4+V2 E2E was pending).
6. **Fresh v0.6.0 install** — clean; first-boot migration of the packaged
   v0.0.2 default → v0.0.8 runs with backup in `/etc/tollgate/config_backups/`.

## Findings (filed)

- **tmbg preinst fragility (upgrade blocker class)**: `preinst` calls bare
  `jq` and `exit 1` on failure while only updating the cosmetic `install_time`.
  If jq is missing at preinst time the upgrade ABORTS, and opkg's orphan
  cleanup then REMOVES `nodogsplash` + `jq` (autoinstalled deps) — a failed
  upgrade breaks the captive portal. Mechanism + repro: Amperstrand
  tollgate-module-basic-go issue (see campaign #113).
  Recommended fix: absolute `/usr/bin/jq` (or BusyBox-only JSON write) and
  never exit non-zero for install_time cosmetics.
- **local-build-ipk.sh emits a tar.gz-format .ipk** (valid — opkg accepts both
  formats) while CI emits ar-format. Matters for byte-equivalence gating
  (#371/#405 lane), not for function.
- **CDK 0.18.0-final startup contract**: no `--config` flag; `config init
  --new-mint` writes config to the DB, then start bare with
  `CDK_MINTD_WORK_DIR`. (AGENTS.md 0.18-rc recipe is slightly stale.)

## Fund-safety answers (campaign questions)

- `config.json`, `wallet.db`, `identities.json` are **not shipped by the
  package** → opkg never touches them on upgrade/rollback; verified
  sha256-identical across upgrade→rollback→upgrade with a funded wallet.
- `preinst` only rewrites `install_time` inside `install.json`; `99-tollgate-setup`
  is flag-guarded (`/etc/tollgate-setup-done`) and does not re-run on upgrade.
- `/lib/upgrade/keep.d/tollgate` ships in the package for **sysupgrade**
  (firmware-image upgrades, the tollgate-os path) — distinct from the opkg
  package-upgrade path validated here.
- Multi-mint wallets: balance preserved across mints; per-mint keysets
  (V1+V2) both re-registered cleanly after restart.
