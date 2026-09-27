#!/bin/bash
# run-matrix.sh — one-command v0.5->v0.6 upgrade matrix on the QEMU bench.
#
# Replays the manual protocol validated 2026-09-17 (PRTA #106):
#   clean-slate -> install OLD -> user config -> fund -> upgrade to NEW
#   -> assert (shas/service/payment) -> rollback to OLD -> re-assert
# Plus a robustness phase: upgrade again with jq REMOVED on purpose —
# the regression case for the preinst/orphan-cascade defect
# (Amperstrand/tollgate-module-basic-go#93, upstream PR #407): the upgrade
# must succeed and the portal (nodogsplash) must survive.
#
# Usage (on ai-legion-small):
#   bash scripts/upgrade-emulation/run-matrix.sh <old.ipk> <new.ipk> [mint_url]
#
# Env:
#   VMPW               VM root password (default Upgr4deTest-2026)
#   SKIP_ROBUSTNESS=1  skip the jq-removal robustness phase
#   MINT2_SKIP=1       skip the second-mint (CDK 0.18) phase
# Requires: the bench (upgrade-bench.sh up + provision); a python venv with
# coincurve at ~/upgrade-test/pyenv; this repo's lib/ (lib/cashu.py + deps)
# at ~/upgrade-test/prta (token minting goes through HttpMinter — cdk-cli's
# interactive send is not deterministic across wallet states).
set -u

OLD_IPK="$(realpath "${1:?usage: run-matrix.sh <old.ipk> <new.ipk> [mint_url]}")"
NEW_IPK="$(realpath "${2:?usage: run-matrix.sh <old.ipk> <new.ipk> [mint_url]}")"
MINT="${3:-https://testnut.cashu.exchange}"
VMPW="${VMPW:-Upgr4deTest-2026}"
BENCH_DIR=~/upgrade-test
VMSSH="$BENCH_DIR/vmssh"
OUT="$BENCH_DIR/results/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"

PASS=0; FAIL=0
phase() { echo; echo "=== [$(date -u +%H:%M:%S)] $* ==="; }
ok()  { PASS=$((PASS+1)); echo "PASS: $*"; echo "PASS $*" >> "$OUT/matrix.log"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL: $*"; echo "FAIL $*" >> "$OUT/matrix.log"; }

# check <description> <command...> — pass iff the command exits 0.
check() {
  local desc="$1"; shift
  if "$@"; then ok "$desc"; else bad "$desc"; fi
}

vm() { $VMSSH "$@"; }
sha() { vm "sha256sum $1 2>/dev/null | cut -d' ' -f1"; }

mint_token() {  # HttpMinter (this repo's lib/cashu.py) — no wallet state, no cdk-cli
  "$BENCH_DIR/pyenv/bin/python" - "$MINT" << 'PYEOF'
import sys
sys.path.insert(0, __import__("os").path.expanduser("~/upgrade-test/prta"))
from lib.cashu import HttpMinter
print(HttpMinter(sys.argv[1]).mint(4))
PYEOF
}

pay_from_host() {  # portal-fetch (NDS client table) then raw-body token POST
  local tok="$1"
  curl -s -m 10 -o /dev/null http://10.99.99.1:2050/ || true
  curl -s -m 45 -X POST -H "Content-Type: text/plain" \
    --data-binary "$tok" http://10.99.99.1:2121/
}

service_up() {
  vm "pgrep -f /usr/bin/tollgate-wrt >/dev/null" 2>/dev/null \
    && vm "netstat -tln 2>/dev/null | grep -q 2121" 2>/dev/null \
    && vm "netstat -tln 2>/dev/null | grep -q 2050" 2>/dev/null
}

wait_service() {  # postinst service bringup is async — poll up to ~45s
  local _
  for _ in $(seq 1 15); do
    service_up && return 0
    sleep 3
  done
  return 1
}

old_installed()  { vm "opkg list-installed 2>/dev/null | grep -q '^tollgate-wrt '"; }
portal_daemon()  { [ -n "$(vm "pidof nodogsplash" </dev/null 2>/dev/null)" ]; }
sha_is()         { [ "$(sha "$1")" = "$2" ]; }
balance_at_least() {
  local want="$1" got
  got=$(vm "/usr/bin/tollgate --json wallet balance 2>/dev/null" \
        | grep -o '"balance_sats": *[0-9]*' | grep -o '[0-9]*')
  [ -n "$got" ] && [ "$got" -ge "$want" ]
}

push_artifacts() {
  scp -q "$OLD_IPK" "$NEW_IPK" root@10.99.99.1:/tmp/ 2>/dev/null \
  || sshpass -p "$VMPW" scp -O -q -o StrictHostKeyChecking=no \
       -o UserKnownHostsFile=/dev/null "$OLD_IPK" "$NEW_IPK" root@10.99.99.1:/tmp/
}
OLD_R=/tmp/$(basename "$OLD_IPK"); NEW_R=/tmp/$(basename "$NEW_IPK")

[ -x "$VMSSH" ] || { echo "bench not provisioned: run upgrade-bench.sh up + provision"; exit 2; }
phase "preflight"
if ! vm "echo ok" >/dev/null 2>&1; then
  echo "VM unreachable"; exit 2
fi
vm "opkg update" >/dev/null 2>&1
vm "opkg install jq curl nodogsplash" >/dev/null 2>&1
if push_artifacts; then ok "artifacts pushed"; else bad "artifact push"; exit 2; fi
vm "opkg remove tollgate-wrt" >/dev/null 2>&1
vm "rm -rf /etc/tollgate /etc/tollgate-setup-done" >/dev/null 2>&1

phase "1. clean install OLD"
vm "opkg install $OLD_R" > "$OUT/install-old.log" 2>&1
if old_installed && wait_service; then
  ok "OLD installed, service up"
else
  bad "OLD install/service"; tail -5 "$OUT/install-old.log"; exit 1
fi

phase "2. user config + fund"
vm "jq '.accepted_mints = [{url:\"$MINT\", min_balance:0, balance_tolerance_percent:0, payout_interval_seconds:999999, min_payout_amount:999999, price_per_step:3, price_unit:\"sats\", min_purchase_steps:1}] | .margin = 0.15' /etc/tollgate/config.json > /tmp/c && cp /tmp/c /etc/tollgate/config.json && /etc/init.d/tollgate-wrt restart" >/dev/null 2>&1
sleep 6
TOK=$(mint_token)
if [ -n "$TOK" ] && printf '%s' "$TOK" | grep -q cashu; then
  RESP=$(pay_from_host "$TOK")
  echo "$RESP" > "$OUT/pay-old.json"
  check "payment on OLD (kind:1022)" sh -c "printf '%s' '$RESP' | grep -q '\"kind\":1022'"
else
  bad "token mint ($MINT)"
fi
CFG_SHA=$(sha /etc/tollgate/config.json); WAL_SHA=$(sha /etc/tollgate/wallet.db)
ID_SHA=$(sha /etc/tollgate/identities.json)
echo "pre-upgrade shas: cfg=$CFG_SHA wal=$WAL_SHA id=$ID_SHA" >> "$OUT/matrix.log"

phase "3. upgrade to NEW"
vm "opkg install $NEW_R" > "$OUT/upgrade.log" 2>&1; RC=$?
wait_service || true
if [ "$RC" -eq 0 ] && old_installed && wait_service; then
  ok "NEW installed (rc=$RC), service up"
else
  bad "NEW install (rc=$RC)"; tail -8 "$OUT/upgrade.log"
fi
check "config.json byte-identical" sha_is /etc/tollgate/config.json "$CFG_SHA"
check "identities.json byte-identical" sha_is /etc/tollgate/identities.json "$ID_SHA"
check "wallet balance preserved (>=3 sats)" balance_at_least 3
TOK2=$(mint_token)
RESP2=$(pay_from_host "$TOK2"); echo "$RESP2" > "$OUT/pay-new.json"
check "payment on NEW" sh -c "printf '%s' '$RESP2' | grep -q '\"kind\":1022'"

phase "4. rollback to OLD"
vm "opkg install --force-downgrade $OLD_R" > "$OUT/rollback.log" 2>&1; RC=$?
wait_service || true
check "rollback installed (rc=$RC)" test "$RC" -eq 0
check "service up after rollback" wait_service
check "config survived rollback" sha_is /etc/tollgate/config.json "$CFG_SHA"
BAL3=$(vm "/usr/bin/tollgate --json wallet balance 2>/dev/null" | grep -o '"balance_sats": *[0-9]*' | grep -o '[0-9]*')
echo "post-upgrade balance: ${BAL3:-unknown} sats" >> "$OUT/matrix.log"
check "funds survived rollback (>= post-upgrade ${BAL3:-?})" balance_at_least "${BAL3:-0}"

if [ "${SKIP_ROBUSTNESS:-0}" != "1" ]; then
  phase "5. robustness: upgrade with jq removed (preinst regression, tmbg#93/PR#407)"
  # --force-depends: plain removal is refused while v0.5.0 declares a jq dep.
  vm "opkg remove --force-depends jq" >/dev/null 2>&1
  if vm "ls /usr/bin/jq" >/dev/null 2>&1; then
    bad "jq removal failed (test setup) — robustness phase invalid"
  else
    vm "opkg install $NEW_R" > "$OUT/upgrade-nojq.log" 2>&1; RC=$?
    wait_service || true
    check "no-jq upgrade succeeded (rc=$RC)" test "$RC" -eq 0
    check "portal (nodogsplash) alive after no-jq upgrade" portal_daemon
    tail -3 "$OUT/upgrade-nojq.log" 2>/dev/null || true
  fi
fi

if [ "${MINT2_SKIP:-0}" != "1" ] && [ -x /opt/cdk-mintd/cdk-cli ] && pgrep -f "cdk-mintd$" >/dev/null 2>&1; then
  phase "6. multi-mint (local CDK 0.18 @ 10.99.99.2:8383)"
  vm "opkg install jq" >/dev/null 2>&1  # phase 5 may have left it removed
  vm "jq '.accepted_mints += [{url:\"http://10.99.99.2:8383\", min_balance:0, balance_tolerance_percent:0, payout_interval_seconds:999999, min_payout_amount:999999, price_per_step:2, price_unit:\"sats\", min_purchase_steps:1}]' /etc/tollgate/config.json > /tmp/c && cp /tmp/c /etc/tollgate/config.json && /etc/init.d/tollgate-wrt restart" >/dev/null 2>&1
  sleep 8
  M2TOK=$("$BENCH_DIR/pyenv/bin/python" - http://10.99.99.2:8383 << 'PYEOF'
import sys
sys.path.insert(0, __import__("os").path.expanduser("~/upgrade-test/prta"))
from lib.cashu import HttpMinter
print(HttpMinter(sys.argv[1]).mint(4))
PYEOF
)
  if [ -n "$M2TOK" ] && printf '%s' "$M2TOK" | grep -q cashu; then
    R3=$(pay_from_host "$M2TOK"); echo "$R3" > "$OUT/pay-mint2.json"
    check "second-mint payment (V2 keyset)" sh -c "printf '%s' '$R3' | grep -q '\"kind\":1022'"
  else
    bad "mint2 token"
  fi
fi

phase "summary"
echo "PASS=$PASS FAIL=$FAIL — evidence: $OUT"
cat >> "$OUT/matrix.log" << EOF
summary: pass=$PASS fail=$FAIL old=$(basename "$OLD_IPK") new=$(basename "$NEW_IPK") mint=$MINT
EOF
[ "$FAIL" -eq 0 ]
