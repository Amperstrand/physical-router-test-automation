#!/usr/bin/env bash
# Phase B: CDK mint compatibility (new issue — created+closed with evidence)
# Switch daemon to shadow-cdk.cashu.exchange (CDK 0.18.0-rc.0), verify:
# details event serves CDK pricing, NUT surface vs signut, foreign-token
# (signut) rejection is graceful, switch back to signut.
EV=/tmp/phase-b
export EV
source /tmp/tg-lib.sh
CDK="https://shadow-cdk.cashu.exchange"

$RSSH "cp /etc/tollgate/config.json /tmp/config.json.phaseb-backup" && log "config backed up on router"

restore(){ # rollback on failure (campaign rule 5)
  $RSSH "cp /tmp/config.json.phaseb-backup /etc/tollgate/config.json && /etc/init.d/tollgate-wrt restart" >/dev/null 2>&1
  sleep 5
  curl -s -m 5 "http://$R:2121/" | grep -q "signut" && log "restored signut config" || bad "restore to signut failed"
}

# ── 1. Switch to CDK ────────────────────────────────────────────────
$RSSH "sed -i 's|https://signut.cashu.exchange|$CDK|' /etc/tollgate/config.json && /etc/init.d/tollgate-wrt restart" >/dev/null 2>&1
sleep 7
DET=$(curl -s -m 8 "http://$R:2121/")
echo "$DET" > "$EV/details-cdk.json"
echo "$DET" | grep -q '"kind":10021' && ok "details event serves with CDK mint configured" || { bad "details event broken after CDK switch"; restore; finish; exit 1; }
echo "$DET" | grep -q "shadow-cdk" && ok "price_per_step now lists CDK mint" || bad "details event does not list CDK mint"
python3 - <<'EOF' && ok "CDK advertisement parses (metric/step/pricing tags)" || bad "CDK advertisement malformed"
import json
d=json.load(open("/tmp/phase-b/details-cdk.json"))
tags={t[0]:t[1:] for t in d["tags"]}
assert tags["metric"]==["milliseconds"] and tags["step_size"]==["30000"]
ppt=[t for t in d["tags"] if t[0]=="price_per_step"][0]
assert ppt[3]=="sat" and ppt[4]=="https://shadow-cdk.cashu.exchange", ppt
print("  pricing:", ppt[2], ppt[3], "per 30s step @", ppt[4])
EOF

# ── 2. Daemon health with CDK (probe ok, not degraded) ─────────────
sleep 3
$RSSH "logread | tail -30" 2>/dev/null | grep -q "Merchant ready" && ok "daemon reached full mode with CDK mint" || log "  (Merchant ready line not in last 30 lines; checking mint probe)"
$RSSH "logread | grep 'mint probe' | tail -2" > "$EV/cdk-probe-logs.txt" 2>/dev/null
grep -q "shadow-cdk.*ok=true" "$EV/cdk-probe-logs.txt" && ok "mint probe against CDK ok=true" || bad "no successful CDK mint probe logged"

# ── 3. NUT surface comparison ───────────────────────────────────────
for m in signut.cashu.exchange shadow-cdk.cashu.exchange; do
  curl -s -m 10 "https://$m/v1/info" > "$EV/info-$m.json"
  curl -s -m 10 "https://$m/v1/keys" > "$EV/keys-$m.json"
done
python3 - <<'EOF' && ok "NUT surface compared (see nut-comparison.txt)" || bad "NUT comparison failed"
import json
rows=[]
for m in ("signut.cashu.exchange","shadow-cdk.cashu.exchange"):
    d=json.load(open(f"/tmp/phase-b/info-{m}.json"))
    nuts=d.get("nuts",{})
    rows.append((m, d.get("version"), sorted(nuts.keys()), {k:(nuts[k].get("disabled") is False) for k in nuts}))
    ks=json.load(open(f"/tmp/phase-b/keys-{m}.json"))
    rows[-1]=(rows[-1][0],rows[-1][1],rows[-1][2],rows[-1][3],len(ks.get("keysets",[])))
with open("/tmp/phase-b/nut-comparison.txt","w") as f:
    f.write(f"{'mint':32} {'version':22} nuts keysets\n")
    for m,v,nuts,_,ksc in rows:
        f.write(f"{m:32} {str(v):22} {','.join(nuts):20} {ksc}\n")
    only_cdk=set(rows[1][2])-set(rows[0][2]); only_nut=set(rows[0][2])-set(rows[1][2])
    f.write(f"\nCDK-only NUTs: {sorted(only_cdk)}\nNutshell-only NUTs: {sorted(only_nut)}\n")
print(open("/tmp/phase-b/nut-comparison.txt").read())
EOF

# ── 4. Foreign-token (signut) rejection under CDK config ───────────
pick_token 1 >>"$EV/log.txt" 2>&1 && log "signut 1-sat token staged for rejection test" || bad "no token for rejection test"
CODE=$(pay "$CLIENT_MAC" "$EV/foreign-token-response.json")
log "foreign-token POST http_code=$CODE"
python3 - "$EV/foreign-token-response.json" "$CODE" <<'EOF' && ok "foreign (signut) token rejected gracefully: kind-21023, no crash" || bad "foreign token handling not graceful (see foreign-token-response.json)"
import json,sys
d=json.load(open(sys.argv[1])); code=int(sys.argv[2])
assert code in (400, 200), f"http {code}"
assert d.get("kind")==21023, f"kind {d.get('kind')} != 21023"
body=json.dumps(d).lower()
assert any(s in body for s in ("mint","trust","error")), body[:200]
print("  notice:", [t for t in d["tags"] if t[0]=="code"], "|", d.get("content","")[:120])
EOF
sleep 2
curl -s -m 5 "http://$R:2121/" | grep -q '"kind":10021' && ok "daemon alive + serving after foreign-token rejection" || bad "daemon unhealthy after foreign token"
PID=$($RSSH "ps | grep tollg | grep -v grep | awk '{print \$1}'" 2>/dev/null | tr -d '\r')
log "daemon pid after rejection: $PID"

# ── 5. Restore signut ───────────────────────────────────────────────
restore
DET=$(curl -s -m 8 "http://$R:2121/")
echo "$DET" > "$EV/details-restored.json"
echo "$DET" | grep -q "signut" && ok "config restored: signut pricing serves again" || bad "signut restore incomplete"
$RSSH "ndsctl json; logread | tail -30" > "$EV/final-state.txt" 2>/dev/null || true

finish
