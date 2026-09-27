#!/bin/sh
# 00b-verify-router-cred.sh — READ-ONLY lab-credential probe.
#
# Answers exactly one question: does this router still accept the shared lab
# password? Run it FIRST when a deploy/E2E run produces a wall of unrelated
# failures — a rotated or drifted password otherwise looks like a broken router.
#
#   . scripts/offline/00b-verify-router-cred.sh          # uses env.sh defaults
#   ROUTER_IP=192.168.8.1 ROUTER_PASSWORD=test123 . scripts/offline/00b-verify-router-cred.sh
#
# Mutates nothing. Exit 0 = both checks PASS, 1 = at least one FAIL.
#
# SIGNALS THAT MEAN SOMETHING (verified live, OpenWrt 25.12.5, 2026-09-22):
#   * SSH: dropbear "Password auth succeeded" → PASS; "Permission denied" → FAIL.
#   * LuCI: POST /cgi-bin/luci/ with luci_username/luci_password →
#       success = HTTP 302 + Set-Cookie: sysauth_http=<sid>
#       failure = HTTP 403 with no sysauth cookie
#     An unauthenticated **GET** /cgi-bin/luci/ is 403 BY DESIGN (the dispatcher
#     answers 403 + X-LuCI-Login-Required + the login page when there is no
#     session). Never read a 403 GET as "router broken".
#
# NOT A BROWSER TEST, ON PURPOSE: driving the LuCI login <form> in Playwright
# fails on hosts whose Chrome still has the retired password saved — autofill
# re-injects it over the typed value (observed: fill('test123') read back as the
# old password) and the router looks broken while the credential is fine.
#
# BURSTS OF AUTH ATTEMPTS REJECT **CORRECT** CREDENTIALS — verified 2026-09-22:
# after ~a dozen failed probes (Chrome-autofill logins, negative controls) both
# dropbear and LuCI rejected the correct password, then accepted it again a
# minute later with no change on the box. Both checks below therefore retry with
# backoff, and a single FAIL is NOT evidence that the password was rotated.
# Confirm with a retry after 30-60s before touching the router.

set -u

RETRIES="${RETRIES:-3}"
RETRY_SLEEP="${RETRY_SLEEP:-8}"

_kit="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$_kit/scripts/offline/env.sh" ]; then
    . "$_kit/scripts/offline/env.sh"
fi

ROUTER_IP="${ROUTER_IP:-192.168.8.1}"
ROUTER_USER="${ROUTER_USER:-root}"
# shared lab test password — see the rig notes; env wins, literal is the default
ROUTER_PASSWORD="${ROUTER_PASSWORD:-test123}"
LUCI_PORT="${LUCI_PORT:-8080}"

rc=0

if ! command -v sshpass >/dev/null 2>&1; then
    echo "FAIL ssh    : sshpass not installed (apt install sshpass)"
    rc=1
else
    ok=0
    i=1
    while [ "$i" -le "$RETRIES" ]; do
        out=$(sshpass -p "$ROUTER_PASSWORD" ssh \
                -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
                -o PubkeyAuthentication=no \
                -o PreferredAuthentications=password,keyboard-interactive \
                "$ROUTER_USER@$ROUTER_IP" 'echo CRED_OK' 2>&1)
        case "$out" in
            *CRED_OK*) ok=1; break ;;
        esac
        [ "$i" -lt "$RETRIES" ] && sleep "$RETRY_SLEEP"
        i=$((i + 1))
    done
    if [ "$ok" = 1 ]; then
        echo "PASS ssh    : $ROUTER_USER@$ROUTER_IP accepts the shared test password"
    else
        echo "FAIL ssh    : $ROUTER_USER@$ROUTER_IP rejected it ${RETRIES}x — $(echo "$out" | tail -1)"
        echo "              (a burst of attempts can throttle the box; retry after 30-60s before concluding)"
        rc=1
    fi
fi

status=; hdr=
i=1
while [ "$i" -le "$RETRIES" ]; do
    hdr=$(curl -s -D - -o /dev/null -m 10 \
            -d "luci_username=$ROUTER_USER&luci_password=$ROUTER_PASSWORD" \
            "http://$ROUTER_IP:$LUCI_PORT/cgi-bin/luci/" 2>/dev/null)
    printf '%s' "$hdr" | grep -qi '^set-cookie:.*sysauth' && break
    [ "$i" -lt "$RETRIES" ] && sleep "$RETRY_SLEEP"
    i=$((i + 1))
done
status=$(printf '%s' "$hdr" | head -1 | awk '{print $2}')
if printf '%s' "$hdr" | grep -qi '^set-cookie:.*sysauth'; then
    echo "PASS luci   : HTTP $status + sysauth cookie (shared test password accepted)"
elif [ "$status" = "403" ]; then
    echo "FAIL luci   : HTTP 403, no sysauth cookie — password rejected"
    rc=1
else
    echo "FAIL luci   : unexpected HTTP ${status:-none} from http://$ROUTER_IP:$LUCI_PORT/cgi-bin/luci/"
    rc=1
fi

[ "$rc" = 0 ] && echo "CRED-CHECK OK" || echo "CRED-CHECK FAILED"
exit "$rc"
