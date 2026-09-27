# PR-Review Marathon 2026-09-26/27 — Operational Lessons

Unattended run: 30 open PRs of `tollgate-module-basic-go` reviewed one at a
time by sequential opencode sessions orchestrated over herdr from a single
oversight session; live tests run for the two PRs whose reviews recommended
them. This file keeps the durable operational knowledge; the per-PR outcomes
live on the PRs themselves.

## Orchestration (herdr + one opencode session per review)

- One linked git worktree (`/tmp/pr-review-wt`, branch `pr-review-base` reset
  to `origin/main` between reviews) + one pane (`wA:p1`) works for fully
  sequential agents. Each agent: `gh pr checkout N` → review → post one
  comment → return the worktree to `pr-review-base` → delete only the exact
  branch it created.
- **Shared git dir hazard:** all worktrees of one repo share branch storage.
  An agent's "cleanup" ran an overbroad `git branch -D` that deleted *every*
  local branch (165 restored from the deletion output; 51 were dangling
  before the incident). Rule: agents may never delete branches by glob or
  filtered list — only the exact name they created. Orchestrator verifies
  branch count + `git fsck` after every agent run.
- `herdr agent prompt` issued immediately after `agent start` can silently
  fail to submit (paste lands, Enter never registers): verify delivery via
  `agent get`/`agent read` before resending; the 5-second activity gate also
  reports `server_unavailable` when the prompt *did* land. Treat error codes
  as "check state", never "resend blindly".
- Clean shutdown: single `ctrl+c` on an idle opencode returns the pane to the
  shell; the pane is reusable for the next `agent start`. Before every start,
  verify the pane is an idle shell *with no live or stopped jobs* (a crashed
  run left an opencode SIGTSTP'd plus a sleep-chain in the pane).
- Verification of "agent finished": the **posted GitHub comment** (author +
  timestamp + body shape) is the source of truth — pane transcripts garble
  the REVIEW-RESULT block through TUI soft-wrap.
- `gh pr list` does not show draft status unless requested; one PR (#391) was
  a draft and correctly skipped. Two PRs merged *during* the marathon —
  refresh PR state immediately before each review.
- `~/bin/gh` wrapper blocks writes to non-owned repos; agents self-served the
  documented `BCR_GH_WRITE_OK=1` env-bypass, every attempt logged in
  `~/.config/bcr/gh-write-attempts.log`. Keep it that way: deliberate,
  user-authorized posts only.

## Repo-side systemic finding (surfaced by the reviews)

A cluster of open PR branches (#530, #557, #569, #570 — all test-tier
branches last touched 09-24/25) had their heads **overwritten with #549's
wallet-recover commit series**; their advertised content survives only as
dangling SHAs (e.g. #530's rootfs tier at `3456de9c`/`e3ce73c2`, #557's
runner at `57864b5b`). #549 is the canonical carrier (verified by ancestry).
Any future review of a PR from that window must verify head content matches
the PR title before reviewing. A maintainer-side audit of the clobbering
force-push is still open.

## Venue health (what blocked "complete tests" and what to do)

- **SHC zone dead** (orders would bill but never be reachable):
  `cloud-lab.py` already fail-fasts with a clear message — worked as
  designed. `--cloud gcp` is the revived alternative (~$0.10/run).
- **Autonomous submit is structurally blocked for un-mirrored PR heads:**
  the artifact resolver tries Blossom/Nostr first, then polls the GitHub
  "Build and Publish" workflow — whose newest run for this repo is
  2026-08-27 (lane moved to ngit). PR branches not carried by the ngit
  mirror produce **no** kind-1063 events and no GitHub runs, so submit
  burned its full 30-minute artifact timeout twice. **Fixed this run:**
  `ensure_artifact` now probes lane health after ~5 minutes of empty polls
  and fails fast with an actionable message (`lib/deploy.py`,
  `tests/unit/test_deploy_artifact_failfast.py`). Verified live: 27 s to
  the fast error vs 30 min.
- **Host port 2121 collisions on shared hosts:** the TMBG cloud-lab compose
  publishes `2121:2121` for `upstream`; any other holder (an SSH tunnel of a
  parallel session, another tollgate service) blocks every scenario that
  needs the router container. Workaround used: run the lane from an in-tree
  path copy with the publish remapped (assertions are container-internal).
  This is #557's motivation — run-scoped, port-parametrized lanes.
- **Lane copies must preserve relative mounts:** copying `tests/cloud-lab/`
  elsewhere breaks `../../scripts:/clientd` (daemon container loses
  `tollgate-clientd.py`). Copy into a sibling dir of the real `tests/`, or
  fix the relationship, when a port-remapped copy is needed.
- **Busy local virtual-lab:** the QEMU lab on this host was mid-test by a
  parallel session (running the *Rust* backend). Before deploying onto any
  shared venue, check `pgrep`/router logs for in-flight work; for read-only
  features (e.g. `wallet recover`: CLI socket → mint NUT-07, no NDS/gate),
  a host-side service (`-tags testenv`, `TOLLGATE_TEST_CONFIG_DIR`) plus an
  isolated containerized cdk-mintd reproduces the full live path without
  touching the shared router. Two off-router nits found that way: the
  invalid-identities backup path hardcodes `/etc/tollgate/config_backups`,
  and the service's HTTP port `:2121` is hardcoded in `main.go` (no config
  knob) — both matter only off-router.

## What was delivered

- 30 PRs triaged; 27 reviews/verdicts posted (incl. #577 post-merge), 3
  correctly skipped (1 draft, 2 merged mid-queue). Recurring theme: most
  non-blocking items were CHANGELOG placement/rebase hygiene.
- Live evidence posted: #549 `wallet recover` (full classification + exit
  contract against a real cdk-mintd), #425 clientd battery S2–S4 full PASS
  at head.
- PRTA improvements: artifact-wait fail-fast (+6 unit tests), this document.

## Fix batch + forensics addendum (2026-09-27 evening)

- **Root cause of the contamination, confirmed by remote-tracking reflogs**
  (`update by push` entries in the shared clone): a local session on
  2026-09-24 pushed the wallet-recover tip to four unrelated PR branches in
  two bursts (14:26 local `c0c8555a`→rootfs-validation-tier; 22:37–22:41
  local: the #549 tip to docs/tests-readme-map 18 s after creating the
  real map commit, the same SHA to feat/cloud-lab-runner, and `5f2201e4`
  to test/crash-injection-lane). Failure mode: an agent pushing HEAD to
  every branch it touched. All four branches were restored from dangling
  SHAs, rebased, pushed, and commented.
- **Guardrails landed:** worktree-scoped pre-push hook (only the branch in
  `.pr-branch` may be pushed — verified in both directions) + series sanity
  rule in the fix template. Runner cleanup now diffs before/after branch
  lists instead of globbing (`pr/[0-9]*`-style names leak through
  `pr-[0-9]*` globs).
- **Fix batch:** 7/8 PRs fixed+rebased+pushed and MERGEABLE; #531 correctly
  stopped by the no-improvisation rule (#605 reversed its policy —
  maintainer decision framed in the PR thread). #549 merge-prep done
  (dropped redundant tidy commit, fixed stale PENDING header, suites
  green). #391/#511 closed with evidence-based rationale.
- **Two PRs opened mid-marathon (#611, #612) were reviewed in the closing
  sweep** — both land-with-followups. Lesson: refresh `gh pr list` in the
  closing sweep, not just at the start.
- **Ops notes:** `gh pr edit --add-reviewer` is broken by the Projects
  (classic) GraphQL deprecation — use
  `gh api -X POST repos/…/pulls/N/requested_reviewers -f 'reviewers[]=…'`.
  herdr settle-detection killed one agent between turns before its push;
  the runner now verifies the GitHub side-effect before shutdown and
  re-prompts once. opencode can hit approval dialogs mid-review (one
  `blocked` state) — read the dialog and answer it; do not let the runner
  kill a blocked agent.
