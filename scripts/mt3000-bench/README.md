# mt3000-bench — the single-owner bench lock and the artifact-naming deploy helper

The bench **GL-MT3000 @192.168.1.1** (OpenWrt 25.12.5, `aarch64_cortex-a53`, apk-tools 3)
is reached over this host's `enp0s31f6` (`192.168.1.200/24`). It **drops ICMP** — judge
liveness by TCP ports, never `ping`.

## Why this exists (measured 2026-09-24)

The bench kept being rewritten under a running test. A Hermes cron job
(`tg-e2e-watcher`, `*/10 * * * *`, `no_agent`) piped the feed's `alpha5` apk to the router
and ran `apk add --allow-untrusted` whenever the box did not carry its pinned build:

| time (router) | what happened |
|---|---|
| 19:50:31 | `apk add --allow-untrusted /tmp/tg-alpha5.apk` — mid-smoke-test |
| 20:05:36 | again, after the test finished — wiped the tested build |
| 20:10:58 | again, from an **orphan** after the parent worker was killed |
| 21:50 / 22:04 | again — invalidating a published-artifact check and a curl\|bash validation |

Each `alpha5` install re-ran its postinst, which **pruned
`/etc/nftables.d/31-admin-board-not-guest-reachable.nft`** (`:8090` guest-reachable again),
**re-randomised the guest SSID**, and replaced the build under test. Root cause class:
*more than one owner, none of them coordinated.* These three scripts make the bench
single-owner and make "what got installed" a verified fact instead of an assumption.

## Files

| file | what |
|---|---|
| `bench-lock.sh` | the flock lock: `status` / `take` / `exec` / `require` / `release` |
| `bench-with-lock.sh` | the sanctioned wrapper: acquire the lock, run your command, release |
| `bench-deploy-apk.sh` | deploy ONE named apk; rotate stale staged apks; verify the installed binary |
| `router-snapshot.sh` | read-only router state dump (`snapshot`), the ssh transport for any local script (`run`), and `render` — the payload printed locally, no ssh, no lock |
| `second-purchase-e2e.sh` | does a SECOND purchase re-open the gate? fresh-MAC buy → exhaust → post-exhaustion → `ndsctl deauth` → buy again. **Dry run by default** |
| `bench-token.py` | `mint` (unsigned NUT-04 quote, no `20008`) and `verify` (NUT-07: every proof must be UNSPENT) |

## The lock

* Lock file: `~/.hermes/state/bench-mt3000.lock` (override `BENCH_LOCK_PATH`).
* Mutual exclusion is a **flock** on that file. The kernel drops it when the holder dies, so
  an orphan cannot hold the bench and a killed holder cannot wedge it.
* **Holder line** (first line of the lock file):

  ```
  <profile> pid=<pid> purpose=<purpose> since=<iso8601> task=<id|-> host=<hostname>
  ```

  The first four fields are the convention the manager's own window used
  (`manager pid=2584919 purpose=curl|bash-pre16-validation since=...`); `task=` and `host=`
  are appended so a refused caller can see who owns the bench. `purpose` is
  whitespace-free (spaces folded to `_`).
* **Refusal is the default.** Every refusal prints the holder's identity. Use `--wait N` to
  wait instead of failing.
* A holder line with **no flock behind it** is *stale metadata*: free by flock, but refused
  by default (`exit 5`) until you pass `--reclaim-stale`, which prints a warning.

```sh
bench-lock.sh status                         # who owns the bench (read-only)
bench-lock.sh take --purpose "smoke test" --hold 600   # hold it (blocks; auto-release)
bench-lock.sh release                        # end the window named by the holder line
bench-with-lock.sh --purpose "smoke test" -- ./my-router-script.sh
```

Exit codes: `0` ok · `2` usage · `3` refused: held · `4` not holding (`require`) ·
`5` stale metadata needs `--reclaim-stale` · `6` lock path unusable.

### Rule: every router-touching script must hold the lock

From inside a script, as its first action:

```sh
"$(dirname "$0")/bench-lock.sh" require || exit $?
```

`require` **fails (`exit 4`)** unless the script is already inside a lock window, and the
error names the current holder. Watchdogs may **report**, never install: they must take the
lock with `--wait 0` and exit silently when it is held (see the `tg-e2e-watch.sh` rewrite in
the manager script repo).

## The deploy helper

```sh
bench-with-lock.sh --purpose "deploy pre16" -- \
  scripts/mt3000-bench/bench-deploy-apk.sh \
    --apk /path/tollgate-wrt_0.6.0_alpha4_pre16_aarch64_cortex-a53.apk \
    --sha256 104e9ce00b8f01c09840c9aa6d8976c6a6712a4cd05376ddf5f5a237eb2b4e72 \
    --task t_xxxx
```

It **refuses unless it holds the lock**, then:

1. **Names its artifact** — `--apk` + `--sha256` are mandatory; it prints path, size,
   sha256, and the extracted `usr/bin/tollgate-wrt` payload sha256 (the identity it will
   verify). A file that does not hash to the named sha256 is refused (`exit 6`).
2. **Rotates** every `/tmp/*.apk` it did not stage for this window to `.rotated-<ts>`
   (renamed, never deleted — another window may own those bytes). `--refuse-foreign-staged`
   refuses instead of rotating.
3. **Refuses substituted artifacts** — an apk already staged under the name this deploy
   intends to use, but with different bytes, stops the deploy (`exit 7`). A
   `/etc/tollgate/install.json` `package_path` pointing at an apk (the *revert bomb* that
   `/usr/bin/check_package_path` re-installs) also stops it, until `--clear-package-path`.
4. **Installs detached** (`setsid`, no `nohup` on this BusyBox) and polls a log for an
   explicit `DONE` — the ssh exit code is never treated as success.
5. **Verifies the installed binary** — `sha256sum $TG_BIN/tollgate-wrt` + size on the router
   vs the payload extracted from the named artifact. Mismatch ⇒ loud failure (`exit 8`) with
   both hashes and the router's `apk.log` tail. Then it re-checks `--verify-only`.

```sh
# the honest handover check (read-only, installs nothing):
bench-with-lock.sh --purpose "pre-handover" -- \
  scripts/mt3000-bench/bench-deploy-apk.sh --verify-only --apk <your.apk> --sha256 <hex>
```

Exit codes: `7` substituted/staged/`package_path` refusal (nothing installed) ·
`8` installed identity MISMATCH · `9` transfer failed · `10` install did not complete.

**Handover rule:** verify immediately before telling anyone the bench is ready, and again if
time passed — a verified install was reverted four minutes later on 2026-09-24.

### Credentials, never on argv

`BENCH_ROUTER_PW_FILE` (default `~/.tg-e2e/pw`) or `BENCH_ROUTER_PASSWORD`. There is no
credential in this repo, and the lab password is documented in the `tollgate-development`
skill, not here.

## The second-purchase lane

The reported product bug (a client buys, exhausts its allotment, and the gate never re-opens
for a second purchase) has one reproducible lane here. Procedure, the measured pre17 result
and the traps: **`docs/second-purchase-bench.md`**.

```sh
make second-purchase-e2e                     # DRY RUN: prints the plan, touches nothing
make second-purchase-e2e SECOND_PURCHASE_ARGS="--purchase" TOKEN_1=... TOKEN_2=...   # spends
make second-purchase-detached SECOND_PURCHASE_ARGS="--purchase" TOKEN_1=... TOKEN_2=...
make bench-snapshot                          # read-only state dump, inside the window
make bench-snapshot-payload                  # what the snapshot will run on the router
make bench-token-mint BENCH_TOKEN_ARGS=--yes # 64-sat test-mint token
make bench-token-verify TOKEN_FILE=...       # NUT-07: is it still UNSPENT?
```

The e2e takes the bench lock itself (re-exec under `bench-lock.sh exec`) and every purchase is
POSTed **through the client's own interface** — the module authorises the MAC of the requesting
socket, so a purchase sent from the bench host authenticates the host, not the client.

## Tests (no router required)

```sh
tests/mt3000-bench/run-tests.sh
```

23 tests / 0 skips on a host with `busybox`, `apk.static` (`~/.cache/apk-v3/`) and two real
fixture apks (`BENCH_TEST_APK_A` / `BENCH_TEST_APK_B` override the defaults). It builds a
throw-away "router root", puts PATH doubles for `ssh`/`scp`/`apk` in front (so the
production transport code is exercised), and runs the production remote scripts — optionally
under **BusyBox ash**. The negative controls are the point: a second owner is refused with
the holder's identity, a substituted staged apk is refused before installing, and
*installing build A while naming build B* fails loudly with both hashes. The suite also covers
the second-purchase lane offline: the e2e is dry-run by default, a paid run without tokens is
refused, a paid run is refused (naming the holder) while another window owns the bench, the
snapshot payload is accepted by `sh -n` **and** BusyBox `ash -n`, and the token tool mints
nothing without `--yes`.

## Pitfalls this path has already paid for

* **`sshpass` kills a detached install.** It allocates a pty; when it exits, the pty closes
  and the session's process group takes SIGHUP. Measured in the harness: `setsid CMD &` was
  killed before it ever ran, leaving no log at all. Fix: `trap '' HUP` in the forking shell
  (inherited across `exec`), then `setsid`.
* **Never execute the installed binary from a shell.** `$BIN version` on a wrong-arch or
  broken binary makes POSIX `sh` fall back to reading 12 MB of ELF as a *script* — the
  harness hung there. Run it as `timeout 5 "$BIN" version`, or skip it.
* **No `nohup`, no `stat`, no `sftp-server`, no `curl` on this box**; `scp` needs `-O`; size
  with `wc -c < file`, never `stat -c %s`; `grep -c` exits 1 on zero matches, so pipe it to
  `head -1` before arithmetic.
* **`apk add` of an equal version is a no-op**, and `apk info -v` prints the *package*
  version, not the build identity — only the payload sha256 is proof.
* **The lock fd must not leak into the command.** `flock` is inherited across `fork` *and*
  `exec`, so a detached descendant (an install launched with `setsid`, a backgrounded helper)
  would keep the bench locked after the window closed — the next window is refused by a
  holder `release` cannot even name, because the holder-line pid is gone. `bench-lock exec`
  runs the command in a subshell with `exec 9>&-`; if you open the lock fd yourself, close it
  before spawning anything. Regression tests: `run-tests.sh` test 17 (RED without the fix).
* **Resolve your own path through symlinks.** `install.sh` links the commands into
  `~/.local/bin`, so `$BASH_SOURCE` is the *symlink*: `$(dirname "${BASH_SOURCE[0]}")` then
  points at `~/.local/bin` and the sibling scripts are "not found" (hit live). Use
  `readlink -f`.
