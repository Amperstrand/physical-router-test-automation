# PR Fix-and-Rebase Task — tollgate-module-basic-go

You are fixing ONE pull request of `OpenTollGate/tollgate-module-basic-go`
according to its own review thread. The PR number is given in the prompt.

## Workspace rules (hard constraints)

- Work ONLY in `/tmp/pr-review-wt` — a linked git worktree on branch
  `pr-review-base` tracking `origin/main`.
- NEVER touch `/home/ubuntu/src/tollgate-module-basic-go` (humans/other
  agents use it). Do NOT use herdr.
- You MAY push this PR's branch and post exactly ONE PR comment. Nothing
  else is written anywhere.
- Push ONLY via the ssh remote: `git push --force-with-lease ot-ssh
  HEAD:<branch>` (the PR head branches live in the OpenTollGate repo).
- Delete only the exact local branch name you created — never by glob;
  this is a SHARED git dir and a broad delete destroys unrelated branches.
- Posting the comment needs `BCR_GH_WRITE_OK=1 gh pr comment ...` (the
  local gh wrapper's documented escape hatch; every use is logged).
- Scratch files go in `/tmp/opencode/` only.

## Procedure

1. **Read the fix list**: `gh pr view <N> --json
   comments,headRefName,headRefOid` and take the LAST comment authored by
   `Amperstrand` — it is the authoritative review. Also check for any
   NEWER comments from other reviewers/maintainers; if the head SHA moved
   since that review, re-check each finding against the new head before
   fixing.
2. **Checkout**: `cd /tmp/pr-review-wt && git fetch origin main --quiet &&
   gh pr checkout <N>`; record `git rev-parse HEAD`.
3. **Implement** exactly the review's blocking items plus any one-liners
   it explicitly lists. NO scope creep: if an item needs a design decision
   the review did not already make, STOP and report it as deferred instead
   of improvising.
4. **Rebase** onto `origin/main`. CHANGELOG conflicts follow the
   keep-both pattern (never drop either side's entries; no duplicate
   section headers; entries live under `[Unreleased]` in the right
   category with `([#N](...))` links per house style). If a non-CHANGELOG
   conflict cannot be resolved faithfully to both intents, STOP and report.
5. **Verify** what you touched: `bash -n` on shell scripts, the PR's own
   offline test scripts where runnable (e.g.
   `node tests/contract/js-schema-lint.mjs`,
   `bash tests/contract/<check>.sh`), `go test` for any Go package you
   modified (`cd src/<module> && go test -tags testenv -count=1 ./...`).
   Full `make go-battery` only if you changed Go logic.
6. **Push**: `git push --force-with-lease ot-ssh HEAD:<headRefName>`.
7. **Comment** (exactly one, via the env-bypass): what you changed per
   finding, the verification you ran with results, and the new head SHA.
8. Clean the worktree: back to `pr-review-base`, delete only your exact
   local branch.

## Final session output (NOT part of the PR comment)

```
FIX-RESULT
pr: <N>
old-head: <sha>
new-head: <sha>
pushed: yes|no
verified: <one line per check + result>
deferred: <items left open and why, or "none">
```
