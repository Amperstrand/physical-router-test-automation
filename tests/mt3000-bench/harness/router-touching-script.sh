#!/usr/bin/env bash
# TEST DOUBLE — stands in for "any router-touching script" in the bench-lock negative
# controls. The point of the test is that this script REFUSES to run while another owner
# holds the bench, and that the error names that holder.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK="$HERE/../../../scripts/mt3000-bench/bench-lock.sh"

"$LOCK" require || exit $?

echo "ROUTER-TOUCHED: the bench lock was held, so the real ssh/apk commands would run here"
echo "router-touching-script: would now do: ssh root@192.168.1.1 'sha256sum /usr/bin/tollgate-wrt'"
