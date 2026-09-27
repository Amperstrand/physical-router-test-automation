#!/usr/bin/env bash
# Phase A: Real Payment E2E — issue #108 scenario 2
# Netns client (real L3/L2 separation) -> NDS intercept -> pay 4-sat signet token
# -> kind-1022 session, NDS Authenticated, http+dns flow through the gate,
# receipt evidence -> double-spend rejected.
EV=/tmp/phase-a
export EV
source /tmp/tg-lib.sh

bash /tmp/lab-preflight.sh > "$EV/preflight.txt" 2>&1 || { log "preflight failed — see $EV/preflight.txt"; exit 2; }
log "preflight clean"
ensure_full_mode && ok "daemon in full mode (details serving)" || { bad "daemon not healthy"; finish; exit 1; }
lab_shim

C1MAC=02:11:22:33:44:51
# clean slate: daemon restart clears in-memory sessions/gate timers (allotment
# stacking from prior runs would otherwise mask the fresh-auth path — see the
# external-deauth finding in the phase evidence)
$RSSH "/etc/init.d/tollgate-wrt restart" >/dev/null 2>&1; sleep 5
$RSSH "ndsctl deauth $C1MAC" >/dev/null 2>&1 || true
ensure_client 1 51 $C1MAC >/dev/null
sudo ip netns del tgclient1 2>/dev/null; ensure_client 1 51 $C1MAC >/dev/null   # fresh client = clean NDS state
log "netns client tgclient1 up (10.99.99.51, $C1MAC)"

# ── 1. Pre-auth: NDS intercepts client traffic ──────────────────────
PC=$(cexec 1 timeout 6 curl -s -o "$EV/preauth-probe.body" -w "%{http_code}" "http://198.51.100.7/" 2>/dev/null || true)
grep -q "10.99.99.1:2050" "$EV/preauth-probe.body" 2>/dev/null && ok "NDS intercepts pre-auth traffic (http $PC -> splash)" || bad "no splash redirect pre-auth (http $PC)"
ST=$(nds_state "$C1MAC"); [ -n "$ST" ] && ok "client registered in NDS (state: $ST)" || bad "client not in NDS"

# ── 2. Whoami from the client perspective ──────────────────────────
W=$(cexec 1 curl -s -m 5 "http://$R:2121/whoami" 2>/dev/null)
[ "$W" = "mac=$C1MAC" ] && ok "whoami returns client MAC" || bad "whoami mismatch: '$W'"

# ── 3. Pay with a real 4-sat token ──────────────────────────────────
log_snap
pick_token 4 >> "$EV/log.txt" 2>&1 && ok "token selected + kind-21000 event built" || bad "token selection failed"
CODE=$(pay "$C1MAC" "$EV/payment-response.json")
log "payment POST http_code=$CODE"
python3 - "$EV/payment-response.json" "$CODE" <<'EOF' && ok "payment accepted: kind-1022 session event (HTTP $CODE)" || bad "payment NOT accepted (http=$CODE, see payment-response.json + TRIAGE.txt)"
import json,sys
d=json.load(open(sys.argv[1])); code=int(sys.argv[2])
assert code==200, f"http {code}: {d.get('content','')[:150]}"
assert d.get("kind")==1022, f"kind {d.get('kind')} != 1022: {d.get('content','')[:150]}"
tags={t[0]:t[1:] for t in d.get("tags",[])}
print("  session:", {k:v for k,v in tags.items() if k in ("allotment","metric","start-time")})
assert tags.get("metric")==["milliseconds"] and int(tags["allotment"][0])>0
EOF
log_new > "$EV/payment-daemon-logs.txt"
grep -q "Amount after swap: 4" "$EV/payment-daemon-logs.txt" && ok "daemon Receive+swap of 4 sats (payment-daemon-logs.txt)" || bad "no swap confirmation in fresh logs"
grep -q "Authorization successful" "$EV/payment-daemon-logs.txt" && ok "valve authorized MAC via ndsctl (fresh log line)" || { log "  (no fresh auth line; NDS state below is authoritative)"; }

# ── 4. NDS authenticated + traffic flows through the gate ──────────
sleep 2
ST=$(nds_state "$C1MAC")
[ "$ST" = "Authenticated" ] && ok "NDS client state=Authenticated" || bad "NDS state: '$ST' (expected Authenticated)"
HC=$(cexec 1 curl -s -m 10 -o "$EV/internet-probe.body" -w "%{http_code}" "http://1.1.1.1/" 2>/dev/null || true)
{ [ "$HC" = 200 ] || [ "$HC" = 301 ] || [ "$HC" = 302 ]; } && ok "http through gate ($HC from 1.1.1.1 via router)" || bad "http through gate failed (code=$HC)"
DNS=$(cexec 1 timeout 10 dig +time=4 +tries=1 @8.8.8.8 example.com A +short 2>/dev/null | grep -cE "^[0-9.]+$" || true)
[ "${DNS:-0}" -ge 1 ] && ok "dns through gate (8.8.8.8 -> A records)" || bad "dns through gate failed"
# while authed: portal-relevant endpoints reachable pre-auth anyway
DR=$(curl -s -m 5 "http://$R:2121/" | head -c 20)
echo "$DR" | grep -q '"kind":10021' && ok "details event still serving during session" || bad "details event down during session"

# ── 5. Receipt evidence on router ───────────────────────────────────
$RSSH "ls -la /etc/tollgate/ecash/; ls -l /etc/tollgate/wallet.db" > "$EV/wallet-evidence.txt" 2>/dev/null
log "wallet evidence: $(tail -1 "$EV/wallet-evidence.txt" 2>/dev/null)"

# ── 6. Double-spend rejection (retry rides out mint latency blips) ──
SPENT=1
for attempt in 1 2 3; do
  CODE2=$(pay "$C1MAC" "$EV/double-spend-response.json")
  log "double-spend attempt $attempt http_code=$CODE2"
  if python3 -c "import json;d=json.load(open('$EV/double-spend-response.json'));exit(0 if d.get('kind')==21023 and 'spent' in json.dumps(d).lower() else 1)" 2>/dev/null; then SPENT=0; break; fi
  [ $attempt -lt 3 ] && sleep 15
done
[ $SPENT -eq 0 ] && ok "double-spend rejected (kind-21023 token-spent)" || bad "double-spend not definitively rejected (see double-spend-response.json)"
$RSSH "ndsctl json; logread | tail -40" > "$EV/final-state.txt" 2>/dev/null || true

finish
