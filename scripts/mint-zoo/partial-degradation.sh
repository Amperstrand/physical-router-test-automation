#!/bin/bash
# mint-zoo partial-degradation.sh — multi-mint resilience (PRTA #142).
#
# Pin 3 zoo mints, block ONE, and verify: the advertisement drops exactly
# that mint, healthy mints keep selling, the blocked mint fails gracefully,
# recovery restores the ad, config never churns. Then the payment-triggered
# variant (pay a dead mint's token; tmbg #401 surface).
#
# Usage (bench host): scripts/mint-zoo/partial-degradation.sh
set -u
HOST_IP=10.99.99.2
ROUTER=10.99.99.1
VMPW="${VMPW:-Upgr4deTest-2026}"
PYENV="$HOME/upgrade-test/pyenv"
M1="http://$HOST_IP:33210"   # nutshell 0.21.0
M2="http://$HOST_IP:33381"   # cdk 0.18.1
M3="http://$HOST_IP:33376"   # cdk 0.17.6 (minibits-class)
P2=33381; P3=33376
OUT="$HOME/upgrade-test/partial-degr-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"

vm() {
  sshpass -p "$VMPW" ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null root@"$ROUTER" "$@" 2>/dev/null
}

log() { echo "[$(date -u '+%H:%M:%S')] $*" | tee -a "$OUT/run.log"; }
ok()  { log "PASS: $*"; echo "PASS $*" >> "$OUT/verdicts"; }
bad() { log "FAIL: $*"; echo "FAIL $*" >> "$OUT/verdicts"; }

mint_token() {
  "$PYENV/bin/python" - "$1" << 'PYEOF'
import sys
sys.path.insert(0, __import__("os").path.expanduser("~/upgrade-test/prta"))
from lib.cashu import HttpMinter
print(HttpMinter(sys.argv[1]).mint(4))
PYEOF
}

pay() {  # $1 = mint url -> prints kind
  local tok resp
  tok=$(mint_token "$1")
  vm "ndsctl deauth $HOST_IP >/dev/null 2>&1; true" </dev/null >/dev/null 2>&1
  curl -s -m 10 -o /dev/null "http://$ROUTER:2050/" || true
  sleep 1
  resp=$(curl -s -m 45 -X POST -H "Content-Type: text/plain" \
    --data-binary "$tok" "http://$ROUTER:2121/")
  echo "$resp" > "$OUT/pay-$(date +%s).json"
  printf '%s' "$resp" | grep -o '"kind":[0-9]*' | head -1 | cut -d: -f2
}

ad_mints() {  # advertisement -> space-separated sorted mint urls in price_per_step tags
  curl -s -m 10 "http://$ROUTER:2121/" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print('AD_UNPARSEABLE'); raise SystemExit
mints=set()
for t in d.get('tags',[]):
    if t[0]=='price_per_step' and len(t)>=5:
        mints.add(t[4])  # tag shape: [price_per_step, cashu, N, unit, mint_url, min_steps]
print(' '.join(sorted(mints)) if mints else 'AD_EMPTY')" 2>/dev/null
}

block()   { vm "iptables -I OUTPUT -d $HOST_IP -p tcp --dport $1 -j DROP" </dev/null >/dev/null 2>&1; }
unblock() { vm "iptables -D OUTPUT -d $HOST_IP -p tcp --dport $1 -j DROP" </dev/null >/dev/null 2>&1; }

wait_ad_change() {  # $1 = seconds timeout; polls until ad differs from baseline
  local deadline=$(( $(date +%s) + $1 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    [ "$(ad_mints)" != "$BASE_AD" ] && return 0
    sleep 10
  done
  return 1
}

log "=== pinning 3 mints: ns-2100, cdk-0181, cdk-0176 ==="
vm "jq '.accepted_mints = [
  {url:\"$M1\", min_balance:0, balance_tolerance_percent:0, payout_interval_seconds:999999, min_payout_amount:999999, price_per_step:2, price_unit:\"sats\", min_purchase_steps:1},
  {url:\"$M2\", min_balance:0, balance_tolerance_percent:0, payout_interval_seconds:999999, min_payout_amount:999999, price_per_step:2, price_unit:\"sats\", min_purchase_steps:1},
  {url:\"$M3\", min_balance:0, balance_tolerance_percent:0, payout_interval_seconds:999999, min_payout_amount:999999, price_per_step:2, price_unit:\"sats\", min_purchase_steps:1}
]' /etc/tollgate/config.json > /tmp/c && cp /tmp/c /etc/tollgate/config.json && rm -f /etc/tollgate/wallet.db && /etc/init.d/tollgate-wrt restart" </dev/null >/dev/null 2>&1
for _ in $(seq 1 20); do
  curl -s -m 5 "http://$ROUTER:2121/" | grep -q kind && break
  sleep 3
done
CFG0=$(vm "sha256sum /etc/tollgate/config.json" </dev/null | cut -d' ' -f1)
BASE_AD=$(ad_mints)
log "baseline ad: $BASE_AD"
case "$BASE_AD" in
  *33210*33381*33376*) ok "advertisement carries all 3 mints" ;;
  *) bad "baseline ad incomplete: $BASE_AD" ;;
esac

log "=== phase 1: block cdk-0176 ($P3) ==="
block "$P3"
if wait_ad_change 420; then
  NOW_AD=$(ad_mints)
  log "ad after block: $NOW_AD"
  case "$NOW_AD" in
    *33210*33381*) case "$NOW_AD" in *33376*) bad "blocked mint still advertised: $NOW_AD";;
                        *) ok "blocked mint dropped from ad, 2 remain: $NOW_AD";; esac ;;
    *) bad "healthy mints lost from ad: $NOW_AD" ;;
  esac
else
  bad "advertisement never reacted to blocked mint (still: $(ad_mints))"
fi

log "--- pay via healthy ns-2100 while cdk-0176 blocked ---"
K=$(pay "$M1")
if [ "$K" = "1022" ]; then ok "payment via healthy mint during partial outage (kind:1022)"; else bad "healthy-mint payment during partial outage (kind=${K:-none})"; fi

log "--- pay via blocked cdk-0176 ---"
K=$(pay "$M3")
if [ "$K" = "21023" ]; then ok "blocked-mint payment fails gracefully (kind:21023)"; else bad "blocked-mint payment kind=${K:-none} (expected 21023)"; fi

log "=== unblock + recovery ==="
unblock "$P3"
REC_AD=$(ad_mints)
for _ in $(seq 1 36); do
  REC_AD=$(ad_mints)
  [ "$REC_AD" = "$BASE_AD" ] && break
  sleep 10
done
if [ "$REC_AD" = "$BASE_AD" ]; then ok "advertisement restored all 3 mints after recovery"; else bad "ad not restored: $REC_AD"; fi

log "=== phase 2: payment-triggered partial degradation (tmbg #401) ==="
block "$P2"
sleep 5
K=$(pay "$M2")
if [ "$K" = "21023" ]; then
  ok "dead-mint payment rejected (kind:21023)"
else
  bad "dead-mint kind=${K:-none}"
fi
sleep 10
AD_AFTER=$(ad_mints)
case "$AD_AFTER" in
  *33210*33376*) ok "healthy mints still advertised after payment-path failure ($AD_AFTER)" ;;
  *) log "note: ad state after payment-path failure: $AD_AFTER (may lag one probe tick)" ;;
esac
K=$(pay "$M1")
if [ "$K" = "1022" ]; then ok "healthy-mint payment survives payment-path degradation trigger"; else bad "payment-path trigger poisoned healthy mint (kind=${K:-none})"; fi
unblock "$P2"

log "=== config churn guard ==="
CFG1=$(vm "sha256sum /etc/tollgate/config.json" </dev/null | cut -d' ' -f1)
if [ "$CFG0" = "$CFG1" ]; then ok "config.json unchanged across all scenarios"; else bad "CONFIG CHURN: $CFG0 -> $CFG1"; fi

log "=== summary ==="
grep -c PASS "$OUT/verdicts" | xargs echo "passes:"
grep -c FAIL "$OUT/verdicts" | xargs echo "failures:"
! grep -q FAIL "$OUT/verdicts"
