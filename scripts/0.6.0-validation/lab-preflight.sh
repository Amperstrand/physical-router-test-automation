#!/usr/bin/env bash
# lab-preflight.sh — executable documentation of the QEMU OpenWrt lab invariants.
# Every check here corresponds to a failure that actually cost debugging time.
# Run before any phase. Exit 0 = lab known-good; output explains any failure.
set -uo pipefail
SSH="ssh -n -i $HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR root@10.99.99.1"
R=10.99.99.1
MINT="https://signut.cashu.exchange"
ERR=0
ck(){ # name, condition-result (0/1), remedy
  if [ "$2" -eq 0 ]; then echo "PASS  $1"; else echo "FAIL  $1"; echo "      remedy: $3"; ERR=1; fi
}

# 1. router reachable + ssh
timeout 3 bash -c "echo > /dev/tcp/$R/22" 2>/dev/null; ck "router TCP:22 reachable" $? "start VM: python3 scripts/virtual-lab.py start-poc --host ai-legion"
$SSH "echo ok" >/dev/null 2>&1; ck "ssh to router" $? "check ~/.ssh/id_ed25519 on ai-legion"

# 2. clock skew < 90s (QEMU RTC drifts; breaks log correlation + nostr created_at)
SKEW=$($SSH "date -u +%s" 2>/dev/null); NOW=$(date -u +%s)
if [ -n "${SKEW:-}" ]; then
  D=$(( NOW - SKEW )); RC=0; [ ${D#-} -lt 90 ] || RC=1
  ck "router clock skew ${D#-}s < 90s" $RC "ssh root 'date -s @<host-epoch>' and /etc/init.d/sysntpd restart"
else ck "router clock" 1 "ssh failed"; fi

# 3. daemon serving + NOT degraded (details event = full or cached; probe a payment-shaped check)
DET=$(curl -s -m 5 "http://$R:2121/")
echo "$DET" | grep -q '"kind":10021'; ck "daemon serves kind:10021 details" $? "/etc/init.d/tollgate-wrt restart"
# current state = whichever appears last in logread: a downgrade line or "Merchant ready"
LAST=$($SSH "logread | grep -E 'downgrading to degraded mode|=== Merchant ready ===' | tail -1" 2>/dev/null || true)
echo "$LAST" | grep -q "Merchant ready"; ck "daemon in full mode" $? "mint blip degraded it; restart daemon (mint reachable): /etc/init.d/tollgate-wrt restart"

# 4. hairpin NAT: accept_local on tg-poc-br (router default-routes via the client host itself;
#    bounced packets have src = our own IP -> martian drop without this)
SL=$(sysctl -n net.ipv4.conf.tg-poc-br.accept_local 2>/dev/null)
RC=0; [ "$SL" = "1" ] || RC=1
ck "accept_local=1 on tg-poc-br (hairpin fix)" $RC "sudo sysctl -w net.ipv4.conf.tg-poc-br.accept_local=1"

# 5. ai-legion NAT/forward rules for the lab subnet
NFT=$(sudo nft list ruleset 2>/dev/null || true)
echo "$NFT" | grep -q 'iifname "tg-poc-br".*accept'; ck "nft forward accept iif tg-poc-br" $? "sudo nft insert rule inet vps_killswitch forward iifname \"tg-poc-br\" accept"
echo "$NFT" | grep -qE 'ip saddr 10\.99\.99\.0/24.*masquerade'; ck "nft masquerade 10.99.99.0/24" $? "sudo nft add rule ip nat POSTROUTING ip saddr 10.99.99.0/24 oifname != \"tg-poc-br\" counter masquerade"

# 6. NDS running with preauth rules for :2121/:2051
$SSH "ndsctl status 2>/dev/null | head -3" > /tmp/pf-nds.txt 2>/dev/null || true; grep -q NoDogSplash /tmp/pf-nds.txt; ck "NDS running" $? "/etc/init.d/nodogsplash start"
$SSH "iptables-save 2>/dev/null | grep -E '^-A ndsRTR'" > /tmp/pf-ndsfw.txt 2>/dev/null || true
grep -q 2121 /tmp/pf-ndsfw.txt; ck "NDS preauth allows :2121" $? "see TESTING.md NDS preauth rules"
grep -q 2051 /tmp/pf-ndsfw.txt; ck "NDS preauth allows :2051" $? "see TESTING.md NDS preauth rules"

# 7. mint reachable + latency (signut latency blips cascade into degraded mode)
MT0=$(date +%s%N); MRESP=$(curl -s -m 10 "$MINT/v1/info" | head -c 40); MT1=$(date +%s%N)
echo "$MRESP" | grep -q pubkey; ck "mint /v1/info reachable" $? "signut.cashu.exchange down? check from Mac too"
MS=$(( (MT1-MT0)/1000000 )); RC=0; [ "$MS" -lt 3000 ] || RC=1
ck "mint latency ${MS}ms < 3000ms" $RC "mint slow -> expect payment timeouts + degraded risk; consider waiting"

# 8. token inventory for payment phases
if [ -f /tmp/phase-a/tokens.json ]; then
  python3 - <<'EOF' && ck "token inventory (4sat>=3, 1sat>=5)" 0 "re-run /tmp/phase-0/mint-tokens.mjs on the Mac" || ck "token inventory" 1 "re-run mint"
import json
d=json.load(open("/tmp/phase-a/tokens.json"))
u=json.load(open("/tmp/phase-a/used.json")) if __import__("os").path.exists("/tmp/phase-a/used.json") else []
a=[t for t in d if t["v3"] not in u]
assert sum(1 for t in a if t["amount"]==4)>=3, f"4sat left={sum(1 for t in a if t['amount']==4)}"
assert sum(1 for t in a if t["amount"]==1)>=5, f"1sat left={sum(1 for t in a if t['amount']==1)}"
EOF
else ck "token inventory" 1 "scp tokens.json to ai-legion:/tmp/phase-a/"; fi

# 9. lab shims (see docs: single-interface topology + hairpin gateway host)
ip rule show | grep -q "iif tg-poc-br lookup 2000"; ck "policy rule: router-bounced -> wlo1 table" $? "sudo ip rule add pref 100 iif tg-poc-br lookup 2000; sudo ip route replace default via 192.168.13.1 dev wlo1 table 2000"
$SSH "iptables -C FORWARD -i br-lan -o br-lan ! -s 10.99.99.0/24 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT" >/dev/null 2>&1; ck "router lab_shim (wan-zone parity)" $? "source /tmp/tg-lib.sh && lab_shim"
[ "$(sysctl -n net.ipv4.conf.tg-poc-br.rp_filter)" = "0" ]; ck "rp_filter=0 on tg-poc-br" $? "sudo sysctl -w net.ipv4.conf.tg-poc-br.rp_filter=0"

echo; [ $ERR -eq 0 ] && echo "LAB READY" || echo "LAB NOT READY — fix the above before running phases"
exit $ERR
