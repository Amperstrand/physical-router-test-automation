#!/usr/bin/env python3
"""Mint fault proxy for the shared conformance matrix (tests/conformance/).

Sits between a TollGate backend and its mint. Applies per-route fault
rules (drop / delay / status / reset) and records every blinded message
the backend exposes to the mint, so lanes can assert the
``no-output-reuse`` invariant: each blinded message must reach the mint
at most once (deterministic re-derivation is the #257/#266/#480 brick).

Deliberately stdlib-only (no PyYAML, no requests): both backend repos and
any CI runner can drive it without extra dependencies. Rules come from
``matrix.yaml`` scenarios but are injected at runtime via the control
endpoint (or a JSON file), not parsed here.

Usage:
    python3 faultproxy.py --upstream http://127.0.0.1:8080 --listen 127.0.0.1:9090
    python3 faultproxy.py --upstream ... --rules rules.json

Control plane (JSON):
    POST /__fault/control   {"rules": [rule, ...]}   replace the rule set
    POST /__fault/control   {"clear": true}          remove all rules
    GET  /__fault/state     hit counters + rule state
    GET  /__fault/observations
                             blinded-message sightings per hash, with the
                             request paths that carried them

Rule schema (matches matrix.yaml fault.proxy entries):
    match_path:   substring matched against the request path
    match_method: optional HTTP method filter (e.g. "POST")
    action:       drop | delay | status | reset | pass | notify
    status_code:  for action=status (e.g. 429, 500)
    delay_ms:     for action=delay
    notify_url:   for action=notify (webhook POSTed before the request is
                  forwarded — blocking, so a lane can act on the trigger,
                  e.g. kill the backend, before the mint response reaches it)
    remaining:    apply to the next N matching requests only (default: all)
    probability:  0.0..1.0 (default 1.0)

Part of the conformance backbone co-owned by
OpenTollGate/tollgate-module-basic-go#503 and
Amperstrand/tollgate-module-basic-rust#15.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request

CONTROL_PREFIX = "/__fault/"

VALID_ACTIONS = {"drop", "drop_response", "delay", "status", "reset", "pass", "notify"}


class Rule:
    """One compiled fault rule with its remaining-match bookkeeping."""

    def __init__(self, spec: dict, index: int) -> None:
        self.id = spec.get("id", f"rule-{index}")
        self.match_path = spec.get("match_path", "")
        self.match_method = str(spec.get("match_method", "")).upper()
        action = spec.get("action", "pass")
        if action not in VALID_ACTIONS:
            raise ValueError(f"rule {self.id}: unknown action {action!r}")
        self.action = action
        self.status_code = int(spec.get("status_code", 503))
        self.delay_ms = int(spec.get("delay_ms", 0))
        self.notify_url = spec.get("notify_url", "")
        notify_on = spec.get("notify_on", "request")
        if notify_on not in {"request", "response"}:
            raise ValueError(f"rule {self.id}: notify_on must be 'request' or 'response'")
        self.notify_on = notify_on
        self.remaining = spec.get("remaining")  # None = unlimited
        self.probability = float(spec.get("probability", 1.0))
        self.hits = 0

    def matches(self, method: str, path: str) -> bool:
        if self.match_method and method.upper() != self.match_method:
            return False
        return self.match_path in path

    def exhausted(self) -> bool:
        return self.remaining is not None and self.remaining <= 0

    def consume(self) -> bool:
        """Record a hit; return True if this rule should fire now."""
        self.hits += 1
        if self.remaining is not None:
            self.remaining -= 1
        return self.probability >= 1.0 or random.random() < self.probability

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "match_path": self.match_path,
            "match_method": self.match_method,
            "action": self.action,
            "status_code": self.status_code,
            "delay_ms": self.delay_ms,
            "notify_url": self.notify_url,
            "notify_on": self.notify_on,
            "remaining": self.remaining,
            "probability": self.probability,
            "hits": self.hits,
        }


class ProxyState:
    """Shared rule set + observation log, guarded by one lock."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rules: list[Rule] = []
        # sha256(blinded message) -> list of "METHOD path" sightings
        self.blinded_sightings: dict[str, list[str]] = {}
        self.request_count = 0

    def set_rules(self, specs: list[dict]) -> list[str]:
        with self.lock:
            self.rules = [Rule(spec, i) for i, spec in enumerate(specs)]
            return [r.id for r in self.rules]

    def clear(self) -> None:
        with self.lock:
            self.rules = []

    def next_action(self, method: str, path: str) -> Rule | None:
        """Return the first matching, non-exhausted rule that fires."""
        with self.lock:
            for rule in self.rules:
                if rule.exhausted():
                    continue
                if rule.matches(method, path) and rule.consume():
                    return rule
                if rule.matches(method, path):
                    # matched but probability said no — keep scanning
                    continue
            return None

    def observe_blinded(self, method: str, path: str, body: bytes) -> None:
        """Hash every blinded message (B_) in a JSON request body."""
        if not body:
            return
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict):
            return
        for key in ("outputs", "inputs"):
            items = payload.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                b = item.get("B_") or item.get("blinded_message")
                if not isinstance(b, str) or not b:
                    continue
                digest = hashlib.sha256(b.encode()).hexdigest()
                with self.lock:
                    self.blinded_sightings.setdefault(digest, []).append(f"{method} {path}")

    def count_request(self) -> int:
        with self.lock:
            self.request_count += 1
            return self.request_count

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "requests": self.request_count,
                "rules": [r.to_dict() for r in self.rules],
            }

    def observations(self) -> dict:
        with self.lock:
            return {
                "blinded_messages": {
                    digest: sightings
                    for digest, sightings in self.blinded_sightings.items()
                },
                "reused": [
                    digest
                    for digest, sightings in self.blinded_sightings.items()
                    if len(sightings) > 1
                ],
            }


STATE = ProxyState()


def make_handler(upstream: str):
    class FaultProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ---- helpers -----------------------------------------------------
        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length > 0 else b""

        # ---- control plane ------------------------------------------------
        def _control(self) -> bool:
            if not self.path.startswith(CONTROL_PREFIX):
                return False
            cmd = self.path[len(CONTROL_PREFIX) :]
            if cmd == "control" and self.command == "POST":
                try:
                    payload = json.loads(self._read_body() or b"{}")
                except ValueError:
                    self._send_json(400, {"error": "invalid JSON"})
                    return True
                if payload.get("clear"):
                    STATE.clear()
                    self._send_json(200, {"cleared": True})
                    return True
                specs = payload.get("rules")
                if not isinstance(specs, list):
                    self._send_json(400, {"error": "expected {'rules': [...]}'"})
                    return True
                try:
                    ids = STATE.set_rules(specs)
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return True
                self._send_json(200, {"installed": ids})
            elif cmd == "state" and self.command == "GET":
                self._send_json(200, STATE.snapshot())
            elif cmd == "observations" and self.command == "GET":
                self._send_json(200, STATE.observations())
            else:
                self._send_json(404, {"error": f"unknown control op {self.command} {cmd}"})
            return True

        # ---- HTTP verbs ------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            if self._control():
                return
            self._forward(b"")

        def do_POST(self) -> None:  # noqa: N802
            if self._control():
                return
            self._forward(self._read_body())

        def do_PUT(self) -> None:  # noqa: N802
            self._forward(self._read_body())

        def do_DELETE(self) -> None:  # noqa: N802
            self._forward(b"")

        # ---- forwarding -----------------------------------------------------
        def _notify(self, rule: Rule, method: str, path: str) -> None:
            if not rule.notify_url:
                return
            payload = json.dumps({"rule": rule.id, "method": method, "path": path}).encode()
            req = urllib.request.Request(
                rule.notify_url, data=payload, method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(req, timeout=5).read()
            except OSError:
                sys.stderr.write(f"[faultproxy] notify {rule.notify_url} failed\n")

        def _forward(self, body: bytes) -> None:
            STATE.count_request()
            STATE.observe_blinded(self.command, self.path, body)
            rule = STATE.next_action(self.command, self.path)

            if rule is not None:
                if rule.action == "reset":
                    # Close the socket mid-flight: client sees connection reset.
                    try:
                        self.connection.close()
                    except OSError:
                        pass
                    return
                if rule.action == "drop":
                    # Request-side black-hole: the upstream never sees the
                    # request; the client stalls then errors.
                    time.sleep(min(rule.delay_ms, 10_000) / 1000.0 if rule.delay_ms else 0.0)
                    try:
                        self.connection.close()
                    except OSError:
                        pass
                    return
                if rule.action == "status":
                    self._send_json(rule.status_code, {"fault": rule.id, "injected": True})
                    return
                if rule.action == "notify" and rule.notify_on == "request":
                    # Blocking by design: lanes use notify to act (e.g. kill the
                    # backend) BEFORE the request reaches the mint.
                    self._notify(rule, self.command, self.path)
                if rule.action == "delay":
                    time.sleep(rule.delay_ms / 1000.0)
                # action == "pass" falls through to forwarding

            outcome = self._upstream(body)

            if rule is not None:
                if rule.action == "notify" and rule.notify_on == "response":
                    # The mint has processed the request; fire the webhook
                    # BEFORE the response reaches the client, so a lane can
                    # kill the backend inside the ambiguity window.
                    self._notify(rule, self.command, self.path)
                if rule.action == "drop_response":
                    # Ambiguous outcome: the upstream processed the request,
                    # but the client never sees the response.
                    try:
                        self.connection.close()
                    except OSError:
                        pass
                    return

            if outcome is not None:
                status, content_type, resp_body = outcome
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)

        def _upstream(self, body: bytes) -> tuple[int, str, bytes] | None:
            """Forward to the upstream; on transport failure respond 502 and
            return None (the response has already been sent)."""
            url = upstream + self.path
            req = urllib.request.Request(url, data=body if body else None, method=self.command)
            for header in ("Content-Type", "Accept"):
                if self.headers.get(header):
                    req.add_header(header, self.headers[header])
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.status, resp.headers.get("Content-Type", "application/json"), resp.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.headers.get("Content-Type", "application/json"), exc.read()
            except (TimeoutError, urllib.error.URLError, OSError) as exc:
                self._send_json(502, {"fault": "upstream-unreachable", "error": str(exc)})
                return None

        def log_message(self, fmt: str, *args) -> None:  # quiet default logging
            sys.stderr.write("[faultproxy] " + fmt % args + "\n")

    return FaultProxyHandler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--upstream", required=True, help="mint base URL, e.g. http://127.0.0.1:8080")
    parser.add_argument("--listen", default="127.0.0.1:9090", help="host:port to listen on")
    parser.add_argument("--rules", help="optional JSON file with initial rules")
    args = parser.parse_args()

    if args.rules:
        with open(args.rules) as fh:
            specs = json.load(fh)
        STATE.set_rules(specs if isinstance(specs, list) else specs.get("rules", []))

    host, _, port = args.listen.rpartition(":")
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), make_handler(args.upstream.rstrip("/")))
    print(f"faultproxy listening on {args.listen} -> {args.upstream}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
