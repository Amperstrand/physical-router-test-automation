#!/usr/bin/env bash
# private-lock-probe.sh — drive the suite's hermeticity guard in isolation.
#
# run-tests.sh must prove that the guard which keeps it off the production bench lock actually
# FIRES. Running a second copy of the whole suite to find out is wasteful, so this probe sources
# the real harness library and calls the real function — the guard under test is never a copy of
# the guard (a copy would be free to drift away from the one the suite uses).
#
#   BENCH_LOCK_PATH=<path> tests/mt3000-bench/harness/private-lock-probe.sh <private workdir>
#
# exit 0  = the lock path is private (safe to run)
# exit 90 = refused: the path is the production lock, empty, or outside the private workdir
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib.sh"
bench_assert_private_lock "${1:-}"
