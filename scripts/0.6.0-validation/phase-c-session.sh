#!/usr/bin/env bash
# Phase C: Session Lifecycle (issue #108 scenario 3)
# step_size=10s -> pay -> Authenticated -> expiry -> deauth -> blocked ->
# re-pay accepted (re-portal flow) -> restore step_size=30000.
EV=/tmp/phase-c
export EV
source /tmp/tg-lib.sh
C1MAC=02:11:22:33:44:51

bash /tmp/lab-preflight.sh > "$EV/preflight.txt" 2>&1 || { log "preflight failed"; exit 2; }
lab_shim
$RSSH "cp /etc/tollgate/config.json /tmp/config.json.phasec-backup" && log "config backed up"

restore(){
  $RSSH "cp /tmp/config.json.phasec-backup /etc/tollgate/config.json && /etc/init.d/tollgate-wrt restart" >/dev/null 2>&1
  sleep 6
  curl -s -m 5 "http://$R:2121/" | grep -q '"step_size", *"30000"' && log "step_size restored to 30000" || bad "step_size restore failed"
  $RSSH "ndsctl deauth $C1MAC" >/dev/null 2>&1 || true
}

# ── 1. Short sessions: step_size 10s ────────────────────────────────
$RSSH "sed -i 's/\"step_size\": 30000/\"step_size\": 10000/' /etc/tollgate/config.json && /etc/init.d/tollgate-wrt restart" >/dev/null 2>&1
sleep 7
DET=$(curl -s -m 8 "http://$R:2121/"); echo "$DET" > "$EV/details-10s.json"
echo "$DET" | grep -q '"step_size", *"10000"' && ok "step_size=10000 advertised (10s sessions)" || { bad "step_size not applied"; restore; finish; exit 1; }

# ── 2. Fresh client + register pre-auth ─────────────────────────────
ensure_client 1 51 $C1MAC >/dev/null
cexec 1 timeout 6 curl -s -o "$EV/preauth.body" "http://198.51.100.7/" 2>/dev/null || true
ST=$(nds_state "$C1MAC"); [ -n "$ST" ] && log "client in NDS pre-payment (state: $ST)" || bad "client not registered"

# ── 3. Pay for a short session (1 sat = 1 step = 10s) ───────────────
log_snap
pick_token 1 >>"$EV/log.txt" 2>&1 && ok "1-sat token staged" || bad "token staging failed"
CODE=$(pay "$C1MAC" "$EV/payment-1.json")
K=$(ev_kind "$EV/payment-1.json"); log "payment 1: http=$CODE kind=$K"
python3 - "$EV/payment-1.json" <<'EOF' && ok "short session granted (allotment 10000ms)" || bad "unexpected session response"
import json,sys
d=json.load(open(sys.argv[1])); tags={t[0]:t[1:] for t in d["tags"]}
assert d.get("kind")==1022
assert tags["allotment"]==["10000"], tags.get("allotment")
print("  allotment:", tags["allotment"][0], "ms")
EOF

# ── 4. Authenticated ────────────────────────────────────────────────
sleep 2
ST=$(nds_state "$C1MAC")
[ "$ST" = "Authenticated" ] && ok "NDS Authenticated during session" || bad "not Authenticated during session ('$ST')"
HC=$(cexec 1 curl -s -m 8 -o /dev/null -w "%{http_code}" "http://1.1.1.1/" 2>/dev/null || true)
{ [ "$HC" = 200 ] || [ "$HC" = 301 ] || [ "$HC" = 302 ]; } && ok "traffic flows mid-session (http $HC)" || bad "traffic blocked mid-session ($HC)"

# ── 5. Wait for expiry (10s allotment + margin) ─────────────────────
log "waiting 18s for session expiry (allotment 10s)..."
sleep 18
ST=$(nds_state "$C1MAC")
[ "$ST" != "Authenticated" ] && ok "session expired: NDS state='$ST' (deauth fired)" || bad "still Authenticated after expiry"
log_new > "$EV/expiry-daemon-logs.txt"
grep -qi "error deauthorizing" "$EV/expiry-daemon-logs.txt" && bad "deauth error in logs" || ok "no deauth errors (valve timer clean)"
HC=$(cexec 1 curl -s -m 8 -o "$EV/postexpiry.body" -w "%{http_code}" "http://1.1.1.1/" 2>/dev/null || true)
grep -q "10.99.99.1:2050" "$EV/postexpiry.body" 2>/dev/null && ok "post-expiry traffic re-intercepted to splash (re-portal works)" || log "  post-expiry http=$HC (interception may show as refused; NDS state is authoritative)"

# ── 6. Daemon accepts new payment after expiry (re-portal flow) ─────
pick_token 1 >>"$EV/log.txt" 2>&1 && ok "second 1-sat token staged" || bad "token staging 2 failed"
CODE=$(pay "$C1MAC" "$EV/payment-2.json")
K=$(ev_kind "$EV/payment-2.json"); log "payment 2: http=$CODE kind=$K"
[ "$K" = "1022" ] && ok "re-payment accepted after expiry (kind-1022)" || bad "re-payment rejected after expiry (kind=$K)"
sleep 2
ST=$(nds_state "$C1MAC")
[ "$ST" = "Authenticated" ] && ok "re-authenticated after re-payment" || bad "not re-authenticated ('$ST')"

# ── 7. Restore ──────────────────────────────────────────────────────
restore
$RSSH "ndsctl json; logread | tail -50" > "$EV/final-state.txt" 2>/dev/null || true
finish
