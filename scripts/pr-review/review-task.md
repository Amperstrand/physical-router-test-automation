# PR Review Task — tollgate-module-basic-go

You are reviewing ONE pull request of `OpenTollGate/tollgate-module-basic-go`.
The PR number is given in the prompt that sent you here.

## Workspace rules (hard constraints)

- Work ONLY in `/tmp/pr-review-wt` — a linked git worktree on branch
  `pr-review-base`, reset to `origin/main`. Verify with `git -C /tmp/pr-review-wt status`.
- NEVER touch `/home/ubuntu/src/tollgate-module-basic-go` (humans/other agents use it).
- Do NOT use herdr. Do NOT push, commit, rebase, or force anything.
- The ONLY GitHub write you perform is exactly ONE review comment on the PR.
- Scratch files go in `/tmp/opencode/` only. Leave the worktree clean when done
  (`git checkout pr-review-base` + delete ONLY the exact branch name that
  `gh pr checkout` created, e.g. `git branch -D feature/foo`. NEVER delete
  branches by glob or filtered list — this is a SHARED git dir serving several
  worktrees; a broad delete destroys unrelated branches).

## Procedure

0. **Pre-flight (do this before anything else)**: confirm the PR is OPEN
   and not draft (`gh pr view <N> --json state,isDraft`); if closed,
   merged or draft, do not review — report `disposition: skipped`.
   Then **verify the head carries what the title claims**: `gh pr diff <N>
   --name-only` must plausibly match the PR's subject (this repo has had
   branches force-pushed over with unrelated commit series; if the diff
   contradicts the title, that IS the review finding — disposition
   request-changes with the evidence).
1. **Read the rubric**: `/tmp/pr-review-wt/PR-REVIEW.md`. Execute it faithfully —
   all of Step 1 (triage), Step 2 (context), the 13 criteria of Step 3,
   Step 4 (composition), Step 5 (filtering), Step 6 (citations).
2. **Read repo guidance**: `/tmp/pr-review-wt/AGENTS.md`. If the diff touches
   payments, wallets, sessions, gates, mints, payouts, retries, or persistent
   identifiers, the "Fund safety, crash consistency, and distributed
   transaction invariants" section is mandatory context — its hard rules
   (derivation-counter discipline, mint-URL canonicalization, no irreversible
   op before local validation, ambiguous-result reconciliation, partial-success
   preservation) are exactly what a maintainer will check against.
3. **Gather context** (Step 2 of the rubric):
   - `cd /tmp/pr-review-wt && gh pr view <N> --json title,body,author,headRefName,baseRefName,headRefOid,baseRefOid,mergeable,statusCheckRollup,commits`
   - `gh pr diff <N>` (pipe to a file if large)
   - Base freshness: `git fetch origin main && git rev-list --count origin/main..<headRefOid>` and the reverse.
   - Check out the PR head if you need to build/test: `gh pr checkout <N>`
     (from `/tmp/pr-review-wt`). Record `git rev-parse HEAD` for permalinks.
   - Skim `gh issue list` and `gh pr list` for overlap (criterion 12).
   - `git blame` before flagging anything as a bug (rubric Step 2.6).
   - Note: GitHub-side status checks may be absent; ngit mirror CI is the build
     of record for this repo. Check `.github/workflows/` only if relevant.
4. **Verify claims** (criterion 4): walk each claim in the PR body against diff
   lines. Where cheap and decisive, run targeted tests in the worktree, e.g.
   `cd /tmp/pr-review-wt/src && go test ./<pkg>/... -count=1 -tags testenv`.
   The full battery (`make go-battery` from the worktree root) takes several
   minutes — run it only when CI evidence is missing AND the change is
   money-path-critical or claims behavior only tests can prove.
5. **Compose the review** per rubric Steps 4–6: natural prose narrative, all 13
   criteria addressed somewhere, short subheadings where they aid scanning,
   concrete fix suggestions inline, full-SHA permalinks, closing disposition
   (`land` / `land-with-followups` / `request-changes` / `hold-for-thematic-batch`).
   House style for the comment opening, e.g.:
   `**Technical review (evidence pass)** — PR head `<full sha>` (base <n> commits behind main).`
   Tone: senior maintainer, evidence-first, no fluff, no AI attribution.
6. **Post exactly one comment**:
   `BCR_GH_WRITE_OK=1 gh pr comment <N> --body-file /tmp/opencode/review-<N>.md`
   (the local gh wrapper blocks writes to non-owned repos; the env-bypass
   is its documented escape hatch and every use is logged — use it for
   this comment only). If a NEWER review comment from another reviewer or
   the author exists since the PR's last update, lead with the delta from
   that thread instead of re-walking the whole PR.
   Verify it posted (capture the URL from the command output).

## Triage shortcut (rubric Step 1)

If the PR is trivially small/obviously correct, post a compact informal
one-paragraph review instead of the full 13-criteria pass — say that's what it
is. If the PR is draft/closed/merged, do not post; report that.

## Final session output (NOT part of the PR comment)

End your turn with exactly this block:

```
REVIEW-RESULT
pr: <N>
head: <full head sha>
disposition: <land|land-with-followups|request-changes|hold-for-thematic-batch|informal-ok|skipped>
posted: <comment URL or "no" + reason>
test-recommendation: <one of: none|go-battery|cloud-lab|ai-legion|qemu-local>
test-rationale: <one line — what a runtime test would prove that reading cannot>
key-findings:
- <finding 1>
- <finding 2>
- <finding 3 (max 5, most important only)>
```
