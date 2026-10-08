#!/usr/bin/env bash
# Smoke test for tests/conformance/faultproxy.py (run from repo root).
set -u
cd "$(dirname "$0")/../.." || exit 1
WORK=$(mktemp -d)
FAIL=0
note() { echo "[smoke] $*"; }
die() { echo "[smoke] FAIL: $*"; FAIL=1; }

# 1. Dummy upstream: echoes JSON, sleeps 1s on /v1/slow, records POST bodies
python3 - "$WORK/upstream-bodies.log" <<'EOF' >"$WORK/upstream.log" 2>&1 &
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
RECORD = sys.argv[1]
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _r(self, body):
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        self._r(json.dumps({"ok": True, "path": self.path}))
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        if self.path.startswith("/v1/slow"):
            time.sleep(1)
        with open(RECORD, "a") as fh:
            fh.write(self.path + " " + body.decode(errors="replace") + "\n")
        self._r(json.dumps({"ok": True, "path": self.path, "echo": body.decode()[:80]}))
    def log_message(self, *a): pass
ThreadingHTTPServer(("127.0.0.1", 18081), H).serve_forever()
EOF
UP=$!
touch "$WORK/upstream-bodies.log"
python3 tests/conformance/faultproxy.py --upstream http://127.0.0.1:18081 --listen 127.0.0.1:18082 >"$WORK/proxy.log" 2>&1 &
PX=$!
sleep 1
P=http://127.0.0.1:18082

# 2. Baseline pass-through
R=$(curl -s "$P/v1/keys")
echo "$R" | grep -q '"ok": *true' && note "pass-through OK" || die "pass-through: $R"

# 3. status 429 with remaining=2, then it must expire
curl -s -X POST "$P/__fault/control" -d '{"rules":[{"match_path":"/v1/quote","action":"status","status_code":429,"remaining":2}]}' | grep -q '"installed"' || die "control install"
C1=$(curl -s -o /dev/null -w '%{http_code}' "$P/v1/quote/bolt11")
C2=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$P/v1/quote/bolt11" -H 'Content-Type: application/plain' -d '{}')
C3=$(curl -s -o /dev/null -w '%{http_code}' "$P/v1/quote/bolt11")
[ "$C1" = 429 ] && [ "$C2" = 429 ] && [ "$C3" = 200 ] && note "status+remaining OK (429,429,200)" || die "status codes: $C1 $C2 $C3"

# 4. delay rule (GET does not sleep upstream; assert the proxy-side delay only)
curl -s -X POST "$P/__fault/control" -d '{"rules":[{"match_path":"/v1/slow","action":"delay","delay_ms":1500}]}' >/dev/null
T0=$(date +%s%N)
curl -s "$P/v1/slow" >/dev/null
MS=$(( ($(date +%s%N) - T0) / 1000000 ))
[ "$MS" -ge 1450 ] && note "delay OK (${MS}ms >= 1450 proxy-side)" || die "delay: ${MS}ms"

# 5. reset rule: curl must fail (connection reset / empty reply)
curl -s -X POST "$P/__fault/control" -d '{"rules":[{"match_path":"/v1/reset","action":"reset"}]}' >/dev/null
curl -s --max-time 5 "$P/v1/reset" >/dev/null 2>&1
[ $? -ne 0 ] && note "reset OK (curl failed)" || die "reset: curl succeeded"

# 6. drop rule: black-hole must time out fast-fail
curl -s -X POST "$P/__fault/control" -d '{"rules":[{"match_path":"/v1/drop","action":"drop"}]}' >/dev/null
DROP_RC=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "$P/v1/drop" 2>&1); RC=$?
[ "$RC" -ne 0 ] || [ "$DROP_RC" = "000" ] && note "drop OK (no response)" || die "drop: rc=$RC code=$DROP_RC"

# 6b. drop_response: upstream MUST have processed it, client gets nothing
curl -s -X POST "$P/__fault/control" -d '{"rules":[{"match_path":"/v1/dropresp","action":"drop_response"}]}' >/dev/null
MARK="dropped-$$"
DR_RC=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' -X POST "$P/v1/dropresp" -H 'Content-Type: text/plain' -d "$MARK" 2>&1); DR=$?
{ [ "$DR" -ne 0 ] || [ "$DR_RC" = "000" ]; } || die "drop_response: client got rc=$DR code=$DR_RC"
grep -q "$MARK" "$WORK/upstream-bodies.log" && note "drop_response OK (upstream processed $MARK, client black-holed)" || die "drop_response: upstream never saw the request"
curl -s -X POST "$P/__fault/control" -d '{"clear":true}' >/dev/null

# 7. blinded-message observation + reuse flag
curl -s -X POST "$P/__fault/control" -d '{"clear":true}' >/dev/null
curl -s -X POST "$P/v1/swap" -H 'Content-Type: application/json' -d '{"outputs":[{"amount":2,"B_":"AAAA"}]}' >/dev/null
curl -s -X POST "$P/v1/swap" -H 'Content-Type: application/json' -d '{"outputs":[{"amount":2,"B_":"AAAA"},{"amount":4,"B_":"BBBB"}]}' >/dev/null
OBS=$(curl -s "$P/__fault/observations")
echo "$OBS" | python3 -c '
import json,sys
o = json.load(sys.stdin)
msgs = o["blinded_messages"]
assert len(msgs) == 2, f"expected 2 distinct hashes, got {len(msgs)}"
counts = sorted(len(v) for v in msgs.values())
assert counts == [1, 2], f"sighting counts wrong: {counts}"
assert len(o["reused"]) == 1, "reuse must flag the double-sighted hash"
print("observations OK: 2 hashes, one reused")' || die "observations: $OBS"

# 8. state endpoint + rule hit counters (control-plane ops are not mint traffic)
curl -s "$P/__fault/state" | python3 -c '
import json,sys
s = json.load(sys.stdin)
assert s["requests"] >= 7, s
print("state OK:", s["requests"], "requests tracked")' || die "state endpoint"

# 9. notify action: proxy POSTs a webhook when a rule matches, then forwards
python3 - <<'EOF' >"$WORK/webhook.log" 2>&1 &
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        print(body.decode(), flush=True)
        b = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def log_message(self, *a): pass
ThreadingHTTPServer(("127.0.0.1", 18083), H).serve_forever()
EOF
WH=$!
sleep 0.5
curl -s -X POST "$P/__fault/control" -d '{"rules":[{"match_path":"/v1/swap","action":"notify","notify_url":"http://127.0.0.1:18083/hit"}]}' >/dev/null
R=$(curl -s "$P/v1/keys")   # unmatched path: no webhook
echo "$R" | grep -q '"ok": *true' || die "notify: unmatched request not forwarded"
sleep 0.5
grep -q "hit" "$WORK/webhook.log" && die "notify: webhook fired on unmatched path"
curl -s -X POST "$P/v1/swap" -H 'Content-Type: application/json' -d '{}' >/dev/null
for i in 1 2 3 4 5; do grep -q '"path": "/v1/swap"' "$WORK/webhook.log" && break; sleep 0.5; done
grep -q '"path": "/v1/swap"' "$WORK/webhook.log" && note "notify OK (webhook fired, request forwarded)" || die "notify: webhook never fired"
kill $WH 2>/dev/null

# 9b. notify_on=response ordering: a slow webhook must delay the client
# response (webhook completes before the response is forwarded)
python3 - <<'EOF' >"$WORK/webhook2.log" 2>&1 &
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        time.sleep(1.0)
        b = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def log_message(self, *a): pass
ThreadingHTTPServer(("127.0.0.1", 18084), H).serve_forever()
EOF
WH2=$!
sleep 0.5
curl -s -X POST "$P/__fault/control" -d '{"rules":[{"match_path":"/v1/keys","action":"notify","notify_on":"response","notify_url":"http://127.0.0.1:18084/hit"}]}' >/dev/null
T0=$(date +%s%N)
curl -s "$P/v1/keys" | grep -q '"ok": *true' || die "notify_on=response: request not forwarded"
MS=$(( ($(date +%s%N) - T0) / 1000000 ))
[ "$MS" -ge 950 ] && note "notify_on=response OK (${MS}ms >= 950: webhook blocked the client response)" || die "notify_on=response ordering: ${MS}ms"
kill $WH2 2>/dev/null
curl -s -X POST "$P/__fault/control" -d '{"clear":true}' >/dev/null

kill $PX $UP 2>/dev/null; wait $PX $UP 2>/dev/null
rm -rf "$WORK"
[ "$FAIL" = 0 ] && echo "[smoke] ALL PASS" || echo "[smoke] FAILURES PRESENT"
exit $FAIL
