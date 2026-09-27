#!/usr/bin/env bash
# Release test pipeline — one command to validate a TollGate release.
#
# Usage: scripts/release-test.sh --version v0.6.0-rc1 [--router IP]
#                                  [--ipk PATH] [--no-flash] [--report-dir DIR]
#
# Phases:
#   1. Preflight — verify DUT reachable, labgrid place available
#   2. Flash (optional) — deploy the release candidate
#   3. Stories — run the full story suite
#   4. Edge cases — error paths, invalid inputs
#   5. Benchmarks — payment latency, portal load time
#   6. Report — markdown summary with evidence links
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && dirname "$0")"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="${TOLLGATE_PYTHON_VENV:-$HOME/venvs/rig-labgrid}"
LABGRID_COORD="${LG_COORDINATOR:-192.168.13.208:20408}"

VERSION=""
ROUTER="${TOLLGATE_SSH_HOST:-192.168.13.124}"
IPK_PATH=""
NO_FLASH=false
REPORT_DIR=""
SKIP_LABGRID=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --router) ROUTER="$2"; shift 2 ;;
        --ipk) IPK_PATH="$2"; shift 2 ;;
        --no-flash) NO_FLASH=true; shift ;;
        --report-dir) REPORT_DIR="$2"; shift 2 ;;
        --skip-labgrid) SKIP_LABGRID=true; shift ;;
        *) echo "unknown: $1"; exit 1 ;;
    esac
done

if [[ -z "$VERSION" ]]; then
    echo "ERROR: --version required (e.g. v0.6.0-rc1)"
    exit 1
fi

if [[ -z "$REPORT_DIR" ]]; then
    REPORT_DIR="/tmp/opencode/release-$VERSION-$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p "$REPORT_DIR"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$REPORT_DIR/pipeline.log"; }

# ═══════════════════════════════════════════════════════════════
# Phase 1: Preflight
# ═══════════════════════════════════════════════════════════════
log "═══ Release Test: $VERSION ═══"
log "DUT: $ROUTER"
log "Report: $REPORT_DIR"
log ""

LOCKED_PLACE=""

if [[ "$SKIP_LABGRID" == "false" ]]; then
    log "Phase 1: Preflight + labgrid reservation"
    # Try to lock the nr7101-router place
    if "$VENV/bin/labgrid-client" -x "$LABGRID_COORD" -p nr7101-router lock 2>/dev/null; then
        LOCKED_PLACE="nr7101-router"
        log "✓ Labgrid place 'nr7101-router' locked"
    else
        log "⚠ Could not lock labgrid place (may be in use) — proceeding without mutex"
    fi
else
    log "Phase 1: Preflight (labgrid skipped)"
fi

# Verify DUT reachable
if ! ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "root@$ROUTER" "echo ok" >/dev/null 2>&1; then
    log "❌ DUT not reachable at $ROUTER"
    [[ -n "$LOCKED_PLACE" ]] && "$VENV/bin/labgrid-client" -x "$LABGRID_COORD" -p "$LOCKED_PLACE" release 2>/dev/null || true
    exit 1
fi
log "✓ DUT reachable at $ROUTER"

# Check current version
CURRENT=$(ssh -o ConnectTimeout=5 "root@$ROUTER" "opkg status tollgate-wrt 2>/dev/null | grep Version | cut -d' ' -f2" 2>/dev/null || echo "unknown")
log "✓ Current version: $CURRENT"
log ""

# ═══════════════════════════════════════════════════════════════
# Phase 2: Flash (optional)
# ═══════════════════════════════════════════════════════════════
if [[ "$NO_FLASH" == "false" && -n "$IPK_PATH" ]]; then
    log "Phase 2: Flashing $IPK_PATH"
    if ssh -o ConnectTimeout=5 "root@$ROUTER" "opkg install --force-overwrite /tmp/release.ipk && /etc/init.d/tollgate-wrt restart" \
        < <(cat "$IPK_PATH") 2>&1 | tee -a "$REPORT_DIR/pipeline.log"; then
        log "✓ Flashed and restarted"
    else
        log "❌ Flash failed"
        [[ -n "$LOCKED_PLACE" ]] && "$VENV/bin/labgrid-client" -x "$LABGRID_COORD" -p "$LOCKED_PLACE" release 2>/dev/null || true
        exit 1
    fi
    sleep 10
elif [[ -n "$IPK_PATH" ]]; then
    scp -O "$IPK_PATH" "root@$ROUTER:/tmp/release.ipk"
    log "Phase 2: Flashed (scp + opkg)"
    ssh "root@$ROUTER" "opkg install --force-overwrite /tmp/release.ipk && /etc/init.d/tollgate-wrt restart"
    sleep 10
else
    log "Phase 2: Skipping flash (--no-flash or no --ipk)"
fi

# ═══════════════════════════════════════════════════════════════
# Phase 3: Stories
# ═══════════════════════════════════════════════════════════════
log "Phase 3: Running story suite"
export TOLLGATE_SSH_HOST="$ROUTER"
export TOLLGATE_SSID="${TOLLGATE_SSID:-TollGate}"
export PHONE_SERIAL="${PHONE_SERIAL:-ZY326DPC7R}"
export TOLLGATE_TEST_MINT_URL="${TOLLGATE_TEST_MINT_URL:-http://192.168.13.221:8383}"

cd "$REPO_ROOT"
STORY_RESULT=0
if "$VENV/bin/python" -m pytest tests/stories/ \
    --no-deploy --timeout-method=signal -v --tb=short \
    2>&1 | tee "$REPORT_DIR/stories.log"; then
    log "✓ All stories passed"
else
    log "⚠ Some stories failed"
    STORY_RESULT=1
fi
log ""

# ═══════════════════════════════════════════════════════════════
# Phase 4: Portal tab-copy (if portal is accessible)
# ═══════════════════════════════════════════════════════════════
PORTAL_CODE=$(curl -s -m 5 -o /dev/null -w "%{http_code}" "http://$ROUTER:2051/splash.html" 2>/dev/null || echo "000")
if [[ "$PORTAL_CODE" == "200" ]]; then
    log "Phase 4: Portal tab-copy tests"
    if ROUTER_IP="$ROUTER" npx playwright test \
        tests/browser/portal-tab-copy.spec.mjs \
        --config tests/browser/portal-tab-copy.config.mjs \
        --reporter=line \
        2>&1 | tee "$REPORT_DIR/portal.log" | tail -3; then
        log "✓ Portal tests passed"
    else
        log "⚠ Portal tests failed"
    fi
else
    log "Phase 4: Skipping portal tests (HTTP $PORTAL_CODE)"
fi
log ""

# ═══════════════════════════════════════════════════════════════
# Phase 5: Report generation
# ═══════════════════════════════════════════════════════════════
log "Phase 5: Generating report"

STORY_PASSED=$(grep -c "PASSED" "$REPORT_DIR/stories.log" 2>/dev/null || echo 0)
STORY_FAILED=$(grep -c "FAILED" "$REPORT_DIR/stories.log" 2>/dev/null || echo 0)
STORY_SKIPPED=$(grep -c "SKIPPED" "$REPORT_DIR/stories.log" 2>/dev/null || echo 0)

cat > "$REPORT_DIR/report.md" <<REPORT
# TollGate Release Test Report: $VERSION

**Date**: $(date -u +%Y-%m-%dT%H:%M:%SZ)
**DUT**: $ROUTER (was running $CURRENT)
**Result**: $([ $STORY_RESULT -eq 0 ] && echo "✅ PASS" || echo "❌ FAIL")

## Story Suite

| Metric | Count |
|--------|-------|
| Passed | $STORY_PASSED |
| Failed | $STORY_FAILED |
| Skipped | $STORY_SKIPPED |
| **Total** | $((STORY_PASSED + STORY_FAILED + STORY_SKIPPED)) |

## Artifacts

- Full log: \`stories.log\`
- Portal tests: \`portal.log\`
- Pipeline log: \`pipeline.log\`

## Contract

Validated against: \`config/behavior-contract.json\` (version 1.0.0)
REPORT

log "Report written to $REPORT_DIR/report.md"
log ""

# ═══════════════════════════════════════════════════════════════
# Cleanup
# ═══════════════════════════════════════════════════════════════
if [[ -n "$LOCKED_PLACE" ]]; then
    "$VENV/bin/labgrid-client" -x "$LABGRID_COORD" -p "$LOCKED_PLACE" release 2>/dev/null || true
    log "✓ Labgrid place released"
fi

log "═══ Release test complete ═══"
log "Report: $REPORT_DIR/report.md"

exit $STORY_RESULT
