#!/usr/bin/env bash
# Point a physical TollGate router at a local test mint, and restore it afterwards.
#
# It backs up the original config + wallet cache on first use, so the router can
# always be returned to its shipping state. wallet.db is removed because the
# backend caches mint URLs/keys there; without clearing it a mint switch is
# ignored.
#
# Env:
#   ROUTER_IP       router LAN IP                (default 192.168.1.1)
#   ROUTER_PASSWORD router root password         (required)
#   ROUTER_USER     ssh user                     (default root)
#   PRICE_PER_STEP  price sat/step               (default 1)
#
# Usage:
#   scripts/configure-router-test-mint.sh http://192.168.1.2:3338
#   scripts/configure-router-test-mint.sh --restore
set -euo pipefail

ROUTER_IP="${ROUTER_IP:-192.168.1.1}"
ROUTER_USER="${ROUTER_USER:-root}"
: "${ROUTER_PASSWORD:?set ROUTER_PASSWORD}"
PRICE_PER_STEP="${PRICE_PER_STEP:-1}"

command -v sshpass >/dev/null || { echo "sshpass required" >&2; exit 1; }

SSH=(sshpass -p "$ROUTER_PASSWORD" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 "${ROUTER_USER}@${ROUTER_IP}")
CONFIG=/etc/tollgate/config.json
WALLET=/etc/tollgate/wallet.db
BACKUP_SUFFIX=.pre-deployment-kit

restore() {
  "${SSH[@]}" "
    set -e
    if [ -f ${CONFIG}${BACKUP_SUFFIX} ]; then
      cp ${CONFIG}${BACKUP_SUFFIX} ${CONFIG}
      [ -f ${WALLET}${BACKUP_SUFFIX} ] && cp ${WALLET}${BACKUP_SUFFIX} ${WALLET}
      echo restored ${CONFIG}
    else
      echo 'no backup found — nothing to restore'
    fi
    /etc/init.d/tollgate-wrt restart >/dev/null 2>&1 || true
  "
  exit 0
}

[ "${1:-}" = "--restore" ] && restore

MINT_URL="${1:?usage: $0 <mint_url> | --restore}"

"${SSH[@]}" "
  set -e
  [ -f ${CONFIG}${BACKUP_SUFFIX} ] || cp ${CONFIG} ${CONFIG}${BACKUP_SUFFIX}
  [ -f ${WALLET} ] && [ ! -f ${WALLET}${BACKUP_SUFFIX} ] && cp ${WALLET} ${WALLET}${BACKUP_SUFFIX} || true
  jq '.accepted_mints = [{\"url\":\"${MINT_URL}\",\"min_balance\":64,\"balance_tolerance_percent\":10,\"payout_interval_seconds\":60,\"min_payout_amount\":128,\"price_per_step\":${PRICE_PER_STEP},\"price_unit\":\"sat\",\"purchase_min_steps\":0}]' ${CONFIG}${BACKUP_SUFFIX} > /tmp/tg-cfg.json
  mv /tmp/tg-cfg.json ${CONFIG}
  rm -f ${WALLET} ${WALLET}-shm ${WALLET}-wal
  /etc/init.d/tollgate-wrt restart >/dev/null 2>&1
  sleep 12
  echo 'advertised mints:'
  curl -s -m 8 http://127.0.0.1:2121/ | jq -c '.tags[]|select(.[0]==\"price_per_step\")'
"
echo "Router pointed at ${MINT_URL}. Restore with: ROUTER_PASSWORD=... $0 --restore"
