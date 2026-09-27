#!/bin/bash
# run-agent-task.sh <name> <task-file> <prompt-text> — one sequential opencode
# agent in pane $PANE (default wD:p1) executing a PR task defined by a
# markdown file. Hardened by the 2026-09-26/27 marathons:
#   - verifies the pane is an idle shell WITH no live/stopped jobs
#   - verifies prompt delivery (herdr's 5s gate can error while the prompt
#     actually landed; resending blindly double-submits)
#   - verifies the GitHub side-effect before declaring success
#   - guards the shared git dir branch count (overbroad agent deletes)
set -u
NAME="$1"; TASKFILE="$2"; PROMPT="$3"
PANE="${PANE:-wD:p1}"
WT="${WT:-/tmp/pr-review-wt}"
LOG="/tmp/opencode/${NAME}.log"
: > "$LOG"
fail() { echo "RUN FAILED: $1" | tee -a "$LOG"; exit 1; }

branch_count_before=$(git -C "$WT" branch --format='%(refname:short)' | sort > /tmp/opencode/${NAME}.branches-before; wc -l < /tmp/opencode/${NAME}.branches-before)

pane_state=$(herdr pane read "$PANE" --source detection --lines 3 2>&1 | tail -3)
echo "$pane_state" >> "$LOG"
echo "$pane_state" | grep -q '\$' || fail "pane $PANE not an idle shell"
herdr pane run "$PANE" "jobs -l" >/dev/null 2>&1; sleep 1
jobs_out=$(herdr pane read "$PANE" --source recent-unwrapped --lines 3 2>&1 | tail -3)
echo "$jobs_out" | grep -qE 'Running|Stopped' && fail "pane has live/stopped jobs: $jobs_out"

herdr agent start "$NAME" --kind opencode --pane "$PANE" >> "$LOG" 2>&1 || fail "agent start"

delivered=0
for attempt in 1 2 3; do
  out=$(herdr agent prompt "$NAME" "$PROMPT" --wait --timeout 2700000 2>&1)
  echo "$out" >> "$LOG"
  echo "$out" | grep -q '"type":"agent_prompted"' && { delivered=1; break; }
  sleep 20
  status=$(herdr agent get "$NAME" 2>/dev/null | grep -o '"agent_status":"[a-z]*"' | head -1)
  case "$status" in
    *working*) herdr agent wait "$NAME" --timeout 2700000 >> "$LOG" 2>&1; delivered=1; break ;;
    *blocked*) fail "agent blocked on approval/question UI" ;;
  esac
done
[ "$delivered" = "1" ] || fail "prompt never delivered"

herdr agent read "$NAME" --source recent-unwrapped --lines 110 >> "$LOG" 2>&1

# Check BEFORE shutdown: herdr can report the agent settled between turns
# while it is mid-task; re-prompt the same agent instead of killing it.
if [ -n "${CHECK:-}" ]; then
  for check_attempt in 1 2; do
    if eval "$CHECK" >> "$LOG" 2>&1; then break; fi
    echo "side-effect missing after attempt $check_attempt — re-prompting agent" >> "$LOG"
    out=$(herdr agent prompt "$NAME" "Your task is incomplete — the expected GitHub side effect is not visible yet. Continue exactly where you left off (do not redo finished steps), complete the remaining steps including the push and the single PR comment, then print the final result block." --wait --timeout 1800000 2>&1)
    echo "$out" >> "$LOG"
    herdr agent read "$NAME" --source recent-unwrapped --lines 80 >> "$LOG" 2>&1
  done
  eval "$CHECK" >> "$LOG" 2>&1 || echo "WARN: side-effect check still failing — inspect $LOG" | tee -a "$LOG"
fi

herdr agent send-keys "$NAME" ctrl+c >> "$LOG" 2>&1
sleep 4
herdr pane read "$PANE" --source detection --lines 3 >> "$LOG" 2>&1

# Worktree cleanup + shared-git-dir guard.
git -C "$WT" checkout pr-review-base --quiet 2>>"$LOG"
git -C "$WT" branch --list 'pr-[0-9]*' --format='%(refname:short)' | while read -r b; do
  git -C "$WT" branch -D "$b" --quiet
done 2>>"$LOG"
git -C "$WT" reset --hard origin/main --quiet >>"$LOG" 2>&1
git -C "$WT" clean -fdq >>"$LOG" 2>&1
git -C "$WT" branch --format='%(refname:short)' | sort > /tmp/opencode/${NAME}.branches-after
comm -13 /tmp/opencode/${NAME}.branches-before /tmp/opencode/${NAME}.branches-after \
  | grep -vx 'pr-review-base' \
  | while read -r b; do git -C "$WT" branch -D "$b" --quiet; done 2>>"$LOG"
branch_count_after=$(git -C "$WT" branch | wc -l)
[ "$branch_count_after" -le "$branch_count_before" ] || echo "WARN: branch count grew ($branch_count_before -> $branch_count_after)" | tee -a "$LOG"

echo "RUN DONE $NAME"
tail -50 "$LOG"
