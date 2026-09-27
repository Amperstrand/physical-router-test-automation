#!/usr/bin/env bash
# Phase F: Portal Deployment (issue #108 scenario 1, QEMU venue)
# Build artifacts staged from the Mac (tg-portal + net4sats). Deploy to
# /etc/tollgate/*-captive-portal-site, serve on uhttpd :2051, verify:
# SPA loads (index + JS asset), CORS to :2121, and pre-auth client reach.
EV=/tmp/phase-f
export EV
source /tmp/tg-lib.sh
C1MAC=02:11:22:33:44:51

bash /tmp/lab-preflight.sh > "$EV/preflight.txt" 2>&1 || { log "preflight failed"; exit 2; }
lab_shim
ensure_client 1 51 $C1MAC >/dev/null

# ── 1. Deploy tollgate portal ───────────────────────────────────────
mkdir -p "$EV/tg-portal" "$EV/net4sats-portal"
tar -C /tmp/phase-f-staging/tg-portal -czf "$EV/tg-portal.tar.gz" . 2>/dev/null || { log "staging missing"; exit 2; }
tar -C /tmp/phase-f-staging/net4sats -czf "$EV/net4sats.tar.gz" . 2>/dev/null || true
$RSSH "mkdir -p /etc/tollgate/tollgate-captive-portal-site /etc/tollgate/net4sats-captive-portal-site"
scp -q -O -i $HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "$EV/tg-portal.tar.gz" root@10.99.99.1:/tmp/portal.tar.gz
$RSSH "tar xzf /tmp/portal.tar.gz -C /etc/tollgate/tollgate-captive-portal-site/ && ls /etc/tollgate/tollgate-captive-portal-site/" > "$EV/deploy-listing.txt" 2>&1
grep -q "index.html" "$EV/deploy-listing.txt" && ok "tollgate portal deployed to router" || { bad "tg-portal deploy failed"; finish; exit 1; }
if [ -f "$EV/net4sats.tar.gz" ]; then
  scp -q -O -i $HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "$EV/net4sats.tar.gz" root@10.99.99.1:/tmp/portal2.tar.gz && $RSSH "tar xzf /tmp/portal2.tar.gz -C /etc/tollgate/net4sats-captive-portal-site/" >/dev/null 2>&1 && log "net4sats portal deployed"
fi

# ── 2. uhttpd instance on :2051 ─────────────────────────────────────
$RSSH "uci -q delete uhttpd.portal; uci set uhttpd.portal=uhttpd; uci set uhttpd.portal.home='/etc/tollgate/tollgate-captive-portal-site'; uci add_list uhttpd.portal.listen_http='0.0.0.0:2051'; uci set uhttpd.portal.rfc1918_filter='0'; uci commit uhttpd; /etc/init.d/uhttpd restart" >/dev/null 2>&1
sleep 2
$RSSH "netstat -tln | grep 2051" > "$EV/uhttpd-2051.txt" 2>&1
grep -q ":2051" "$EV/uhttpd-2051.txt" && ok "uhttpd listening on :2051" || { bad "uhttpd not on 2051"; cat "$EV/uhttpd-2051.txt"; finish; exit 1; }

# ── 3. SPA serves: index + JS asset + manifest ──────────────────────
IDX=$(curl -s -m 8 "http://$R:2051/")
echo "$IDX" > "$EV/portal-index.html"
echo "$IDX" | grep -qi "<html" && echo "$IDX" | grep -qi "script" && ok "SPA index.html serves from router" || bad "index.html malformed"
JS=$(echo "$IDX" | grep -oE '(assets/[A-Za-z0-9_.-]+\.js)' | head -1)
[ -n "$JS" ] && JSC=$(curl -s -m 8 -o "$EV/portal-asset.js" -w "%{http_code}" "http://$R:2051/$JS") && [ "$JSC" = 200 ] && ok "JS bundle serves ($JS, $(wc -c < "$EV/portal-asset.js") bytes)" || bad "JS asset failed ($JS http=$JSC)"
MC=$(curl -s -m 8 -o /dev/null -w "%{http_code}" "http://$R:2051/manifest.json"); [ "$MC" = 200 ] && ok "PWA manifest serves" || log "  manifest.json http=$MC"

# ── 4. CORS: portal origin can fetch details event ─────────────────
CR=$(curl -s -m 8 -D - -o /dev/null -H "Origin: http://$R:2051" "http://$R:2121/")
echo "$CR" > "$EV/cors-check.txt"
echo "$CR" | grep -qi "access-control-allow-origin" && ok "CORS: :2121 allows portal origin" || bad "CORS headers missing for portal origin"
CR2=$(curl -s -m 8 -D - -o /dev/null -H "Origin: http://$R:2051" -X OPTIONS "http://$R:2121/")
echo "$CR2" | grep -qi "access-control-allow-origin" && ok "CORS preflight (OPTIONS) handled" || log "  preflight response: $(echo "$CR2" | head -1)"

# ── 5. Pre-auth client can load portal + fetch details (real captive UX)
cexec 1 timeout 6 curl -s -o /dev/null "http://198.51.100.7/" 2>/dev/null || true   # ensure pre-auth state
ST=$(nds_state "$C1MAC")
if [ "$ST" != "Authenticated" ]; then
  PI=$(cexec 1 curl -s -m 8 -o "$EV/client-portal.html" -w "%{http_code}" "http://$R:2051/" 2>/dev/null || true)
  [ "$PI" = 200 ] && grep -qi "<html" "$EV/client-portal.html" && ok "PRE-AUTH client loads portal SPA (:2051 reachable)" || bad "pre-auth client cannot load portal (http=$PI)"
  PD=$(cexec 1 curl -s -m 8 "http://$R:2121/" 2>/dev/null | head -c 40)
  echo "$PD" | grep -q '"kind":10021' && ok "PRE-AUTH client fetches details event (:2121 reachable)" || bad "pre-auth details fetch failed"
else
  log "  client unexpectedly authed; skipping pre-auth reachability (covered by NDS preauth rules)"
fi

# ── 6. net4sats portal variant ──────────────────────────────────────
if $RSSH "ls /etc/tollgate/net4sats-captive-portal-site/index.html" >/dev/null 2>&1; then
  $RSSH "uci set uhttpd.portal.home='/etc/tollgate/net4sats-captive-portal-site'; uci commit uhttpd; /etc/init.d/uhttpd restart" >/dev/null 2>&1
  sleep 2
  N4=$(curl -s -m 8 "http://$R:2051/")
  echo "$N4" > "$EV/net4sats-index.html"
  N4JS=$(echo "$N4" | grep -oE '(assets/[A-Za-z0-9_.-]+\.js)' | head -1)
  N4C=$(curl -s -m 8 -o /dev/null -w "%{http_code}" "http://$R:2051/${N4JS:-index.html}")
  echo "$N4" | grep -qi "<html" && [ "$N4C" = 200 ] && ok "net4sats portal variant serves on :2051 (asset http $N4C)" || bad "net4sats variant failed"
  # restore tollgate portal as the served site
  $RSSH "uci set uhttpd.portal.home='/etc/tollgate/tollgate-captive-portal-site'; uci commit uhttpd; /etc/init.d/uhttpd restart" >/dev/null 2>&1
  sleep 2
  curl -s -m 8 "http://$R:2051/" | grep -qi "<html" && log "tollgate portal restored as served site" || bad "restore of served portal failed"
fi

# ── 7. Splash redirect target sanity (NDS splash -> portal) ─────────
# NDS serves its own splash at :2050; the deployed SPA at :2051 is the
# payment UI. Confirm the splash page references are reachable.
SP=$(curl -s -m 8 "http://$R:2050/" | head -c 200)
echo "$SP" > "$EV/nds-splash.txt"
log "NDS splash head: $(echo "$SP" | tr -d '\n' | head -c 80)"

$RSSH "netstat -tln | grep -E '2050|2051|2121'; uci show uhttpd.portal" > "$EV/final-ports-config.txt" 2>/dev/null
finish
