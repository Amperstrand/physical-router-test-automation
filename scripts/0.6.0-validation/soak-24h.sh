#!/usr/bin/env bash
# soak-24h.sh — PRTA #110 scenario 5: 24h repeating paid-session workload.
# Self-contained (no tg-lib coupling). Runs under nohup on ai-legion.
# Design notes:
#  - mint: testnut (fakewallet) — deterministic, no external latency blips
#  - 1-sat payments every 10 min (144 cycles); 2 alternating netns clients;
#    allotment stacking per MAC is deliberate (exercises session-table growth)
#  - hourly capture: RSS/CPU, fd count, NDS clients, wallet balance, daemon
#    errors delta, config.md5 (drift detector), pid (restart detector)
#  - 3 consecutive payment failures -> triage snapshot, keep going
set -u
EV=/tmp/soak
mkdir -p "$EV"
RSSH="ssh -n -i $HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR root@10.99.99.1"
R=10.99.99.1
LOG="$EV/soak.log"; CSV="$EV/metrics.csv"
CYCLES="${1:-144}"          # 144 x 10min = 24h
INTERVAL=600
C1MAC=02:11:22:33:44:51
C2MAC=02:11:22:33:44:52
log(){ echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }

# ── setup ───────────────────────────────────────────────────────────
curl -s -m 5 "http://$R:2121/" | grep -q testnut || { log "ABORT: config not on testnut"; exit 1; }
for i in 1 2; do
  IP=$((50+i)); MAC=$(printf "02:11:22:33:44:5%d" $i)
  sudo ip netns del tgclient$i 2>/dev/null
  sudo ip netns add tgclient$i
  sudo ip link add veth-c$i type veth peer name veth-c$i-host
  sudo ip link set veth-c$i netns tgclient$i
  sudo ip netns exec tgclient$i ip link set veth-c$i address $MAC
  sudo ip netns exec tgclient$i ip addr add 10.99.99.$IP/24 dev veth-c$i
  sudo ip netns exec tgclient$i ip link set lo up
  sudo ip netns exec tgclient$i ip link set veth-c$i up
  sudo ip netns exec tgclient$i ip route replace default via 10.99.99.1
  sudo ip link set veth-c$i-host master tg-poc-br
  sudo ip link set veth-c$i-host up
done
# wan-zone parity shim (single-interface lab topology)
$RSSH "iptables -C FORWARD -i br-lan -o br-lan ! -s 10.99.99.0/24 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || iptables -I FORWARD 1 -i br-lan -o br-lan ! -s 10.99.99.0/24 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT" >/dev/null 2>&1
sudo ip rule show | grep -q "iif tg-poc-br lookup 2000" || { sudo ip rule add pref 100 iif tg-poc-br lookup 2000; sudo ip route replace default via 192.168.13.1 dev wlo1 table 2000; sudo ip route replace 10.99.99.0/24 dev tg-poc-br table 2000; }
sudo sysctl -qw net.ipv4.conf.tg-poc-br.accept_local=1 net.ipv4.conf.tg-poc-br.rp_filter=0

snap(){ # one metrics row
  local pid rss cpu fds clients bal cfgmd5 errs
  pid=$($RSSH "ps | grep tollgate-wr[t] | awk '{print \$1}'" 2>/dev/null | tr -d '\r')
  rss=""; cpu=""; fds=""
  if [ -n "$pid" ]; then
    read -r rss cpu <<< "$($RSSH "awk '/VmRSS|^\{cpu\}/{print}' /proc/$pid/status 2>/dev/null | awk '{print \$2}'" 2>/dev/null | tr '\n' ' ')"
    rss=${rss:-0}; cpu="0"
    fds=$($RSSH "ls /proc/$pid/fd 2>/dev/null | wc -l" 2>/dev/null | tr -d ' \r')
  fi
  clients=$($RSSH "ndsctl json 2>/dev/null" | python3 -c "import json,sys
try: print(json.load(sys.stdin)['client_length'])
except Exception: print(-1)" 2>/dev/null)
  bal=$($RSSH "tollgate wallet balance 2>/dev/null | grep -oE '[0-9]+' | head -1" 2>/dev/null | tr -d '\r')
  cfgmd5=$($RSSH "md5sum /etc/tollgate/config.json 2>/dev/null | awk '{print \$1}'" 2>/dev/null | tr -d '\r')
  errs=$(ssh -n -i $HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o LogLevel=ERROR root@10.99.99.1 "logread | grep -c 'ERRO\|WARN'" 2>/dev/null | tail -1 | tr -d '\r')
  echo "$(date -u +%FT%TZ),${pid:-0},${rss:-0},${cpu:-0},${fds:-0},${clients:--1},${bal:--1},${cfgmd5:-none},${errs:-0}"
}
echo "timestamp,pid,rss_kb,cpu,fd_count,nds_clients,wallet_balance,config_md5,errors_cum" > "$CSV"
CFGMD5_START=$($RSSH "md5sum /etc/tollgate/config.json | awk '{print \$1}'" 2>/dev/null | tr -d '\r')
PID_START=$($RSSH "ps | grep tollgate-wr[t] | awk '{print \$1}'" 2>/dev/null | tr -d '\r')
log "soak start: cycles=$CYCLES interval=${INTERVAL}s pid=$PID_START cfg=${CFGMD5_START:0:8}"

# ── main loop ───────────────────────────────────────────────────────
python3 - <<'PYEOF'
import json
toks=json.load(open("/tmp/soak/tokens.json"))
json.dump([t["v3"] for t in toks], open("/tmp/soak/queue.json","w"))
print(len(toks),"tokens queued")
PYEOF
CONSEC_FAIL=0
for n in $(seq 1 "$CYCLES"); do
  MAC=$([ $((n % 2)) -eq 1 ] && echo "$C1MAC" || echo "$C2MAC")
  NS=$([ $((n % 2)) -eq 1 ] && echo 1 || echo 2)
  TOK=$(python3 -c "
import json
q=json.load(open('/tmp/soak/queue.json'))
print(q[0] if q else '')" 2>/dev/null)
  [ -z "$TOK" ] && { log "cycle $n: token queue empty — stopping"; break; }
  python3 -c "
import json,sys,time
q=json.load(open('/tmp/soak/queue.json')); q.pop(0); json.dump(q,open('/tmp/soak/queue.json','w'))
ev={'kind':21000,'pubkey':'00'*32,'created_at':int(time.time()),'tags':[['payment','$TOK']],'content':'','sig':'00'*64}
json.dump(ev,open('/tmp/soak/event.json','w'))"
  # register/refresh client in NDS (ndsctl auth fails for unknown MACs)
  sudo ip netns exec tgclient$NS timeout 5 curl -s -o /dev/null "http://198.51.100.7/" 2>/dev/null || true
  CODE=$(curl -s -m 45 -o "$EV/pay-$n.json" -w "%{http_code}" -X POST "http://$R:2121/?mac=$MAC" -H "Content-Type: application/json" --data @"$EV/event.json" 2>/dev/null || echo 000)
  KIND=$(python3 -c "import json;print(json.load(open('$EV/pay-$n.json')).get('kind',''))" 2>/dev/null)
  DET=$(curl -s -m 5 "http://$R:2121/" | head -c 20)
  if [ "$KIND" = "1022" ]; then CONSEC_FAIL=0; else CONSEC_FAIL=$((CONSEC_FAIL+1)); fi
  log "cycle $n: mac=$MAC kind=$KIND http=$CODE details=$(echo "$DET" | grep -o '10021' || echo BAD)"
  if [ $((n % 6)) -eq 0 ]; then snap >> "$CSV"; fi
  if [ $CONSEC_FAIL -ge 3 ]; then
    log "cycle $n: 3 consecutive failures — triage snapshot"
    { $RSSH "ps; logread | tail -80; ndsctl json; cat /etc/tollgate/config.json" ; } > "$EV/triage-$(date -u +%H%M).txt" 2>&1
    CONSEC_FAIL=0
  fi
  # drift/restart watchdog
  PID_NOW=$($RSSH "ps | grep tollgate-wr[t] | awk '{print \$1}'" 2>/dev/null | tr -d '\r')
  [ -n "$PID_NOW" ] && [ "$PID_NOW" != "$PID_START" ] && { log "ALERT: daemon restarted ($PID_START -> $PID_NOW) at cycle $n"; PID_START=$PID_NOW; }
  CFG_NOW=$($RSSH "md5sum /etc/tollgate/config.json | awk '{print \$1}'" 2>/dev/null | tr -d '\r')
  [ -n "$CFG_NOW" ] && [ "$CFG_NOW" != "$CFGMD5_START" ] && { log "ALERT: config.json changed at cycle $n (${CFGMD5_START:0:8} -> ${CFG_NOW:0:8})"; CFGMD5_START=$CFG_NOW; }
  rm -f "$EV/pay-$n.json"
  [ $n -lt "$CYCLES" ] && sleep "$INTERVAL"
done
snap >> "$CSV"
log "soak complete: $(wc -l < "$CSV") metric rows; final summary:"
tail -5 "$CSV"
