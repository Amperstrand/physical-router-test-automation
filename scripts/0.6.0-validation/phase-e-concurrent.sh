#!/usr/bin/env bash
# Phase E: Concurrent Sessions (issue #108, extended)
# Two isolated netns clients pay near-simultaneously; sessions must be
# independent: both authed, one expiring must not affect the other.
EV=/tmp/phase-e
export EV
source /tmp/tg-lib.sh
C1MAC=02:11:22:33:44:51
C2MAC=02:11:22:33:44:52

bash /tmp/lab-preflight.sh > "$EV/preflight.txt" 2>&1 || { log "preflight failed"; exit 2; }
lab_shim
# clean session table (cumulative AddAllotment from prior phases would skew windows)
$RSSH "/etc/init.d/tollgate-wrt restart" >/dev/null 2>&1; sleep 7
curl -s -m 5 "http://$R:2121/" | grep -q '"kind":10021' && ok "fresh daemon (clean session table)" || { bad "daemon not serving after restart"; finish; exit 1; }

# ── 1. Two isolated clients on the LAN ──────────────────────────────
ensure_client 1 51 $C1MAC >/dev/null
ensure_client 2 52 $C2MAC >/dev/null
log "clients: tgclient1 (10.99.99.51/$C1MAC) + tgclient2 (10.99.99.52/$C2MAC)"
$RSSH "ndsctl deauth $C1MAC; ndsctl deauth $C2MAC" >/dev/null 2>&1 || true
cexec 1 timeout 6 curl -s -o "$EV/c1-preauth.body" "http://198.51.100.7/" 2>/dev/null || true
cexec 2 timeout 6 curl -s -o "$EV/c2-preauth.body" "http://198.51.100.7/" 2>/dev/null || true
S1=$(nds_state "$C1MAC"); S2=$(nds_state "$C2MAC")
{ [ -n "$S1" ] && [ -n "$S2" ]; } && ok "both clients registered in NDS ('$S1' / '$S2')" || bad "client registration incomplete ('$S1'/'$S2')"

# ── 2. Near-simultaneous payments (parallel POSTs) ──────────────────
# c1: 1-sat = 30s session; c2: 4-sat = 120s session
pick_token 1 >>"$EV/log.txt" 2>&1 && cp /tmp/phase-a/token-in-play.txt "$EV/tok-c1.txt" && pick_token 4 >>"$EV/log.txt" 2>&1 && cp /tmp/phase-a/token-in-play.txt "$EV/tok-c2.txt" && ok "tokens staged (c1: 1-sat/30s, c2: 4-sat/120s)" || bad "token staging failed"
python3 - "$EV" <<'EOF'
import json,time
def ev(tokfile):
    tok=open(tokfile).read().strip()
    return json.dumps({"kind":21000,"pubkey":"00"*32,"created_at":int(time.time()),"tags":[["payment",tok]],"content":"","sig":"00"*64})
import sys; d=sys.argv[1]
open(d+"/event-c1.json","w").write(ev(d+"/tok-c1.txt"))
open(d+"/event-c2.json","w").write(ev(d+"/tok-c2.txt"))
EOF
log_snap
( curl -s -m 45 -o "$EV/pay-c1.json" -w "%{http_code}" -X POST "http://$R:2121/?mac=$C1MAC" -H "Content-Type: application/json" --data @"$EV/event-c1.json" > "$EV/code-c1.txt" ) &
( curl -s -m 45 -o "$EV/pay-c2.json" -w "%{http_code}" -X POST "http://$R:2121/?mac=$C2MAC" -H "Content-Type: application/json" --data @"$EV/event-c2.json" > "$EV/code-c2.txt" ) &
wait
K1=$(ev_kind "$EV/pay-c1.json"); K2=$(ev_kind "$EV/pay-c2.json")
log "parallel payments: c1 http=$(cat "$EV/code-c1.txt") kind=$K1 | c2 http=$(cat "$EV/code-c2.txt") kind=$K2"
[ "$K1" = "1022" ] && [ "$K2" = "1022" ] && ok "both concurrent payments accepted (kind-1022)" || bad "concurrent payment failure (kinds: $K1/$K2)"
python3 - <<'EOF' && ok "independent allotments (c1=30000ms, c2=120000ms)" || bad "unexpected allotments"
import json
a1={t[0]:t[1:] for t in json.load(open("/tmp/phase-e/pay-c1.json"))["tags"]}["allotment"][0]
a2={t[0]:t[1:] for t in json.load(open("/tmp/phase-e/pay-c2.json"))["tags"]}["allotment"][0]
print(f"  c1 allotment={a1}ms  c2 allotment={a2}ms")
assert a1=="30000" and a2=="120000", (a1,a2)
EOF

# ── 3. Both authenticated, tracked separately ───────────────────────
sleep 3
S1=$(nds_state "$C1MAC"); S2=$(nds_state "$C2MAC")
[ "$S1" = "Authenticated" ] && [ "$S2" = "Authenticated" ] && ok "NDS tracks both clients Authenticated" || bad "NDS states: '$S1'/'$S2'"
$RSSH "ndsctl json" > "$EV/nds-both.json" 2>/dev/null
python3 - "$EV/nds-both.json" <<'EOF' && ok "two distinct sessions in NDS client table" || bad "client table incomplete"
import json,sys
c=json.load(open(sys.argv[1]))["clients"]
m1="02:11:22:33:44:51"; m2="02:11:22:33:44:52"
assert m1 in c and m2 in c, list(c)
assert c[m1]["ip"]!=c[m2]["ip"], (c[m1]["ip"],c[m2]["ip"])
print("  ", m1, c[m1]["ip"], c[m1]["state"], "|", m2, c[m2]["ip"], c[m2]["state"])
EOF
H2=$(cexec 2 curl -s -m 8 -o /dev/null -w "%{http_code}" "http://1.1.1.1/" 2>/dev/null || true)
H1=$(cexec 1 curl -s -m 8 -o /dev/null -w "%{http_code}" "http://1.1.1.1/" 2>/dev/null || true)
{ [ "$H1" = 301 ] || [ "$H1" = 200 ]; } && { [ "$H2" = 301 ] || [ "$H2" = 200 ]; } && ok "both clients pass traffic mid-session (http $H1/$H2)" || bad "traffic mid-session: c1=$H1 c2=$H2"

# ── 4. Independence: c1 expires (30s), c2 must survive (120s) ───────
log "waiting 45s: c1 session (30s) should expire; c2 (120s) should survive..."
sleep 45
S1=$(nds_state "$C1MAC"); S2=$(nds_state "$C2MAC")
[ "$S1" != "Authenticated" ] && ok "c1 expired as scheduled (state='$S1')" || bad "c1 still Authenticated after its window"
[ "$S2" = "Authenticated" ] && ok "c2 UNAFFECTED by c1 expiry (still Authenticated)" || bad "c2 lost auth when c1 expired ('$S2')"
H2=$(cexec 2 curl -s -m 8 -o /dev/null -w "%{http_code}" "http://1.1.1.1/" 2>/dev/null || true)
{ [ "$H2" = 301 ] || [ "$H2" = 200 ]; } && ok "c2 traffic still flows after c1 expiry (http $H2)" || bad "c2 traffic broken after c1 expiry ($H2)"
log_new > "$EV/phase-e-daemon-logs.txt"
grep -q "Amount after swap: 1" "$EV/phase-e-daemon-logs.txt" && grep -q "Amount after swap: 4" "$EV/phase-e-daemon-logs.txt" && ok "daemon processed both receives independently (swap logs)" || log "  (swap log lines not both captured; payment responses are authoritative)"

# ── 5. Cleanup ──────────────────────────────────────────────────────
$RSSH "ndsctl deauth $C1MAC; ndsctl deauth $C2MAC" >/dev/null 2>&1 || true
$RSSH "ndsctl json" > "$EV/final-state.txt" 2>/dev/null || true
finish
