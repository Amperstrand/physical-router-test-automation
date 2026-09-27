# PR review/fix marathon tooling

One oversight session orchestrating sequential one-shot opencode agents
(over herdr) against a shared linked git worktree. Used successfully for
the 2026-09-26/27 marathon (30 PRs reviewed, 8 fixed+rebased, 4
contaminated branches restored). Full operational lessons:
[docs/pr-review-marathon-2026-09-27.md](../../docs/pr-review-marathon-2026-09-27.md).

## Setup (once)

```bash
cd /home/ubuntu/src/tollgate-module-basic-go
herdr worktree create --cwd . --path /tmp/pr-review-wt \
  --branch pr-review-base --base origin/main --label pr-reviews \
  --no-focus --trust-repository     # note the returned pane ID
cp scripts/pr-review/review-task.md /tmp/opencode/   # agents read it from there
```

The task templates reference `/tmp/pr-review-wt` and `/tmp/opencode/` —
copy them there (or edit the paths) before prompting agents.

## One review

```bash
scripts/pr-review/run-agent-task.sh pr<N> /tmp/opencode/review-task.md \
  "Execute the PR review task defined in /tmp/opencode/review-task.md for PR #<N> (<title>). The PR number is <N>. Work in /tmp/pr-review-wt only. When done, print the REVIEW-RESULT block."
```

`CHECK` (optional) is a shell condition verifying the GitHub side-effect,
e.g. `CHECK="gh pr view <N> --json comments --jq '.comments[-1].author.login' | grep -q Amperstrand"`.

## One fix-and-rebase (agent implements the review's blocking items)

```bash
scripts/pr-review/run-agent-task.sh fix<N> /tmp/opencode/fix-task.md \
  "Execute the PR fix-and-rebase task defined in /tmp/opencode/fix-task.md for PR #<N> (<title>). The PR number is <N>. Work in /tmp/pr-review-wt only. When done, print the FIX-RESULT block."
```

## Templates

- `review-task.md` — the review agent's contract (PR-REVIEW.md rubric,
  pre-flight draft/merged/head-content checks, one comment, REVIEW-RESULT
  block with closed-enum test recommendation).
- `fix-task.md` — the fix agent's contract (latest Amperstrand review
  comment as the authoritative fix list, keep-both CHANGELOG rebase
  pattern, push via `ot-ssh`, verification, FIX-RESULT block).

## Rules that earned their place

- Agents never delete branches by glob (shared git dir blast radius).
- The posted GitHub comment is the completion signal, not the pane
  transcript.
- Prompt delivery is verified via `herdr agent get` after any herdr
  error; never blind-resend.
- One comment per agent; `BCR_GH_WRITE_OK=1` only for that comment.
