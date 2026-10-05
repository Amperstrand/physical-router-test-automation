#!/usr/bin/env python3
"""Mint fault proxy (R12): an HTTP proxy in front of a Cashu mint with
per-route fault rules — the shared fault-injection surface for the
conformance matrix. Generalizes the delay proxies proven in the tmbr
fund-safety lanes.

Rule modes:
  delay <secs>                 forward, delay the response
  drop_response_after_forward  forward to the mint, never answer (ambiguous window)
  status <code> [count n]      answer <code> without forwarding (n times, then pass)
  reset                        close the connection on matching requests
  passthrough (default)

Rules come from a control file (default /tmp/faultproxy.json), rewritten at
runtime by the runner: {"rules": {"<route-substr>": {"mode": ..., ...}}}
plus an optional "count" decrement. Unknown routes pass through untouched.
"""

import json
import http.server
import os
import sys
import time
import urllib.error
import urllib.request

PORT = int(sys.argv[1])
TARGET = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8388"
CONTROL = sys.argv[3] if len(sys.argv) > 3 else "/tmp/faultproxy.json"


def rules():
    try:
        with open(CONTROL) as f:
            return json.load(f).get("rules", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def match_rule(path):
    for sub, r in rules().items():
        if sub in path:
            return r
    return None


def consume(r):
    """Decrement a count-limited rule in the control file (best-effort)."""
    if "count" not in r:
        return
    try:
        with open(CONTROL) as f:
            doc = json.load(f)
        for sub, rr in doc.get("rules", {}).items():
            if rr is r or rr.get("mode") == r.get("mode") and rr.get("count") == r.get("count"):
                rr["count"] = int(rr.get("count", 0)) - 1
                if rr["count"] <= 0:
                    rr.pop("count")
                    rr["expired"] = True
                break
        with open(CONTROL, "w") as f:
            json.dump(doc, f)
    except Exception:
        pass


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _relay(self, body):
        r = match_rule(self.path)
        if r and not r.get("expired"):
            mode = r.get("mode", "passthrough")
            if mode == "status":
                code = int(r.get("status", 500))
                consume(r)
                self.send_response(code)
                msg = json.dumps({"code": 0, "error": f"fault-injected {code}"}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            if mode == "reset":
                consume(r)
                self.close_connection = True
                # Closing mid-request without a response = connection reset
                # from the client's perspective.
                return
            if mode == "drop_response_after_forward":
                consume(r)
                self._forward(body, drop=True)
                return
            if mode == "delay":
                delay = float(r.get("delay", 0))
                time.sleep(delay)
                self._forward(body)
                return
        self._forward(body)

    def _forward(self, body, drop=False):
        req = urllib.request.Request(
            TARGET + self.path,
            data=body if body else None,
            headers={k: v for k, v in self.headers.items()})
        try:
            resp = urllib.request.urlopen(req, timeout=120)
            data, code, ctype = resp.read(), resp.status, resp.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            data, code, ctype = e.read(), e.code, e.headers.get("Content-Type", "application/json")
        except Exception:
            if drop:
                return
            self.send_error(502)
            return
        if drop:
            # The mint answered; the client must never see it.
            return
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._relay(None)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self._relay(self.rfile.read(n) if n else None)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    with open(CONTROL, "w") as f:
        json.dump({"rules": {}}, f)
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
