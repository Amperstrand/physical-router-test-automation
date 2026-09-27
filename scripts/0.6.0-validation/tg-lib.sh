#!/usr/bin/env bash
# tg-lib.sh — shared helpers for the 0.6.0 validation phase scripts (ai-legion).
# Design: phases call finish() which, on ANY failure, auto-collects a triage
# bundle (router state, logs, config) into the phase evidence dir so the
# failure can be analyzed offline instead of interactively.
set -uo pipefail
export EV="${EV:-/tmp/phase-x}"
mkdir -p "$EV"
RSSH="ssh -n -i $HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR root@10.99.99.1"
R=10.99.99.1
CLIENT_MAC=82:49:5a:95:5e:53
MINT="https://signut.cashu.exchange"
PASS=0; FAIL=0
: > "$EV/log.txt"
log(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$EV/log.txt"; }
ok(){  echo "[$(date +%H:%M:%S)] PASS: $*" | tee -a "$EV/log.txt"; PASS=$((PASS+1)); }
bad(){ echo "[$(date +%H:%M:%S)] FAIL: $*" | tee -a "$EV/log.txt"; FAIL=$((FAIL+1)); }

# ensure_client <n> <ip-last-octet> <mac> -> netns client bridged on tg-poc-br
# (own conntrack+L3; host never plays client + gateway in one kernel)
ensure_client(){
  local NS=tgclient$1 IP=10.99.99.$2 MAC=$3
  if ! sudo ip netns list | grep -q "^$NS"; then
    sudo ip netns add $NS
    sudo ip link add veth-c$1 type veth peer name veth-c$1-host
    sudo ip link set veth-c$1 netns $NS
    sudo ip netns exec $NS ip link set veth-c$1 address $MAC
    sudo ip netns exec $NS ip addr add $IP/24 dev veth-c$1
    sudo ip netns exec $NS ip link set lo up
    sudo ip netns exec $NS ip link set veth-c$1 up
    sudo ip netns exec $NS ip route replace default via 10.99.99.1
    sudo ip link set veth-c$1-host master tg-poc-br
    sudo ip link set veth-c$1-host up
  fi
  # return path via the tollgate (physical parity: gateway handles both directions;
  # otherwise router conntrack sees only one side and drops the flow as INVALID)
  sudo ip rule del to $IP/32 2>/dev/null
  sudo ip rule add to $IP/32 lookup 2200
  sudo ip route replace $IP/32 via 10.99.99.1 dev tg-poc-br table 2200
  echo "$NS"
}
cexec(){ sudo ip netns exec "tgclient$1" "${@:2}"; }

# lab_shim: single-interface lab topology means upstream replies arrive iif br-lan
# and hit NDS's ndsNET unmarked (production: WAN zone bypasses it). Accept
# established flows from non-lan sources = production wan-zone semantics;
# client->internet stays NDS-gated.
lab_shim(){
  $RSSH "iptables -C FORWARD -i br-lan -o br-lan ! -s 10.99.99.0/24 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || iptables -I FORWARD 1 -i br-lan -o br-lan ! -s 10.99.99.0/24 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT" >/dev/null 2>&1
}

# pick_token <amount> -> writes $EV/token-in-play.txt, marks used
pick_token(){
  python3 - "$1" <<'EOF'
import json,sys,time,os
amt=int(sys.argv[1])
toks=json.load(open("/tmp/phase-a/tokens.json"))
uf="/tmp/phase-a/used.json"
used=json.load(open(uf)) if os.path.exists(uf) else []
t=next((t for t in toks if t["amount"]==amt and t["v3"] not in used), None)
assert t, f"no unused {amt}-sat token left (used {len(used)})"
open("/tmp/phase-a/token-in-play.txt","w").write(t["v3"])
used.append(t["v3"]); json.dump(used,open(uf,"w"))
ev={"kind":21000,"pubkey":"00"*32,"created_at":int(time.time()),"tags":[["payment",t["v3"]]],"content":"","sig":"00"*64}
json.dump(ev,open("/tmp/phase-a/event.json","w"))
print(f"{amt}-sat token selected")
EOF
}

# pay <mac> <outfile> -> http code; response body in outfile
pay(){
  curl -s -m 45 -o "$2" -w "%{http_code}" -X POST "http://$R:2121/?mac=$1" \
    -H "Content-Type: application/json" --data @/tmp/phase-a/event.json
}

# ev_kind <file> -> kind value
ev_kind(){ python3 -c "import json;print(json.load(open('$1')).get('kind',''))" 2>/dev/null; }

# nds_state <mac> -> state or ""
nds_state(){ $RSSH "ndsctl json" > /tmp/nds-state.json 2>/dev/null; python3 -c "
import json
try: print(json.load(open('/tmp/nds-state.json'))['clients'].get('$1',{}).get('state',''))
except Exception: print('')"; }

# snapshot logread for diffing (call before the action under test)
log_snap(){ $RSSH "logread" > "$EV/logread-snap.txt" 2>/dev/null; }
# new log lines since snap
log_new(){ $RSSH "logread" 2>/dev/null > "$EV/logread-now.txt"; diff "$EV/logread-snap.txt" "$EV/logread-now.txt" | grep '^>' | sed 's/^> //' || true; }

# assert full mode; if degraded, restart once and re-check (test isolation)
ensure_full_mode(){
  local resp; resp=$(curl -s -m 8 -X POST "http://$R:2121/?mac=$CLIENT_MAC" -H "Content-Type: text/plain" -d "cashuApreflight-probe-invalid")
  if echo "$resp" | grep -q "service-unavailable"; then
    log "daemon degraded -> restarting for test isolation"
    $RSSH "/etc/init.d/tollgate-wrt restart" >/dev/null 2>&1; sleep 6
    curl -s -m 8 -X POST "http://$R:2121/?mac=$CLIENT_MAC" -H "Content-Type: text/plain" -d "cashuAprobe" | grep -q "service-unavailable" \
      && { log "still degraded after restart"; return 1; }
  fi
  curl -s -m 5 "http://$R:2121/" | grep -q '"kind":10021' && return 0
  log "daemon not serving details"; return 1
}

# triage bundle — the "analyze bugs later" payload
triage(){
  { echo "=== TRIAGE BUNDLE $(date -u) phase=$EV ==="
    echo "--- daemon ---"; $RSSH "ps | grep tollg | grep -v grep; logread | tail -60" 2>/dev/null
    echo "--- nds ---"; $RSSH "ndsctl status 2>/dev/null | head -12; ndsctl json" 2>/dev/null
    echo "--- config ---"; $RSSH "cat /etc/tollgate/config.json" 2>/dev/null
    echo "--- routes/fw ---"; $RSSH "ip route; iptables -t mangle -S ndsOUT 2>/dev/null | head -5" 2>/dev/null
    echo "--- mint probe from router ---"; $RSSH "wget -q -O- -T 8 $MINT/v1/info 2>&1 | head -c 200; echo; echo rc=\$?" 2>/dev/null
    echo "--- hints ---"
    grep -q "service-unavailable" "$EV"/*.json 2>/dev/null && echo "HINT: daemon was degraded (mint blip marks unreachable on one timeout) — see lab-preflight #3"
    grep -q "timeout" "$EV"/*.json 2>/dev/null && echo "HINT: mint latency — check preflight #7 latency; consider retry/backoff"
    true
  } > "$EV/TRIAGE.txt" 2>&1
  echo "triage bundle: $EV/TRIAGE.txt"
}

finish(){
  triage
  log "═══ RESULTS: $PASS passed, $FAIL failed (evidence: $EV, triage: $EV/TRIAGE.txt) ═══"
  [ $FAIL -eq 0 ]
}
