#!/usr/bin/env bash
#
# bench-with-lock.sh — the ONLY sanctioned way to touch the bench MT3000 bench router.
#
# Thin wrapper over `bench-lock.sh exec`: it acquires the single-owner bench lock
# (refusing, with the current holder's identity, if another window owns the bench),
# runs your command with the lock held, and releases it when the command exits.
#
#   scripts/mt3000-bench/bench-with-lock.sh --purpose "smoke test" -- ./my-router-script.sh
#   scripts/mt3000-bench/bench-with-lock.sh --purpose "curl|bash validation" --task t_aa94ad3b -- \
#       bash ~/tollgate-pre16-validation/run.sh
#
# A router-touching script can ALSO insist on being run this way, from inside itself:
#
#   "$(dirname "$0")/bench-lock.sh" require     || exit $?
#
# The lock file is ~/.hermes/state/bench-mt3000.lock (override: BENCH_LOCK_PATH).
# See scripts/mt3000-bench/README.md and the tollgate-development skill.
#
set -uo pipefail
# resolve the symlink: install.sh puts `bench-with-lock` in ~/.local/bin pointing here, and
# $BASH_SOURCE is then the SYMLINK - using its dirname would look for bench-lock.sh next to
# the symlink (measured live: `~/.local/bin/bench-lock.sh: No such file or directory`).
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
HERE="$(cd "$(dirname "$SELF")" && pwd)"
exec "$HERE/bench-lock.sh" exec "$@"
