#!/usr/bin/env bash
# Run user-story tests against a TollGate router.
#
# Usage: scripts/run-stories.sh [--router IP] [--mint URL] [--phone SERIAL]
#
# This is the CI entrypoint for user-story tests. It:
# 1. Verifies the router is reachable
# 2. Sets up SSH tunnels if the backend is WAN-firewalled
# 3. Runs the story test suite
# 4. Reports pass/fail + evidence paths
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

ROUTER="${TOLLGATE_SSH_HOST:-}"
MINT="${TOLLGATE_TEST_MINT_URL:-http://192.168.13.221:8383}"
PHONE="${PHONE_SERIAL:-}"
SSID="${TOLLGATE_SSID:-TollGate}"
VENV="${TOLLGATE_PYTHON_VENV:-$HOME/venvs/rig-labgrid}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --router) ROUTER="$2"; shift 2 ;;
        --mint) MINT="$2"; shift 2 ;;
        --phone) PHONE="$2"; shift 2 ;;
        --ssid) SSID="$2"; shift 2 ;;
        *) echo "unknown arg: $1"; exit 1 ;;
    esac
done

if [[ -z "$ROUTER" ]]; then
    echo "ERROR: router not specified (use --router or TOLLGATE_SSH_HOST)"
    exit 1
fi

echo "═══ TollGate Story Runner ═══"
echo "Router: $ROUTER"
echo "Mint:   $MINT"
echo "Phone:  ${PHONE:-not set}"
echo "SSID:   $SSID"
echo ""

# Verify router reachable
if ! ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "root@$ROUTER" "echo ok" >/dev/null 2>&1; then
    echo "ERROR: router not reachable at $ROUTER"
    exit 1
fi
echo "✓ Router reachable"

# Verify mint reachable
if ! curl -s -m 5 -o /dev/null -w '' "$MINT/v1/info" 2>/dev/null; then
    echo "WARNING: mint may not be reachable at $MINT"
fi
echo "✓ Mint checked"

# Run stories
echo ""
echo "Running stories..."
cd "$REPO_ROOT"

export TOLLGATE_SSH_HOST="$ROUTER"
export TOLLGATE_SSID="$SSID"
export TOLLGATE_TEST_MINT_URL="$MINT"
if [[ -n "$PHONE" ]]; then
    export PHONE_SERIAL="$PHONE"
fi

"$VENV/bin/python" -m pytest tests/stories/ \
    --no-deploy --timeout-method=signal -v --tb=short \
    2>&1 | tee /tmp/story-run.log

EXIT_CODE=$?

echo ""
if [[ $EXIT_CODE -eq 0 ]]; then
    echo "✅ All stories PASSED"
else
    echo "❌ Some stories FAILED"
fi

# Report evidence
RESULTS_DIR=$(ls -td "$REPO_ROOT"/results/test-* 2>/dev/null | head -1)
if [[ -n "$RESULTS_DIR" && -d "$RESULTS_DIR/artifacts" ]]; then
    echo ""
    echo "Evidence: $RESULTS_DIR/artifacts/"
    find "$RESULTS_DIR/artifacts" -name "*.png" -o -name "*.json" | head -10
fi

exit $EXIT_CODE
