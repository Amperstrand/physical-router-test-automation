#!/usr/bin/env bash
# Phase D: Degraded Mode (issue #110, scenario 1)
# Fresh daemon (clean tracker) -> block mint via /etc/hosts -> probe-path
# downgrade -> degraded behavior checks -> unblock -> observe recovery
# (expected: STUCK — runtime-downgrade path never re-registers the
# first-reachable callback; code-confirmed) -> restart recovers -> same
# token redeems (no stranded funds).
EV=/tmp/phase-d
export EV
source /tmp/tg-lib.sh
C1MAC=02:11:22:33:44:51

bash /tmp/lab-preflight.sh > "$EV/preflight.txt" 2>&1 || { log "preflight failed"; exit 2; }
lab_shim

unblock(){ $RSSH "sed -i '/signut.cashu.exchange/d' /etc/hosts" >/dev/null 2>&1; }

# ── 0. Fresh daemon: clean health-tracker state (a prior payment failure
#       zeroes reachableCount without firing the callback, suppressing the
#       probe-path transition — recorded as finding D-1)
$RSSH "/etc/init.d/tollgate-wrt restart" >/dev/null 2>&1; sleep 7
PID0=$($RSSH "ps | grep tollg | grep -v grep | awk '{print \$1}'" 2>/dev/null | tr -d '\r')
log "fresh daemon pid: $PID0"
curl -s -m 5 "http://$R:2121/" | grep -q '"kind":10021' && ok "daemon full mode at start" || { bad "daemon not serving at start"; finish; exit 1; }
ensure_client 1 51 $C1MAC >/dev/null
cexec 1 timeout 6 curl -s -o /dev/null "http://198.51.100.7/" 2>/dev/null || true
pick_token 1 >>"$EV/log.txt" 2>&1 || { bad "no token"; finish; exit 1; }
log_snap

# ── 1. Block the mint ───────────────────────────────────────────────
$RSSH "echo '127.0.0.1 signut.cashu.exchange' >> /etc/hosts" >/dev/null 2>&1 && log "mint blackholed (/etc/hosts -> 127.0.0.1)"

# ── 2. Probe-path downgrade (<=6 min) ───────────────────────────────
log "waiting for periodic mint probe to fail (<=6 min)..."
DEG=1
for _ in $(seq 1 72); do
  $RSSH "logread | tail -50" > "$EV/logcheck.txt" 2>/dev/null
  grep -q "downgrading to degraded mode" "$EV/logcheck.txt" && { DEG=0; break; }
  sleep 5
done
if [ $DEG -eq 0 ]; then
  ok "downgrade observed: 'All mints unreachable — downgrading to degraded mode'"
  grep -E "mint probe FAILED|downgrading|offline wallet" "$EV/logcheck.txt" | tail -4 > "$EV/downgrade-evidence.txt"
  log "evidence: $(sed -n 2p "$EV/downgrade-evidence.txt" | sed 's/.*tollgate-wrt[^ ]* //' | head -c 120)"
else
  bad "no probe-path downgrade within 6 min"
fi

# ── 3. Degraded-state behavior ──────────────────────────────────────
DET=$(curl -s -m 8 "http://$R:2121/"); echo "$DET" > "$EV/details-degraded.json"
echo "$DET" | grep -q '"kind":10021' && ok "details event serves in degraded mode" || bad "details event down in degraded mode"
echo "$DET" | grep -q "signut" && ok "cached pricing still advertised in degraded mode" || log "  pricing tag contents changed (see details-degraded.json)"
CODE=$(pay "$C1MAC" "$EV/outage-payment.json")
K=$(ev_kind "$EV/outage-payment.json")
[ "$K" = "21023" ] && ok "payment in degraded state: graceful kind-21023 (token unspent)" || log "  outage payment kind=$K http=$CODE (see outage-payment.json)"
python3 -c "
import json; d=json.load(open('$EV/outage-payment.json'))
print('  degraded notice:', [t[1] for t in d['tags'] if t[0]=='code'], '|', d.get('content','')[:110])" 2>/dev/null || true
PID1=$($RSSH "ps | grep tollg | grep -v grep | awk '{print \$1}'" 2>/dev/null | tr -d '\r')
[ "$PID0" = "$PID1" ] && ok "daemon stable in degraded mode (pid $PID0)" || bad "daemon restarted in degraded mode ($PID0 -> $PID1)"

# ── 4. Unblock; observe recovery behavior ───────────────────────────
unblock; log "mint unblocked — observing recovery (aggressive retry is startup-only; recovery rides the 5-min probe)"
REC=0; NOPROBE=0
for _ in $(seq 1 96); do
  $RSSH "logread | tail -60" > "$EV/logcheck.txt" 2>/dev/null
  grep -q "Upgrading from degraded to full merchant" "$EV/logcheck.txt" && { REC=1; break; }
  grep -q "Mint became reachable — attempting to upgrade" "$EV/logcheck.txt" && { REC=1; break; }
  if ! grep -q "mint probe: .*ok=true" "$EV/logcheck.txt"; then NOPROBE=$((NOPROBE+1)); fi
  sleep 5
done
if [ $REC -eq 1 ]; then
  ok "auto-recovery observed ('Upgrading from degraded to full merchant')"
else
  log "FINDING D-2 (expected, code-confirmed): no auto-recovery within 8 min after unblock —"
  log "  runtime-downgraded merchant never re-registers SetOnFirstReachableForDegraded"
  log "  (both call sites are startup/wallet-init paths: merchant.go:85,132) -> STUCK until restart."
  $RSSH "logread | grep -E 'mint probe' | tail -3" > "$EV/recovery-window-probes.txt" 2>/dev/null
  grep -q "ok=true" "$EV/recovery-window-probes.txt" && log "  (mint probes DID succeed post-unblock — merchant just never upgraded)" || log "  (no ok probes captured)"
fi

# ── 5. Recovery via restart + same-token redemption ─────────────────
$RSSH "/etc/init.d/tollgate-wrt restart" >/dev/null 2>&1; sleep 7
curl -s -m 5 "http://$R:2121/" | grep -q '"kind":10021' && ok "restart restores full mode (mint reachable)" || bad "restart did not restore service"
CODE3=$(pay "$C1MAC" "$EV/recovery-payment.json")
K3=$(ev_kind "$EV/recovery-payment.json"); log "recovery payment: http=$CODE3 kind=$K3"
[ "$K3" = "1022" ] && ok "SAME token redeemed after recovery — no stranded funds" || bad "token not redeemed post-recovery (kind=$K3)"
sleep 2
ST=$(nds_state "$C1MAC")
[ "$ST" = "Authenticated" ] && ok "client authenticated after recovery payment" || log "  nds state after recovery payment: '$ST'"

# ── 6. Panic/crash check ────────────────────────────────────────────
$RSSH "logread | grep -iE 'panic|fatal' | tail -3" > "$EV/panic-check.txt" 2>/dev/null
[ -s "$EV/panic-check.txt" ] && bad "panic/fatal lines (panic-check.txt)" || ok "no panic/fatal in logs"
$RSSH "ndsctl deauth $C1MAC" >/dev/null 2>&1 || true
log_new > "$EV/phase-d-daemon-logs.txt"

finish
