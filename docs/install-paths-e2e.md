# Dual-install-path end-to-end coverage (fresh flash → install → happy path)

This is the evidence gate for the review-club card **"REVIEWER GUIDE: package-only
install — policy identical, choices differ"** (`t_a05094ad`, board
`tollgate-module-basic-go`).  It answers one question mechanically:

> after a **fresh flash**, does a **bare package install** produce the same
> *policy* as the **installer** path, and does the **happy path** actually work
> in both cases?

* scenarios: `tests/scenarios/test_install_paths.py`
* flash-free runner: `scripts/install-path-e2e.py --dry-run`
* fresh-flash prerequisite: `scripts/fresh-flash.py`
* unit coverage: `tests/unit/test_install_paths.py`, `test_fresh_flash.py`,
  `test_bench_lock.py`

**Status of this branch (read this first).**  The scenario code, the pre-flight,
the lock integration and the flash gates are complete and exercised by the
flash-free dry run (evidence in
[install-paths-dry-run-report.md](install-paths-dry-run-report.md)).  The two
flash cycles themselves are **deferred**: they need a bench window and a drained
wallet, and they are the only steps this branch does not execute.  Nothing in
this document claims a run that did not happen.

---

## 1. What the two scenarios do

Both start from a **freshly flashed** OpenWrt 25.12.x image and end with the same
five gates.  Only the install step differs.

| # | gate | assertion |
|---|---|---|
| 1 | **artifact identity** | `sha256sum /usr/bin/tollgate-wrt` on the router **equals** the sha256 of that binary *inside the artifact that was installed* (the **same-format** artifact — see §4.1) |
| 2 | **version** | the installed package version string contains the artifact's version stem |
| 3 | **surfaces** | `:2051` → 200 (splash/SPA), `:2050` → 200 (stub), `:2121` → 200 (API), `:8080` → 307 (LuCI → portal), and SSH stays alive (port 22 still in the NDS allow list) |
| 4 | **POLICY** | `uci show nodogsplash | grep users_to_router` **set-equals** `{22,23,53,67,80,443,2050,2051,2121,8080}`, `8090`/`8443` are **absent**, `/etc/nftables.d/31-admin-board-not-guest-reachable.nft` is present and drops from `br-lan`, and `:8090` is **not reachable** from a `br-lan` client |
| 5 | **happy path** | the *existing* Playwright suite passes (see §3) |

**Scenario A — direct package install**

1. `test_a1` select the published artifact by the router's **detected package
   manager** (apk on 25.x ⇒ `.apk`; opkg on ≤ 24.10 ⇒ `.ipk`).  The opkg case is
   an **explicit skip with the reason**, never a silent pass.
2. `test_a2` verify the artifact's sha256 against the release `SHA256SUMS`.
3. `test_a2b` **flash-free**: the payload must actually ship the guard file *and*
   a setup script that removes `8090`/`8443` from `users_to_router`.  If the
   release does not ship them the policy gate is unreachable, and the failure is
   reported *here*, with the reason (see §7).
4. `test_a2c` **flash-free**: the opkg branch is a recorded skip carrying the
   real failure mode, and `ArtifactSelection.require()` raises rather than
   yielding an empty pass.
5. `test_a3` push the **`.ipk`** and prove `apk` rejects it on an apk-only image
   (`ERROR: v2 package format error`, exit `99`).  This is what makes the
   selection in step 1 meaningful.
6. `test_a4` install the `.apk`.  **Preferred**: the sanctioned
   `bench-deploy-apk` verifier (names the artifact + payload sha256, rotates
   foreign staged apks, refuses a substituted one, installs detached, then
   verifies the installed binary against the payload of the artifact it was told
   to install — exit `8` on mismatch).  **Fallback** (helper not installed):
   `apk add --no-check-certificate --allow-untrusted` plus this repo's own
   identity gate in `test_a5`.  The backend used is recorded in the evidence.
7. `test_a5`–`test_a8` gates 1–5.

**Scenario B — the canonical installer path**

1. `test_b1` fetch `install-and-test.sh` **from its repo URL**, `bash -n` it,
   assert it advertises `--tag`, then build the canonical command
   (Felix's preferred test form):
   `bash <(curl -fsSL <raw url>) --tag <tag> <router> <password> <ln-address>)`
2. `test_b2` run it **inside our bench window** (the lock env is exported to the
   child); require exit 0 **and** `=== DEPLOY COMPLETE ===`; never call a partial
   run a pass.
3. `test_b3`–`test_b5` gates 1–5.
4. `test_b6` **policy parity**: the two snapshot dicts must agree on the policy
   components; the only tolerated difference is the operator *choices*
   (hostname, guest SSID, LN address) — the card's actual claim.

Every run writes `install-paths-evidence.json` (raw outputs, hashes, snapshots,
the policy pre-flight verdict) next to the pytest temp dir, or to
`$TOLLGATE_INSTALL_PATHS_EVIDENCE`.

---

## 2. The flash-free POLICY pre-flight (fail fast, name the release)

The guard this card asserts (`31-admin-board-not-guest-reachable.nft` present,
`8090`/`8443` gone from `users_to_router`) only ships **from pre17 on**.  A run
against an older release therefore fails for a reason that has nothing to do with
the install path.  That is a *named* outcome now:

```bash
make install-path-preflight                      # exit 0 = supported, 3 = unsupported
# or
python3 -c "from lib import install_paths as ip; print(ip.policy_preflight().message())"
```

* the target release is `TOLLGATE_FEED_TAG` (default `v0.6.0-alpha4-pre17`);
* the floor is `TOLLGATE_POLICY_MIN_RELEASE` (default `v0.6.0-alpha4-pre17`);
* the expected guard path is `TOLLGATE_POLICY_GUARD_PATH` (default
  `/etc/nftables.d/31-admin-board-not-guest-reachable.nft`) — configurable
  because the module names the file by release;
* `--dry-run`, the pytest module (`policy_release_gate`, module-scoped autouse)
  and `--flash-and-run` all evaluate it **before anything touches the router**;
* an unsupported release **exits 3** and prints, by name:

  > release `v0.6.0-alpha4-pre16` is UNSUPPORTED for the POLICY/guard assertion
  > (needs >= `v0.6.0-alpha4-pre17`): …

`--continue-unsupported` (`TOLLGATE_CONTINUE_UNSUPPORTED=1`) is the only way
past, and it records the gate as **UNSUPPORTED**, never as a pass.

### Exit codes of `scripts/install-path-e2e.py --dry-run`

| code | meaning |
|---|---|
| 0 | all flash-free checks passed |
| 1 | a check failed |
| 2 | the bench lock is held (or stale) by another window — refusal names the holder |
| 3 | the release is **unsupported** for the POLICY/guard assertion (fail fast, named) |
| 4 | the release has **no published `SHA256SUMS`** yet — a named "not published" outcome, not a mystery HTTPError |

---

## 3. The happy path is reused, not reinvented

The UI flow driven is the **existing** suite:

* spec — `tests/protocol/captive-portal.spec.mjs`
* describe — `captive portal — happy path` (advertisement + pricing, cashu token
  input, lightning amount input, mint pricing buttons)
* the same spec that `make test-captive-portal-happy` runs
  (`config/make-pytest-map.yaml` → `pytest: tests/protocol/captive-portal.spec.mjs`)

Command actually executed (cwd `tests/`, runner resolved to
`node_modules/.bin/playwright`, **not** a global CLI):

```
<repo>/node_modules/.bin/playwright test --config=playwright.config.mjs \
  --project=desktop-portal --grep "captive portal — happy path" \
  protocol/captive-portal.spec.mjs
```

The Playwright JSON report (`tests/report/report.json`) is parsed by
`lib.install_paths.parse_happy_path_report`, which fails on **0 tests collected**
(an empty run is not a pass), on any failure, on any expected test missing, and
— by default — on any **skip** (a skipped happy path is not a completed happy
path; on a degraded router the suite skips itself, and that must be visible).
`TOLLGATE_HAPPY_PATH_ALLOW_SKIP=1` records skips as a warning instead.

The dry run proves the suite is **collectable** without a router
(`playwright test --list`, 6 tests for the grep) — that is how the "reuse, do not
reinvent" claim is checkable before any bench time is spent.

---

## 4. Traps this harness encodes (found the hard way)

1. **The `.apk` and `.ipk` of one release do not carry the same binary.**
   Verified on `v0.6.0-alpha4-pre16`: `.apk` payload `usr/bin/tollgate-wrt` is
   12 242 208 B / `dce8b1f1…`, the `.ipk` payload is 12 295 456 B /
   `5ddda42b…` (identical for every non-Go file; the two are built by different
   CI jobs — the `.apk` through the OpenWrt SDK, the `.ipk` by
   `packaging/build-ipk.sh`).  **Consequence:** the identity gate derives its
   expected hash from the artifact of the *same format it installs*.  This is
   enforced in code, not just in prose: `lib.install_paths.identity_gate(
   expected_format=…)` **raises `CrossFormatIdentityComparison`** when handed the
   sibling format's hash instead of comparing it (a cross-format compare is a
   false failure — or hides a real swap).  Pinned by
   `tests/unit/test_install_paths.py::test_identity_gate_refuses_a_hash_from_the_sibling_format`.
2. **apk v3 artifacts are `ADBd`-prefixed, not gzipped tars.**  `tar` cannot read
   them; unpacking needs apk-tools (`apk extract --allow-untrusted
   --destination DIR file.apk`).  Install Alpine's `apk-tools-static` and point
   `TOLLGATE_APK_TOOL` at `apk.static` (the harness never guesses).
3. **`npx playwright test` only works with the repo's `@playwright/test`.**  A
   global `playwright` install has no `test` command (`unknown command 'test'`).
   Run `npm install`; the harness resolves `node_modules/.bin/playwright` and
   fails with that instruction rather than a confusing CLI error.
4. **The setup marker trap.**  `99-tollgate-setup` only *re-converges* the NDS
   allow list on a full setup; on a same-version reinstall it merely verifies
   the wireless APs.  So a reinstall can inherit a stale `users_to_router` list
   (including `8090`) and look "unchanged".  The whole point of measuring after
   a **fresh flash** (no `/etc/tollgate-setup-done`) is to exercise the real
   first-install path; the harness asserts the marker is absent on the fresh
   image and refuses a "fresh" image that already carries it.
5. **The bench router drops ICMP.**  Never `ping` it; probe TCP
   (`22/2050/2051/2121/8080/8090`).
6. **`scp` needs `-O` and `apk`/`wget` need `--no-check-certificate`** on this
   image (no `sftp-server`, no CA store).

---

## 5. The bench is single-owner: the lock is not optional

The MT3000 (`192.168.1.1`) is shared by several cards.  An earlier cron
re-installed an old build every 10 minutes and invalidated a day of runs; a
stray deploy loop reverted the `:8090` guard three times in one evening
(`t_aa94ad3b`).

The sanctioned tooling is `scripts/mt3000-bench/` (branch
`pr/bench-mt3000-single-owner`), installed on `PATH` as `bench-lock`,
`bench-with-lock` and `bench-deploy-apk`.  `lib/bench_lock.py` is the Python
client of that convention and is **interoperable with it by construction**:

* the same `flock` on the same file (`~/.hermes/state/bench-mt3000.lock`,
  override `BENCH_LOCK_PATH` — honoured by both the shell helper and this module);
* the same **canonical holder line**:
  `<profile> pid=<pid> purpose=<purpose> since=<iso8601> task=<id|-> host=<hostname>`
  (`purpose` whitespace-folded; releasing **clears** the line);
* a holder line with no flock behind it is **stale metadata**: the bench is free
  by flock but we refuse and require an explicit `--reclaim-stale` /
  `TOLLGATE_BENCH_RECLAIM_STALE=1`;
* `BenchLock.child_env()` exports `BENCH_LOCK_HELD` / `BENCH_LOCK_HOLDER_PID`, so a
  child may run `bench-lock require` / `bench-deploy-apk` unchanged — verified by
  `tests/unit/test_bench_lock.py::test_canonical_helper_require_accepts_our_child_env`;
* the kernel releases the flock when the holder dies, so a crashed worker cannot
  wedge the bench.

| I want to… | command |
|---|---|
| see who owns the bench | `bench-lock status` / `python3 -m lib.bench_lock status` |
| run anything router-touching | `bench-with-lock --purpose "<what>" --task t_a05094ad -- <cmd>` |
| install one named artifact | `bench-with-lock --purpose "<what>" -- bench-deploy-apk --apk <f.apk> --sha256 <64hex>` |
| run the dry run | `make install-path-dry-run` (wraps itself in `bench-with-lock` when installed) |

Three layers refuse, all naming the holder:

1. `bench-with-lock` will not start the command (`exit 3`);
2. the dry run **skips its read-only probes** when another window owns the bench
   (probing another window's bench reports *their* state as if it were ours);
3. `BenchLock.acquire()` raises `BenchBusy` with the holder's identity line.

All three were executed as part of producing the committed evidence.

---

## 6. Draining before a flash is the operator's step (real money)

`sysupgrade -n` wipes `/etc/tollgate` — `config.json`, `identities.json`,
`install.json` **and `/etc/tollgate/ecash`**.  On an *upgrade* or *reinstall* the
package keeps `/etc/tollgate` (conffiles) and the ecash survives; only a firmware
flash takes it.  Therefore:

* flashing is **doubly gated**: `TOLLGATE_ENABLE_SYSUPGRADE_FLASHING=true` (the
  destructive switch) **and** an empty wallet;
* `scripts/fresh-flash.py` probes `tollgate --json wallet balance` **and**
  non-empty files under `/etc/tollgate/ecash`;
* if either says "money", it **refuses** to flash and prints the drain command:
  `tollgate wallet drain cashu --yes` (exit 2 = cancelled, nothing moved);
* `--allow-nonempty-wallet` is the only way past, and it is explicit;
* the same gate runs as `TestFreshFlashPrerequisite::test_02`, and
  `scripts/install-path-e2e.py --flash-and-run` refuses *before* pytest starts;
* `lib.fresh_flash.flash_preconditions()` returns **every** blocker at once (the
  switch and the money), so an operator is not told one at a time.

Never suggest `rm -rf /etc/tollgate` without draining first.

---

## 7. Running it

### 7.1 Flash-free (safe: no router state change; probes only under a window)

```bash
# the whole flash-free surface; writes reports/install-paths/ + the markdown report
TOLLGATE_APK_TOOL=$HOME/.cache/apk-v3/apk.static \
  make install-path-dry-run
# bench untouched entirely:
TOLLGATE_APK_TOOL=$HOME/.cache/apk-v3/apk.static \
  python3 scripts/install-path-e2e.py --dry-run --skip-probe
```

Checks: **policy pre-flight** → release manifest → artifact selection → artifact
sha256 → expected binary sha256 **from the same-format artifact** → payload
policy readiness → apk/ipk format split → installer script fetched/`--tag`/
command shape → flash image verified → **happy-path suite collectable** → bench
lock state → read-only bench ports/identity/policy snapshot.

Exit code is non-zero when a check fails, so it can be a CI/preflight gate.

### 7.2 The full locked bench phase (two flash cycles) — **the deferred part**

```bash
# one-time: the reused happy-path suite needs @playwright/test
npm install

# preconditions (read-only) — refuses on a non-empty wallet
python3 scripts/fresh-flash.py --check

# drain is the OPERATOR's step (real money):
#   ssh root@192.168.1.1 'tollgate wallet drain cashu --yes'   # prints the tokens

# the single documented entry point: pre-flight -> switch check -> bench lock ->
# wallet gate -> flash -> both install paths -> happy path
TOLLGATE_ENABLE_SYSUPGRADE_FLASHING=true \
TOLLGATE_LN_ADDRESS=you@coinos.io \
  python3 scripts/install-path-e2e.py --flash-and-run --host 192.168.1.1
```

`--flash-and-run` performs, in order: the policy pre-flight (exit 3 if
unsupported), the `--ln-address` requirement, the
`TOLLGATE_ENABLE_SYSUPGRADE_FLASHING=true` check, `BenchLock.acquire()` (refusing
with the holder's identity), the wallet gate (`flash_preconditions`), then
`pytest tests/scenarios/test_install_paths.py --no-deploy -v --timeout=3600`
inside that window.

**Flash cycle B needs a second flash.**  The scenarios assert the *first-install*
policy, so scenario B must run on a freshly flashed image too — flash again
between A and B (the run prints this reminder).  The card's parity comparison
(`test_b6`) needs both snapshots from the **same release**.

`--no-deploy` is **required**.  The session-scoped `deploy_session` fixture
otherwise rewrites the mint list (`replace_mints`), enables the debug portal and
restarts the backend — that is not a clean install, and it would invalidate the
"policy as installed" claim.  The module fails loudly if the option is missing.

### 7.3 Environment knobs

| var | meaning |
|---|---|
| `TOLLGATE_FEED_TAG` / `TOLLGATE_FEED_VERSION` | release under test (default `v0.6.0-alpha4-pre17` / `0.6.0_alpha4_pre17`) |
| `TOLLGATE_POLICY_TARGET_RELEASE` | release the policy assertion is asserted against (default: `TOLLGATE_FEED_TAG`) |
| `TOLLGATE_POLICY_MIN_RELEASE` | oldest release that can satisfy the guard assertion (default `v0.6.0-alpha4-pre17`) |
| `TOLLGATE_POLICY_GUARD_PATH` | the expected nft guard path (default `/etc/nftables.d/31-admin-board-not-guest-reachable.nft`) |
| `TOLLGATE_CONTINUE_UNSUPPORTED` | `1` = keep going on an unsupported release, reporting the gate as UNSUPPORTED (never PASS) |
| `TOLLGATE_ARTIFACT_ARCH` | arch to fetch (default `aarch64_cortex-a53`) |
| `TOLLGATE_ARTIFACT_DIR` | artifact cache (default `~/.cache/prta-install-paths/<tag>`) |
| `TOLLGATE_APK_TOOL` | `apk.static` able to `extract` (see §4.2) |
| `TOLLGATE_LN_ADDRESS` | required for scenario B (the installer's operator choice) |
| `TOLLGATE_INSTALLER_URL` | override the installer script URL |
| `TOLLGATE_HAPPY_PATH_ALLOW_SKIP` | `1` downgrades skipped happy-path tests to a warning (default: skip = fail) |
| `TOLLGATE_CAPTIVE_PORTAL_PORT` | portal port for the UI suite (default `2051`) |
| `TOLLGATE_BENCH_LOCK` / `BENCH_LOCK_PATH` | bench lock path override |
| `TOLLGATE_BENCH_RECLAIM_STALE` | `1` = take over a stale holder line whose owner died (explicit recovery) |
| `BENCH_LOCK_CLI` / `BENCH_DEPLOY_APK_CLI` | explicit paths to the sanctioned helpers |
| `TOLLGATE_TEST_PYTHON` | interpreter used for the pytest phase of `--flash-and-run` |
| `TOLLGATE_ALLOW_NONEMPTY_WALLET` | `1` accepts losing the ecash on a non-empty wallet |
| `TOLLGATE_ENABLE_SYSUPGRADE_FLASHING` | `true` allows the flash step (mandatory for `--flash-and-run`) |
| `TOLLGATE_FRESH_FLASH_IMAGE` | image path (default: the pre-downloaded copy under `~/worktrees/mt3000-flash/`) |

---

## 8. Known state of the release under test

`v0.6.0-alpha4-pre17` is the **default** release under test and is **not
published yet** (the feed's newest release is `v0.6.0-alpha4-pre16`), so the
default `--dry-run` **fails fast with exit 4** naming the release: that is the
honest outcome, not a broken harness.  As soon as the pre17 assets land the same
command runs end to end.

Until then the *runnable* evidence is the published `pre16` with
`--continue-unsupported`, and it says exactly what the card needs:

* the payload **does not ship** `/etc/nftables.d/31-admin-board-not-guest-reachable.nft`
  (its `/etc/nftables.d/` holds only `20-nds-enforce.nft` and
  `30-backend-firewall.nft`), even though the module tree at the commit the
  binary reports (`0.6.0-alpha4-g2796d96`) does contain it — the fix has to
  actually ship in the package;
* the `.apk` and `.ipk` payloads differ (`dce8b1f1…` vs `5ddda42b…`), so the
  same-format rule is load-bearing;
* on the bench, which currently runs `tollgate-wrt-0.6.0_alpha4_pre16-r1`, the
  guard file is **absent** and `:8090` **answers from a br-lan client**.

Re-run when pre17 is published:

```bash
TOLLGATE_APK_TOOL=$HOME/.cache/apk-v3/apk.static \
  python3 scripts/install-path-e2e.py --dry-run          # exit 0 expected
```

The dry-run report, the raw JSON and the exact `pre16` evidence are in
[install-paths-dry-run-report.md](install-paths-dry-run-report.md) and
`reports/install-paths/`.
