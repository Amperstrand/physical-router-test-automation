# Remaining Work Plan

## Item 1: Refactor `cli_command()` to use `tollgate --json` (eliminates socat)

**Files:** `lib/router.py`, `lib/deploy.py`

- [ ] Refactor `cli_command()` in `lib/router.py:597-610` to use `tollgate --json <command> [args...]` via SSH instead of socat
- [ ] Remove `has_cli_socket` guard — `tollgate --json` is the standard CLI interface
- [ ] Remove `socat` from `TEST_DEPS` in `lib/deploy.py:18`
- [ ] Run full pytest suite on both routers to verify ~13 previously-failing tests now pass
- [ ] Commit and push

## Item 2: Install `curl` on both routers (test setup step)

- [ ] `sshpass -p 'c03rad0r123' ssh root@10.47.41.1 "opkg update && opkg install curl"`
- [ ] `sshpass -p 'c03rad0r123' ssh root@192.168.244.1 "opkg update && opkg install curl"`

## Item 3: Fix e2e scripts

### test-configwizzard-e2e.sh
- [ ] Line 331: Change admin SPA check to also check `/www/tollgate/admin.html`
- [ ] Lines 336, 340, 345: Same fix for index.html, fail message, and JS bundle paths

### test-config-save-e2e.sh
- [ ] Lines 198-199: Replace `sleep 18` with polling loop (up to 60s, check every 2s)

## Item 4: Add skip guards to pytest for missing infrastructure

- [ ] `tests/api/vpn/` (10 tests): Add `@pytest.mark.skipif` for `TOLLGATE_VPN_TESTS_ENABLED`
- [ ] `tests/api/test_log_beacon_cgi.py` (2 tests): Skip guard checking if CGI exists
- [ ] `tests/api/test_pending_token_cgi.py` (2 tests): Same CGI skip guard
- [ ] `tests/api/test_mint_502_handling.py` (1 test): Skip guard for local proxy requirement
- [ ] `tests/api/test_portal_degraded_ui.py` (2 tests): Update selectors for current SPA build

## Item 5: Fix remaining pytest assertion issues

- [ ] `tests/api/test_session_endpoint.py`: Add null check in `parse_json_or_fail`
- [ ] `tests/api/test_setup_script.py`: Update SSL setup script path
- [ ] `tests/api/test_tls_transport.py`: Update mint info endpoint assertion

## Item 6: Re-run full test suite on both routers

- [ ] Alpha: `TOLLGATE_SSH_HOST=10.47.41.1 pytest tests/api/ --no-deploy -m "api and not phone" -v`
- [ ] Beta: `TOLLGATE_SSH_HOST=192.168.244.1 pytest tests/api/ --no-deploy -m "api and not phone" -v`
- [ ] Run e2e scripts on both routers
- [ ] Document final pass/fail counts

## Item 7: Address PR #124 review feedback

**Repo:** `tollgate-module-basic-go`, branch `develop`

### Must fix
- [ ] Remove committed binary `src/cmd/tollgate-cli/tollgate-cli`, add to `.gitignore`
- [ ] Fix `isLocalOrigin()` — proper CIDR matching for `172.16.0.0/12`
- [ ] Fix `GetConfig()` etc. — return deep copy instead of mutable pointer

### Should fix
- [ ] `validateAgainstSchema` — error on unknown keys
- [ ] `SetDotPath` — move disk I/O outside mutex
- [ ] `handleConfigSave` — consider stdin/file for JSON
- [ ] `EnsureDefaultConfig` — log before/after on reset

### After fixes
- [ ] Resolve merge conflicts with `develop`
- [ ] Push, re-request review, re-test on routers

## Item 8: Rebase PR decomposition chain (#138-142)

**Repo:** `tollgate-module-basic-go`

- [ ] PR #138: Merge first (already mergeable)
- [ ] PR #139: Rebase onto main, resolve conflicts
- [ ] PR #140: Rebase onto #139, resolve conflicts
- [ ] PR #141: Rebase onto #140, resolve conflicts
- [ ] PR #142: Rebase onto #141, resolve conflicts
- [ ] PR #143: Rebase onto main (independent)
- [ ] Run `go test -tags testenv ./...` after each rebase
- [ ] Force-push and update PR comments

## Item 9: CI Runner Setup

- [ ] Resolve OpenTollGate org suspension (GitHub Support)
- [ ] Or: Set up self-hosted `act-runner` on workstation
- [ ] Or: Run CI from personal fork

## Item 10: Publish nsite Dashboard (deferred)

- [ ] Run `scripts/publish-nsite.py` from machine with Blossom access
- [ ] Post nsite URL to all PR comments and configurationwizzard issue #3

Dashboard HTML + screenshots at `/tmp/e2e-dashboard/combined/`.
