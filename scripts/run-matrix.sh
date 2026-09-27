#!/usr/bin/env bash
# Cross-implementation test matrix — runs stories against multiple TollGate
# implementations and produces a markdown comparison table.
#
# Usage: scripts/run-matrix.sh [--dut name:ip:host ...]
#
# Example:
#   scripts/run-matrix.sh \
#     --dut nr7101:192.168.13.124:phone \
#     --dut openwrt-vm:10.99.99.1:debian-vm
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && dirname "$0")"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="${TOLLGATE_PYTHON_VENV:-$HOME/venvs/rig-labgrid}"

declare -a DUT_NAMES=()
declare -a DUT_IPS=()
declare -a DUT_CLIENTS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dut)
            IFS=':' read -r name ip client <<< "$2"
            DUT_NAMES+=("$name")
            DUT_IPS+=("$ip")
            DUT_CLIENTS+=("$client")
            shift 2
            ;;
        *) shift ;;
    esac
done

if [[ ${#DUT_NAMES[@]} -eq 0 ]]; then
    echo "No DUTs specified. Use --dut name:ip:client"
    exit 1
fi

echo "# Cross-Implementation Test Matrix"
echo ""
echo "Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

# Results storage
RESULTS_DIR="/tmp/opencode/matrix-results"
mkdir -p "$RESULTS_DIR"

for i in "${!DUT_NAMES[@]}"; do
    name="${DUT_NAMES[$i]}"
    ip="${DUT_IPS[$i]}"
    client="${DUT_CLIENTS[$i]}"
    logfile="$RESULTS_DIR/$name.log"

    echo "## Running stories against: $name ($ip, client: $client)"
    echo ""

    # Set up environment for this DUT
    export TOLLGATE_SSH_HOST="$ip"
    export TOLLGATE_SSID="TollGate"

    if [[ "$client" == "phone" ]]; then
        export PHONE_SERIAL="${PHONE_SERIAL:-ZY326DPC7R}"
        unset TOLLGATE_DEBIAN_HOST
    elif [[ "$client" == "debian-vm" ]]; then
        export TOLLGATE_DEBIAN_HOST="10.99.99.100"
        unset PHONE_SERIAL
    fi

    # Run the stories
    cd "$REPO_ROOT"
    if "$VENV/bin/python" -m pytest tests/stories/ \
        --no-deploy --timeout-method=signal -v --tb=line \
        2>&1 | tee "$logfile"; then
        echo "✅ $name: ALL STORIES PASSED"
    else
        echo "❌ $name: SOME STORIES FAILED"
    fi
    echo ""
done

# Build the matrix table
echo "## Test Matrix"
echo ""
echo "| Story | ${DUT_NAMES[*]} |"
echo "|-------|${DUT_NAMES[@]/#/|---|} |"

# Extract results per DUT
for story_file in "$REPO_ROOT"/tests/stories/test_*.py; do
    story_name=$(basename "$story_file" .py)
    # Skip conftest
    [[ "$story_name" == "conftest" ]] && continue

    row="| $story_name "
    for i in "${!DUT_NAMES[@]}"; do
        name="${DUT_NAMES[$i]}"
        logfile="$RESULTS_DIR/$name.log"
        if [[ ! -f "$logfile" ]]; then
            row+="| ⬜ not run "
            continue
        fi
        # Count pass/fail for this story
        passed=$(grep "PASSED" "$logfile" 2>/dev/null | grep -c "$story_name" || true)
        failed=$(grep "FAILED" "$logfile" 2>/dev/null | grep -c "$story_name" || true)
        passed=${passed:-0}
        failed=${failed:-0}
        if [[ "$failed" -gt 0 ]]; then
            row+="| ❌ $failed fail "
        elif [[ "$passed" -gt 0 ]]; then
            row+="| ✅ $passed pass "
        else
            row+="| ⏭️ skipped "
        fi
    done
    row+="|"
    echo "$row"
done

echo ""
echo "## Summary"
for i in "${!DUT_NAMES[@]}"; do
    name="${DUT_NAMES[$i]}"
    logfile="$RESULTS_DIR/$name.log"
    total=$(grep -E "PASSED|FAILED" "$logfile" 2>/dev/null | wc -l || true)
    passed=$(grep "PASSED" "$logfile" 2>/dev/null | wc -l || true)
    failed=$(grep "FAILED" "$logfile" 2>/dev/null | wc -l || true)
    total=${total:-0}
    passed=${passed:-0}
    failed=${failed:-0}
    echo "- **$name**: $passed/$total passed, $failed failed"
done
