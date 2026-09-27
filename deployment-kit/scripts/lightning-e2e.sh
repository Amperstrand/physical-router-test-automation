#!/usr/bin/env bash
# Verify the full Lightning payment path against a physical TollGate router:
#   NDS prime -> POST /ln-invoice -> mint auto-settles -> backend mints tokens
#   -> ndsctl auth -> GET /ln-invoice reports access_granted=true
#
# Requires a local FakeWallet mint (see bring-up-fakewallet-mint.sh) pointed at
# by the router (see configure-router-test-mint.sh). The client MUST be a
# separate machine on the router LAN (not the router itself): the backend binds
# the quote to the client MAC and calls `ndsctl auth <mac>`.
#
# Env:
#   ROUTER_IP  router LAN IP             (default 192.168.1.1)
#   MINT_URL   mint URL advertised       (default http://192.168.1.2:3338)
#   AMOUNT     sats to pay               (default 1)
#   ATTEMPTS   poll iterations           (default 30)
#   INTERVAL   seconds between polls     (default 2)
set -euo pipefail

ROUTER_IP="${ROUTER_IP:-192.168.1.1}"
MINT_URL="${MINT_URL:-http://192.168.1.2:3338}"
AMOUNT="${AMOUNT:-1}"
ATTEMPTS="${ATTEMPTS:-30}"
INTERVAL="${INTERVAL:-2}"

command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }

echo "[ln] priming NDS client table via :2050 and :2051..."
curl -s -m 10 -o /dev/null "http://${ROUTER_IP}:2050/" || true
curl -s -m 10 -o /dev/null "http://${ROUTER_IP}:2051/splash.html" || true
sleep 2

echo "[ln] POST /ln-invoice amount=${AMOUNT} mint=${MINT_URL}"
RESP="$(curl -s -m 30 -X POST "http://${ROUTER_IP}:2121/ln-invoice" \
  -H 'Content-Type: application/json' \
  -d "{\"amount\":${AMOUNT},\"mint_url\":\"${MINT_URL}\"}")"
echo "[ln] ${RESP}"

QUOTE="$(printf '%s' "$RESP" | jq -r '.quote // empty')"
[ -n "$QUOTE" ] || { echo "[ln] FAIL: no quote"; exit 1; }

for i in $(seq 1 "$ATTEMPTS"); do
  sleep "$INTERVAL"
  S="$(curl -s -m 10 "http://${ROUTER_IP}:2121/ln-invoice?quote=${QUOTE}")"
  echo "[ln] ${i}: $(printf '%s' "$S" | jq -c '{state,access_granted,allotment}' 2>/dev/null || echo "$S")"
  if printf '%s' "$S" | jq -e '.access_granted == true' >/dev/null 2>&1; then
    echo "[ln] PASS: access granted (allotment=$(printf '%s' "$S" | jq -r '.allotment'))"
    exit 0
  fi
done

echo "[ln] FAIL: access not granted after ${ATTEMPTS} polls"
echo "[ln] hint: check backend log for 'ensureLightningAccessGranted' errors:"
echo "        ssh root@${ROUTER_IP} 'logread | grep -i lightning | tail -20'"
exit 1
