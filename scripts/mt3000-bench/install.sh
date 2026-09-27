#!/usr/bin/env bash
# Install the bench tooling onto PATH as `bench-lock`, `bench-with-lock`, `bench-deploy-apk`,
# `router-snapshot` and `bench-token`.
#
# It SYMLINKS into ~/.local/bin (or $BENCH_BIN_DIR) so there is exactly one copy of the
# code — no drift between a checkout and an installed copy. Run it from whichever checkout
# you want to be authoritative:
#
#   ~/physical-router-test-automation/scripts/mt3000-bench/install.sh     # the kit (default)
#   ~/worktrees/bench-mt3000-lock/scripts/mt3000-bench/install.sh        # a feature worktree
#
# Re-run it after merging into the kit checkout so the links follow the main tree.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${BENCH_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BIN_DIR" || exit 1

rc=0
for s in bench-lock bench-with-lock bench-deploy-apk router-snapshot; do
  chmod +x "$HERE/$s.sh" || true
  ln -sfn "$HERE/$s.sh" "$BIN_DIR/$s" && printf 'linked %s -> %s\n' "$BIN_DIR/$s" "$HERE/$s.sh" || rc=1
done
# the token tool is Python (and resolves the repo through its own symlink)
chmod +x "$HERE/bench-token.py" || true
ln -sfn "$HERE/bench-token.py" "$BIN_DIR/bench-token" \
  && printf 'linked %s -> %s\n' "$BIN_DIR/bench-token" "$HERE/bench-token.py" || rc=1

printf '\n'
case ":$PATH:" in
  *":$BIN_DIR:"*) printf '%s is on PATH\n' "$BIN_DIR" ;;
  *) printf 'NOTE: %s is NOT on PATH; add it, or call the scripts by absolute path.\n' "$BIN_DIR" ;;
esac
"$BIN_DIR/bench-lock" --help >/dev/null 2>&1 || { printf 'WARNING: bench-lock did not run from %s\n' "$BIN_DIR" >&2; rc=1; }
exit "$rc"
