#!/usr/bin/env bash
# Build the captive-portal SPA and hot-deploy it onto a running TollGate router.
#
# This is the fast loop for portal-only changes: no feed/package rebuild. It
# replaces the served SPA at /etc/tollgate/tollgate-captive-portal-site (uhttpd
# instance on :2051) with the local build, keeping a one-time backup.
#
# Run this on a host that can reach the router LAN and has the portal repo +
# Node toolchain (e.g. a laptop cabled to the router). To build on a different
# machine and deploy from the LAN host, see runbooks/hot-deploy-via-jump-host.md.
#
# Env:
#   PORTAL_REPO     path to OpenTollGate/tollgate-captive-portal-site (default: $PWD)
#   ROUTER_IP       router LAN IP                (default 192.168.1.1)
#   ROUTER_PASSWORD router root password         (required)
#   ROUTER_USER     ssh user                     (default root)
#   PORTAL_DIR      on-router webroot            (default /etc/tollgate/tollgate-captive-portal-site)
#   VITE_BASE_PATH  vite base path               (default /)
#   SKIP_BUILD=1    deploy the existing ./build instead of rebuilding
set -euo pipefail

PORTAL_REPO="${PORTAL_REPO:-$PWD}"
ROUTER_IP="${ROUTER_IP:-192.168.1.1}"
ROUTER_USER="${ROUTER_USER:-root}"
: "${ROUTER_PASSWORD:?set ROUTER_PASSWORD}"
PORTAL_DIR="${PORTAL_DIR:-/etc/tollgate/tollgate-captive-portal-site}"
export VITE_BASE_PATH="${VITE_BASE_PATH:-/}"

command -v sshpass >/dev/null || { echo "sshpass required" >&2; exit 1; }

SSH=(sshpass -p "$ROUTER_PASSWORD" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 "${ROUTER_USER}@${ROUTER_IP}")
SCP=(sshpass -p "$ROUTER_PASSWORD" scp -O -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)

cd "$PORTAL_REPO"

if [ "${SKIP_BUILD:-0}" != "1" ]; then
  [ -d node_modules ] || npm ci
  npm run build
fi

[ -f build/splash.html ] || [ -f build/index.html ] || { echo "no build output — run npm run build" >&2; exit 1; }

TMP_TAR="$(mktemp /tmp/tg-portal-XXXXXX.tgz)"
tar czf "$TMP_TAR" -C build .
"${SCP[@]}" "$TMP_TAR" "${ROUTER_USER}@${ROUTER_IP}:/tmp/tg-portal.tgz"
rm -f "$TMP_TAR"

"${SSH[@]}" "
  set -e
  DST='${PORTAL_DIR}'
  rm -rf /tmp/portal-new && mkdir -p /tmp/portal-new
  tar xzf /tmp/tg-portal.tgz -C /tmp/portal-new
  [ -d \"\${DST}.pre-deployment-kit\" ] || cp -a \"\$DST\" \"\${DST}.pre-deployment-kit\"
  rm -rf \"\$DST\"/*
  cp -a /tmp/portal-new/. \"\$DST\"/
  find \"\$DST\" -type f -exec touch {} +
  /etc/init.d/uhttpd restart >/dev/null 2>&1 || true
  echo 'deployed:'; ls \"\$DST\"
"

echo "== verifying =="
curl -s -m 8 -o /dev/null -w "splash.html: %{http_code}\n" "http://${ROUTER_IP}:2051/splash.html?_cb=$(date +%s)" || true
curl -s -m 8 -o /dev/null -w "balance.html: %{http_code}\n" "http://${ROUTER_IP}:2051/balance.html?_cb=$(date +%s)" || true
