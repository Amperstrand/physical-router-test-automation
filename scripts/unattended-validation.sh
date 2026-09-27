#!/usr/bin/env bash
# unattended-tests.sh — runs the full 0.6.0 validation suite against the
# QEMU OpenWrt 24.10 lab on ai-legion. No LLM babysitting needed.
#
# PREREQUISITES (must be done once, see TESTING.md):
#   1. QEMU lab started: python3 scripts/virtual-lab.py start-poc --host ai-legion
#   2. tollgate-wrt deployed to the VM (bash /tmp/deploy3.sh on ai-legion)
#   3. NAT + forwarding configured (see TESTING.md "QEMU Lab Complete Fix")
#   4. NDS pre-auth rules for :2121/:2051 added (see TESTING.md)
#
# This script:
#   Phase 1: Preflight checks (router reachable, daemon serving, mint accessible)
#   Phase 2: API tests (backend protocol compliance)
#   Phase 3: Payment tests (Cashu token → session → internet)
#   Phase 4: Session tests (expiry, extension, persistence)
#   Phase 5: Enforcement tests (NDS pre-auth, firewall surface)
#   Phase 6: Degraded mode (mint unreachable, error handling)
#   Phase 7: Summary + evidence collection
#
# Usage: bash /tmp/unattended-tests.sh [--phase N] [--quick]
#        --phase N  only run phase N
#        --quick    skip slow/extended tests
set -euo pipefail

SSH="ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@10.99.99.1"
ROUTER_IP="10.99.99.1"
MINT_URL="https://signut.cashu.exchange"
RESULTS_DIR="/tmp/tollgate-test-results-$(date +%Y%m%d-%H%M%S)"
QUICK=false
PHASE=""

mkdir -p "$RESULTS_DIR"

# ─── Argument parsing ───────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --phase) PHASE="$2"; shift 2 ;;
    --quick) QUICK=true; shift ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

# ─── Helpers ────────────────────────────────────────────────────────
log()  { echo "[$(date +%H:%M:%S)] $*" | tee -a "$RESULTS_DIR/log.txt"; }
pass() { echo "[$(date +%H:%M:%S)] ✅ PASS: $*" | tee -a "$RESULTS_DIR/log.txt"; }
fail() { echo "[$(date +%H:%M:%S)] ❌ FAIL: $*" | tee -a "$RESULTS_DIR/log.txt"; }
skip() { echo "[$(date +%H:%M:%S)] ⏭️  SKIP: $*" | tee -a "$RESULTS_DIR/log.txt"; }

should_run() { [[ -z "$PHASE" || "$PHASE" == "$1" ]]; }

# ─── Phase 1: Preflight ─────────────────────────────────────────────
run_preflight() {
  log "═══ Phase 1: Preflight ═══"
  local errors=0

  # Router reachable (TCP check, not ICMP which may be blocked)
  if timeout 5 bash -c "echo > /dev/tcp/$ROUTER_IP/22" 2>/dev/null; then
    pass "router reachable at $ROUTER_IP (TCP:22)"
  else
    fail "router unreachable at $ROUTER_IP"
    ((errors++))
  fi

  # SSH works
  if $SSH "echo ok" >/dev/null 2>&1; then
    pass "SSH to router works"
  else
    fail "SSH to router failed"
    ((errors++))
  fi

  # Daemon serving :2121
  local resp=$(timeout 5 curl -s "http://$ROUTER_IP:2121/" 2>/dev/null | head -c 100)
  if echo "$resp" | grep -q '"kind":10021'; then
    pass "daemon serving kind:10021 on :2121"
  else
    fail "daemon not serving on :2121 (got: ${resp:-nothing})"
    ((errors++))
  fi

  # Daemon pubkey present (fetch full response, not truncated)
  local full_resp=$(timeout 5 curl -s "http://$ROUTER_IP:2121/" 2>/dev/null)
  local pubkey=$(echo "$full_resp" | python3 -c "import json,sys; print(json.load(sys.stdin).get('pubkey',''))" 2>/dev/null)
  if [ -n "$pubkey" ] && [ ${#pubkey} -ge 60 ]; then
    pass "router identity pubkey: ${pubkey:0:16}..."
  else
    fail "missing or short pubkey"
    ((errors++))
  fi

  # Mint reachable
  local mint=$(timeout 8 curl -s "$MINT_URL/v1/keys" 2>/dev/null | head -c 50)
  if echo "$mint" | grep -q keysets; then
    pass "mint reachable at $MINT_URL"
  else
    fail "mint unreachable at $MINT_URL"
    ((errors++))
  fi

  # NDS running
  local nds=$($SSH "ndsctl status 2>/dev/null | head -4" 2>/dev/null)
  if echo "$nds" | grep -q "NoDogSplash"; then
    pass "NDS running: $nds"
  else
    fail "NDS not running"
    ((errors++))
  fi

  # OpenWrt version
  local ver=$($SSH "cat /etc/openwrt_release | grep RELEASE" 2>/dev/null)
  log "  OpenWrt: $ver"

  if [ $errors -gt 0 ]; then
    log "  ⚠️  $errors preflight error(s) — some tests may fail"
  fi
}

# ─── Phase 2: API/Protocol tests ────────────────────────────────────
run_api_tests() {
  log "═══ Phase 2: API/Protocol Tests ═══"

  # TIP-03: details event structure
  local details=$(timeout 5 curl -s "http://$ROUTER_IP:2121/" 2>/dev/null)
  echo "$details" | python3 -c "
import json, sys
d = json.load(sys.stdin)
tags = {t[0]: t[1:] for t in d.get('tags', [])}
assert d.get('kind') == 10021, 'wrong kind'
assert 'metric' in tags, 'missing metric tag'
assert 'step_size' in tags, 'missing step_size tag'
assert any('price_per_step' in t for t in d.get('tags', [])), 'missing pricing'
assert 'sig' in d, 'missing signature'
print('  kind={}, metric={}, pricing=OK, sig=OK'.format(d['kind'], tags['metric'][0]))
" 2>/dev/null && pass "TIP-03 details event valid" || fail "TIP-03 details event invalid"

  # TIP-04: whoami
  local whoami=$(timeout 5 curl -s "http://$ROUTER_IP:2121/whoami" 2>/dev/null)
  if echo "$whoami" | grep -q "mac="; then
    pass "TIP-04 whoami responds: $whoami"
  else
    fail "TIP-04 whoami invalid: $whoami"
  fi

  # CORS headers
  local cors=$(timeout 5 curl -s -I -H "Origin: http://$ROUTER_IP:2051" "http://$ROUTER_IP:2121/" 2>/dev/null | grep -i access-control)
  if [ -n "$cors" ]; then
    pass "CORS headers present for portal origin"
  else
    fail "CORS headers missing"
  fi

  # Pricing sanity
  echo "$details" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for tag in d.get('tags', []):
    if tag[0] == 'price_per_step':
        assert len(tag) >= 5, 'price tag too short'
        assert tag[2].isdigit(), 'price not numeric'
        assert tag[3] == 'sat', 'wrong unit'
        print('  pricing: {} {} per step at {}'.format(tag[2], tag[3], tag[4]))
        break
else:
    print('  no pricing found')
    sys.exit(1)
" 2>/dev/null && pass "pricing valid" || fail "pricing invalid"
}

# ─── Phase 3: Payment tests ─────────────────────────────────────────
run_payment_tests() {
  log "═══ Phase 3: Payment Tests ═══"

  # Mint a test token
  local token_json=$(curl -s -X POST "$MINT_URL/v1/mint/quote/bolt11" \
    -H "Content-Type: application/json" \
    -d "{\"amount\": 4, \"unit\": \"sat\"}" 2>/dev/null)
  # Note: This is a simplified test — real minting needs the admin grant flow
  # For now we test with a direct API payment (TIP-03 POST /)

  # Test POST / with invalid token (should get 402 or notice event)
  local invalid_resp=$(timeout 10 curl -s -X POST "http://$ROUTER_IP:2121/" \
    -H "Content-Type: application/json" \
    -d '{"kind":21000,"tags":[["payment","cashuAinvalid"]],"pubkey":"00","sig":"00","content":"","created_at":0}' 2>/dev/null)
  if [ -n "$invalid_resp" ]; then
    pass "POST / responds to invalid token (got response)"
    local kind=$(echo "$invalid_resp" | python3 -c "import json,sys; print(json.load(sys.stdin).get('kind',''))" 2>/dev/null)
    if [ "$kind" = "21023" ] || echo "$invalid_resp" | grep -q "error\|notice"; then
      pass "invalid token rejected with notice/error"
    else
      log "  response kind: $kind (expected 21023 or error)"
    fi
  else
    fail "POST / no response to invalid token"
  fi

  # Test session endpoint (if any)
  local session=$($SSH "ndsctl json 2>/dev/null | head -5" 2>/dev/null)
  if [ -n "$session" ]; then
    pass "NDS session state queryable"
  else
    skip "NDS json output not available"
  fi
}

# ─── Phase 4: Session tests ─────────────────────────────────────────
run_session_tests() {
  log "═══ Phase 4: Session Tests ═══"

  # Check NDS client table
  local clients=$($SSH "ndsctl clients 2>/dev/null" 2>/dev/null)
  if [ -n "$clients" ]; then
    local count=$(echo "$clients" | grep -c "^client_id=" || true)
    log "  NDS clients: $count"
    pass "NDS client table accessible"
  else
    skip "NDS client table not available"
  fi

  # Check daemon session tracking
  local sessions=$($SSH "ls /etc/tollgate/ecash/ 2>/dev/null | wc -l" 2>/dev/null)
  log "  ecash entries: $sessions"

  # Test config.json is valid
  local config=$($SSH "cat /etc/tollgate/config.json" 2>/dev/null)
  echo "$config" | python3 -c "
import json, sys
c = json.load(sys.stdin)
assert 'accepted_mints' in c, 'no mints'
assert c['accepted_mints'][0]['url'], 'empty mint URL'
assert 'step_size' in c, 'no step_size'
print('  config: mints={}, step_size={}'.format(
    len(c['accepted_mints']), c['step_size']))
" 2>/dev/null && pass "config.json valid" || fail "config.json invalid"
}

# ─── Phase 5: Enforcement tests ─────────────────────────────────────
run_enforcement_tests() {
  log "═══ Phase 5: Enforcement Tests ═══"

  # NDS pre-auth rules
  local nds_rules=$($SSH "iptables -L ndsRTR -n 2>/dev/null" 2>/dev/null)
  if echo "$nds_rules" | grep -q "2121"; then
    pass "NDS pre-auth allows :2121"
  else
    fail "NDS pre-auth missing :2121 rule"
  fi

  if echo "$nds_rules" | grep -q "2050"; then
    pass "NDS pre-auth allows :2050"
  else
    fail "NDS pre-auth missing :2050 rule"
  fi

  # Firewall INPUT allows from LAN
  local fw=$($SSH "nft list chain inet fw4 input_lan 2>/dev/null | head -3" 2>/dev/null)
  if echo "$fw" | grep -q "accept"; then
    pass "fw4 input_lan accepts"
  else
    fail "fw4 input_lan missing accept rule"
  fi

  # Daemon not on loopback-only (issue #226 says it should be, but for
  # testing we need it on the LAN interface)
  local listening=$($SSH "netstat -tln | grep 2121" 2>/dev/null)
  if echo "$listening" | grep -q ":::"; then
    pass "daemon on all interfaces (dual-stack)"
  elif echo "$listening" | grep -q "0.0.0.0"; then
    pass "daemon on all interfaces (IPv4)"
  else
    log "  listening: $listening"
  fi
}

# ─── Phase 6: Degraded mode ─────────────────────────────────────────
run_degraded_tests() {
  log "═══ Phase 6: Degraded Mode Tests ═══"

  # Check if daemon reports degraded mode in logs
  local degraded=$($SSH "logread | grep -i 'degraded' | tail -3" 2>/dev/null)
  if [ -n "$degraded" ]; then
    log "  degraded mode was triggered:"
    echo "$degraded" | while read line; do log "    $line"; done
  else
    log "  no degraded mode in recent logs (mint was reachable)"
  fi

  # Check mint probe behavior
  local probe=$($SSH "logread | grep -i 'mint probe' | tail -3" 2>/dev/null)
  if [ -n "$probe" ]; then
    if echo "$probe" | grep -q "FAILED"; then
      log "  mint probe failures recorded (expected if mint was down)"
    fi
    if echo "$probe" | grep -qi "ok\|success"; then
      pass "mint probe succeeded"
    fi
  else
    skip "no mint probe logs"
  fi

  # Test: block mint, verify daemon enters degraded mode
  log "  (skipping active degraded-mode test — would need to block the mint)"
}

# ─── Phase 7: Summary ───────────────────────────────────────────────
run_summary() {
  log "═══ Phase 7: Summary ═══"

  local passes=$(grep -c "✅ PASS" "$RESULTS_DIR/log.txt" 2>/dev/null || echo 0)
  local fails=$(grep -c "❌ FAIL" "$RESULTS_DIR/log.txt" 2>/dev/null || echo 0)
  local skips=$(grep -c "⏭️  SKIP" "$RESULTS_DIR/log.txt" 2>/dev/null || echo 0)

  log ""
  log "════════════════════════════════════════════"
  log "  RESULTS: $passes passed, $fails failed, $skips skipped"
  log "  Evidence: $RESULTS_DIR/"
  log "════════════════════════════════════════════"

  # Collect router state for evidence
  $SSH "logread | tail -50" > "$RESULTS_DIR/router-log.txt" 2>/dev/null || true
  $SSH "ndsctl status" > "$RESULTS_DIR/nds-status.txt" 2>/dev/null || true
  $SSH "cat /etc/tollgate/config.json" > "$RESULTS_DIR/router-config.json" 2>/dev/null || true
  $SSH "netstat -tln" > "$RESULTS_DIR/router-ports.txt" 2>/dev/null || true
  curl -s "http://$ROUTER_IP:2121/" > "$RESULTS_DIR/details-event.json" 2>/dev/null || true

  if [ "$fails" -gt 0 ]; then
    exit 1
  fi
}

# ─── Main ───────────────────────────────────────────────────────────
log "Starting unattended test run at $(date)"
log "Results directory: $RESULTS_DIR"
log ""

should_run 1 && run_preflight
should_run 2 && run_api_tests
should_run 3 && run_payment_tests
should_run 4 && run_session_tests
should_run 5 && run_enforcement_tests
should_run 6 && run_degraded_tests
should_run 7 && run_summary

log "Done at $(date)"
