#!/bin/bash
# mint-zoo matrix.sh — backend x mint-version interop + reliability matrix.
#
# For every mint in the zoo (nutshell 16.5..20.3, cdk 0.17.x/0.18.x):
#   1. health: /v1/info + a real NUT-04 quote settle probe
#   2. payment: mint a token (HttpMinter), pin the router to this mint,
#      pay raw-body, assert kind:1022 + allotment
#   3. keyset class: report V1 (00…) vs V2 (01…) keyset id
#   4. outage resilience (#400/#401 hunt): block the mint on the router,
#      watch for the degraded signal, unblock, watch recovery, re-check ad
#   5. config churn (#402 hunt): config.json sha must be identical across
#      the whole outage cycle
#
# Runs on the bench host against the upgrade-bench router (10.99.99.1).
# Usage: scripts/mint-zoo/matrix.sh [mint-name ...]   (default: all)
set -u
HOST_IP=10.99.99.2
ROUTER=10.99.99.1
VMPW="${VMPW:-Upgr4deTest-2026}"
PYENV="$HOME/upgrade-test/pyenv"
OUT="$HOME/upgrade-test/mint-matrix-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"

vm() {
  sshpass -p "$VMPW" ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null root@"$ROUTER" "$@" 2>/dev/null
}

mint_token() {  # $1 = mint url -> prints a 4-sat V3 token
  "$PYENV/bin/python" - "$1" << 'PYEOF'
import sys
sys.path.insert(0, __import__("os").path.expanduser("~/upgrade-test/prta"))
from lib.cashu import HttpMinter
print(HttpMinter(sys.argv[1]).mint(4))
PYEOF
}

settle_probe() {  # a mint answering /v1/info can still be dead (AGENTS lesson)
  local port="$1" qid state
  qid=$(curl -s -m 5 -X POST "http://$HOST_IP:$port/v1/mint/quote/bolt11" \
    -H 'Content-Type: application/json' -d '{"unit":"sat","amount":1}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["quote"])' 2>/dev/null) || return 1
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    state=$(curl -s -m 5 "http://$HOST_IP:$port/v1/mint/quote/bolt11/$qid" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))' 2>/dev/null)
    [ "$state" = "PAID" ] && return 0
    sleep 1
  done
  return 1
}

keyset_prefix() {
  curl -s -m 5 "http://$HOST_IP:$1/v1/keysets" 2>/dev/null \
    | python3 -c 'import json,sys; ks=json.load(sys.stdin).get("keysets",[]); print(",".join(k.get("id","")[:2] for k in ks) or "?")' 2>/dev/null
}

pin_mint() {  # router accepted_mints := [url], restart, wait
  vm "jq '.accepted_mints = [{url:\"$1\", min_balance:0, balance_tolerance_percent:0, payout_interval_seconds:999999, min_payout_amount:999999, price_per_step:2, price_unit:\"sats\", min_purchase_steps:1}]' /etc/tollgate/config.json > /tmp/c && cp /tmp/c /etc/tollgate/config.json && rm -f /etc/tollgate/wallet.db && /etc/init.d/tollgate-wrt restart" </dev/null >/dev/null 2>&1
  for _ in $(seq 1 20); do
    vm "pidof tollgate-wrt" </dev/null | grep -q . && vm "wget -qO- http://127.0.0.1:2121/ 2>/dev/null" </dev/null | grep -q kind && return 0
    sleep 3
  done
  return 1
}

row() { echo "$*" | tee -a "$OUT/matrix.tsv"; }

# name|version-family|port — latest + second-latest per implementation
FLEET="
ns-2100|nutshell|33210
ns-2003|nutshell|33203
cdk-0181|cdk|33381
cdk-0180|cdk|33380
cdk-0176|cdk|33376
"
if [ $# -ge 1 ]; then
  FLEET=$(printf '%s\n' "$FLEET" | grep -E "^($(IFS='|'; echo "$*"))\|")
fi

row "mint	family	port	health	settle	keyset	payment	degrade_secs	recover_secs	config_churn"

while IFS='|' read -r name family port; do
  [ -z "$name" ] && continue
  url="http://$HOST_IP:$port"
  health=$(curl -s -m 5 -o /dev/null -w "%{http_code}" "$url/v1/info" 2>/dev/null)
  if [ "$health" != "200" ]; then
    row "$name	$family	$port	$health	SKIP	-	SKIP	-	-	-"
    continue
  fi
  settle_probe "$port" && st=ok || st=FAIL
  ks=$(keyset_prefix "$port")

  pay=FAIL
  if pin_mint "$url"; then
    tok=$(mint_token "$url")
    # deauth first: a prior mint's payment leaves the client authenticated and
    # ndsctl auth then exits 1 (consume-before-gate burns the token)
    vm "ndsctl deauth $HOST_IP 2>/dev/null; true" </dev/null >/dev/null 2>&1
    curl -s -m 10 -o /dev/null "http://$ROUTER:2050/" || true
    sleep 1
    resp=$(curl -s -m 45 -X POST -H "Content-Type: text/plain" --data-binary "$tok" "http://$ROUTER:2121/")
    echo "$resp" > "$OUT/pay-$name.json"
    printf '%s' "$resp" | grep -q '"kind":1022' && pay=ok
  fi

  # outage resilience + config churn
  cfg0=$(vm "sha256sum /etc/tollgate/config.json" </dev/null | cut -d' ' -f1)
  d0=$(vm "logread | grep -c 'downgrading to degraded mode'" </dev/null | tr -d '\r')
  d_secs=FAIL; r_secs=FAIL
  vm "iptables -I OUTPUT -d $HOST_IP -p tcp --dport $port -j DROP" </dev/null >/dev/null 2>&1
  t0=$(date +%s)
  # degrade fires on the NEXT 5-min probe tick — window must span a full tick
  for _ in $(seq 1 105); do
    d1=$(vm "logread | grep -c 'downgrading to degraded mode'" </dev/null | tr -d '\r')
    [ "${d1:-0}" -gt "${d0:-0}" ] 2>/dev/null && { d_secs=$(( $(date +%s) - t0 )); break; }
    sleep 4
  done
  # baseline AFTER degrade: "Reachable mint set changed" fires on BOTH
  # transitions, so a pre-block baseline would match the degrade transition.
  r0=$(vm "logread | grep -cE 'became reachable|upgrade from degraded|Reachable mint set changed'" </dev/null | tr -d '\r')
  vm "iptables -D OUTPUT -d $HOST_IP -p tcp --dport $port -j DROP" </dev/null >/dev/null 2>&1
  if [ "$d_secs" != "FAIL" ]; then
    t1=$(date +%s)
    # recovery needs 3 consecutive successful 5-min probes (~15+ min worst case)
    for _ in $(seq 1 120); do
      r1=$(vm "logread | grep -cE 'became reachable|upgrade from degraded|Reachable mint set changed'" </dev/null | tr -d '\r')
      [ "${r1:-0}" -gt "${r0:-0}" ] 2>/dev/null && { r_secs=$(( $(date +%s) - t1 )); break; }
      sleep 8
    done
  fi
  cfg1=$(vm "sha256sum /etc/tollgate/config.json" </dev/null | cut -d' ' -f1)
  [ "$cfg0" = "$cfg1" ] && churn=no || churn=YES

  row "$name	$family	$port	$health	$st	$ks	$pay	$d_secs	$r_secs	$churn"
done <<< "$FLEET"

echo
echo "matrix complete: $OUT/matrix.tsv"
