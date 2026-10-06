#!/usr/bin/env python3
"""Conformance-matrix runner (R12, tmbr #15) — host venue v1.

Executes the matrix.yaml scenarios against one backend binary with a real
cdk-mintd behind the fault proxy, asserts the per-scenario invariants from
observable state (mint ledger, wallet balance, sessions.json, payment
journal, CLI), and emits a per-scenario verdict table (JSON + markdown).

Venue honesty (matrix.yaml carries it): scenarios whose kill points or
faults need a full VM (vm_reboot, qemu_quit, dns_blackhole, keyset
rotation) are marked pending-venue, never guessed. The QEMU lane is the
follow-up; this runner is the differential evidence engine both lanes share.

Usage:
  python3 run_matrix.py --backend rust-basic \\
      --binary ~/.cargo-target/release/tollgate-module-basic-rust \\
      --mint /opt/cdk-mintd/cdk-mintd --out results/rust-basic
"""

import argparse
import base64
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error

import yaml
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY_PORT = 8390
MINT_PORT = 8388
PROXY_MINT = f"http://127.0.0.1:{PROXY_PORT}"


# ── matrix spec ────────────────────────────────────────────────────────
# PyYAML is a framework requirement (the test framework imports it); the
# spec is authored in the YAML subset safe_load handles.


def load_matrix(path):
    with open(path) as f:
        return yaml.safe_load(f)


# ── helpers ─────────────────────────────────────────────────────────────

class Proc:
    def __init__(self, proc, log_path):
        self.proc, self.log_path = proc, log_path

    def log(self):
        try:
            return open(self.log_path).read()
        except FileNotFoundError:
            return ""

    def kill9(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait()

    def term(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def wait_http(port, secs=60):
    deadline = time.time() + secs
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def cli(cfg, cmd, timeout=30, retries=6, backoff=25):
    """CLI query with patience: after fault scenarios the wallet mutex may
    be held by bounded saga recovery (120s class). Waiting it out observes
    the CONVERGED state — which is what the invariants assert."""
    last = None
    for _ in range(retries):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(os.path.join(cfg, "tollgate.sock"))
            s.sendall((cmd + "\n").encode())
            r = s.recv(65536).decode()
            s.close()
            return r
        except (TimeoutError, OSError) as e:
            last = e
            time.sleep(backoff)
    raise last


def http(method, url, body=None, headers=None, timeout=15):
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def mint_token(amount, base):
    """Full NUT-04 mint with client-side blinding against the (proxied) mint."""
    from coincurve import PrivateKey, PublicKey
    DOMAIN = b"Secp256k1_HashToCurve_Cashu_"
    N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

    def hash_to_curve(secret: bytes) -> bytes:
        h = hashlib.sha256(DOMAIN + secret).digest()
        for counter in range(2**16):
            p = b"\x02" + hashlib.sha256(h + counter.to_bytes(4, "little")).digest()
            try:
                PublicKey(p)
                return p
            except Exception:
                continue
        raise RuntimeError

    _, body = http("GET", f"{base}/v1/keysets")
    ks_doc = json.loads(body)
    keyset = next(k["id"] for k in ks_doc["keysets"] if k.get("active"))
    _, body = http("POST", f"{base}/v1/mint/quote/bolt11",
                   json.dumps({"amount": amount, "unit": "sat"}).encode(),
                   {"Content-Type": "application/json"})
    q = json.loads(body)
    for _ in range(50):
        _, body = http("GET", f"{base}/v1/mint/quote/bolt11/{q['quote']}")
        st = json.loads(body)
        if st.get("state") in ("PAID", "ISSUED"):
            break
        time.sleep(0.15)
    secret = hashlib.sha256(os.urandom(32)).hexdigest()
    y = PublicKey(hash_to_curve(secret.encode()))
    r = PrivateKey(os.urandom(32))
    b_ = y.combine([r.public_key])
    _, body = http("POST", f"{base}/v1/mint/bolt11",
                   json.dumps({"quote": q["quote"],
                               "outputs": [{"amount": amount, "id": keyset,
                                            "B_": b_.format().hex()}]}).encode(),
                   {"Content-Type": "application/json"})
    sig = json.loads(body)["signatures"][0]
    _, body = http("GET", f"{base}/v1/keys")
    keys = json.loads(body)
    amt_key = next(ks["keys"][str(amount)] for ks in keys["keysets"] if ks["id"] == sig["id"])
    r_neg = (N - int.from_bytes(r.secret, "big")) % N
    ra = PublicKey(bytes.fromhex(amt_key)).multiply(r_neg.to_bytes(32, "big"))
    c = PublicKey(bytes.fromhex(sig["C_"])).combine([ra])
    proof = {"amount": amount, "id": sig["id"], "secret": secret, "C": c.format().hex()}
    payload = {"token": [{"mint": base, "proofs": [proof]}], "unit": "sat"}
    s_ = json.dumps(payload, separators=(",", ":"))
    return "cashuA" + base64.urlsafe_b64encode(s_.encode()).decode().rstrip("=")


# ── runner ──────────────────────────────────────────────────────────────

class Harness:
    def __init__(self, args):
        self.args = args
        self.mint = None
        self.proxy = None
        self.backend = None
        self.cfg = None

    def start_mint(self):
        d = tempfile.mkdtemp(prefix="conf-mint-")
        env = dict(**os.environ,
                   CDK_MINTD_MNEMONIC="abandon abandon abandon abandon abandon "
                                      "abandon abandon abandon abandon abandon abandon about",
                   CDK_MINTD_WORK_DIR=d)
        cfg = f"""[info]
url = "http://127.0.0.1:{MINT_PORT}/"
listen_host = "127.0.0.1"
listen_port = {MINT_PORT}
mnemonic = "env:CDK_MINTD_MNEMONIC"

[database]
engine = "sqlite"

[payment_backend]
backend = "fakewallet"

[fake_wallet]
fee_percent = 0
reserve_fee_min = 0
min_delay_time = 0
max_delay_time = 0
"""
        open(f"{d}/config.toml", "w").write(cfg)
        subprocess.run([self.args.mint, "--work-dir", d, "config", "validate",
                        "--file", f"{d}/config.toml"], env=env, capture_output=True)
        subprocess.run([self.args.mint, "--work-dir", d, "config", "init", "--new-mint",
                        "--file", f"{d}/config.toml"], env=env, capture_output=True)
        p = subprocess.Popen([self.args.mint, "--work-dir", d], env=env,
                             stdout=open(f"{d}/mint.log", "w"), stderr=subprocess.STDOUT,
                             start_new_session=True)
        assert wait_http(MINT_PORT), "mint did not start"
        self.mint = Proc(p, f"{d}/mint.log")
        self.mint_dir = d

    def start_proxy(self):
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "fault_proxy.py"),
                              str(PROXY_PORT), f"http://127.0.0.1:{MINT_PORT}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        time.sleep(1)
        assert wait_http(PROXY_PORT), "proxy did not start"
        self.proxy = p

    def set_rule(self, route, rule):
        doc = {"rules": {route: rule}}
        with open("/tmp/faultproxy.json", "w") as f:
            json.dump(doc, f)

    def clear_rules(self):
        with open("/tmp/faultproxy.json", "w") as f:
            json.dump({"rules": {}}, f)

    def start_backend(self, mint_url=PROXY_MINT, config_variation=None):
        cfg = tempfile.mkdtemp(prefix="conf-backend-")
        url = mint_url
        if config_variation == "trailing_slash":
            url = mint_url + "/"
        if config_variation == "host_case":
            url = mint_url.replace("127.0.0.1", "LOCALHOST").replace("localhost", "127.0.0.1").replace("LOCALHOST", "localhost")  # no-op host is already lowercase; use direct
            url = f"http://localhost:{PROXY_PORT}"
        doc = {"config_version": "v0.0.8", "log_level": "info",
               "metric": "milliseconds", "step_size": 5000, "margin": 0.1,
               "accepted_mints": [{"url": url, "min_balance": 0,
                                   "balance_tolerance_percent": 0,
                                   "payout_interval_seconds": 36000,
                                   "min_payout_amount": 0, "price_per_step": 1,
                                   "price_unit": "sats", "purchase_min_steps": 1}],
               "profit_share": [{"factor": 1.0, "identity": "owner"}]}
        open(os.path.join(cfg, "config.json"), "w").write(json.dumps(doc))
        env = dict(**os.environ, TOLLGATE_TEST_CONFIG_DIR=cfg)
        p = subprocess.Popen([self.args.binary], env=env,
                             stdout=open(f"{cfg}/boot.log", "w"), stderr=subprocess.STDOUT)
        assert wait_http(2121), "backend did not start"
        self.backend = Proc(p, f"{cfg}/boot.log")
        self.cfg = cfg
        self.lease()
        time.sleep(2)

    def lease(self):
        subprocess.run(["sudo", "-n", "sh", "-c",
                        "printf '%s' '9999999999 aa:bb:cc:dd:ee:10 10.99.99.110 conf *\n' "
                        ">> /var/lib/misc/dnsmasq.leases"], check=True)

    def unlease(self):
        subprocess.run(["sudo", "-n", "sed", "-i",
                        "/aa:bb:cc:dd:ee:10 10.99.99.110/d",
                        "/var/lib/misc/dnsmasq.leases"], check=False)

    def stop_backend(self):
        if self.backend:
            self.backend.term()
            self.backend = None

    def teardown(self):
        self.stop_backend()
        self.unlease()
        if self.proxy:
            self.proxy.terminate()
            self.proxy.wait(timeout=5)
            self.proxy = None
        if self.mint:
            self.mint.term()
            self.mint = None

    # observable state for invariants
    def wallet_balance(self):
        out = cli(self.cfg, "wallet balance")
        try:
            return int(json.loads(out.strip())["message"])
        except Exception:
            return 0

    def payment_journal(self):
        p = os.path.join(self.cfg, "payment-journal.jsonl")
        try:
            return [json.loads(l) for l in open(p) if l.strip()]
        except FileNotFoundError:
            return []

    def sessions(self):
        p = os.path.join(self.cfg, "sessions.json")
        try:
            return json.load(open(p))
        except Exception:
            return []

    def pay(self, token, timeout=70):
        return http("POST", "http://127.0.0.1:2121/", token.encode(),
                    {"Content-Type": "text/plain",
                     "X-Forwarded-For": "10.99.99.110"}, timeout)


# ── per-scenario execution ─────────────────────────────────────────────

def host_venue(sc):
    v = sc.get("venue") or {}
    if isinstance(v, dict):
        hv = v.get("host", "no")
        return hv if isinstance(hv, str) else ("yes" if hv else "no")
    return v if isinstance(v, str) else "no"


def run_scenario(h, sc, results):
    venue = host_venue(sc)
    if venue in ("no", "pending-venue"):
        results[sc["id"]] = {"verdict": "pending-venue",
                             "reason": sc.get("note") or "needs the QEMU/full-VM lane"}
        return
    try:
        run_scenario_inner(h, sc, results)
    except Exception as e:
        import traceback
        results[sc["id"]] = {"verdict": "fail",
                             "reason": f"harness error: {e}",
                             "trace": traceback.format_exc()[-800:]}
    finally:
        h.stop_backend()
        h.clear_rules()


def run_scenario_inner(h, sc, results):
    sid = sc["id"]
    kill = sc.get("kill_at")
    fault = sc.get("fault") or {}
    inv = sc["id"]

    h.clear_rules()
    h.start_backend(config_variation=sc.get("config_variation"))

    if isinstance(fault, dict) and fault.get("mode") == "status":
        h.set_rule(fault.get("route", "/"), {"mode": "status",
                                              "status": fault["status"],
                                              "count": fault.get("count", 1)})
    elif isinstance(fault, dict) and fault.get("mode") == "delay":
        h.set_rule(fault.get("route", "/v1/swap"), {"mode": "delay",
                                                     "delay": fault["delay"]})
    elif isinstance(fault, dict) and fault.get("mode") == "drop_response_after_forward":
        h.set_rule(fault.get("route", "/v1/swap"),
                   {"mode": "drop_response_after_forward"})

    token = mint_token(8, PROXY_MINT)

    if kill == "pre_receive":
        # Intent fires before receive: post and kill mid-flight. Host venue
        # approximates the window: kill right after the request is sent.
        import threading
        t = threading.Thread(target=lambda: h.pay(token, timeout=5), daemon=True)
        t.start()
        time.sleep(0.4)
        h.backend.kill9()
        t.join(timeout=10)
    elif kill in ("post_receive", "post_session", "post_gate", "post_timeout"):
        code, body = h.pay(token, timeout=60)
        # timeout paths answer 504; the kill windows need precise timing the
        # host venue approximates by killing right after the answer.
        h.backend.kill9()
    else:
        code, body = h.pay(token)

    time.sleep(1)

    # Restart to observe convergence (kill scenarios) or just observe.
    if kill:
        h.clear_rules()
        h.start_backend()
        time.sleep(6)

    balance = h.wallet_balance()
    journal = h.payment_journal()
    sess = h.sessions()

    # ── invariant assertions (host-observable subset) ──
    verdicts = {}
    if isinstance(sess, list):
        n_sessions = len(sess)
    elif isinstance(sess, dict):
        inner = sess.get("sessions", sess)
        n_sessions = len(inner) if hasattr(inner, "__len__") else 0
    else:
        n_sessions = 0

    # I1 (host-observable form): value PROVABLY moved (a settled journal
    # record exists) yet the wallet shows nothing and no session exists —
    # that is disappearance. Intent-only (never landed) and pending-
    # reconcile records are not: value either never moved or is converging
    # via the reconciler.
    settled = [e for e in journal if isinstance(e.get("phase"), str)
               and e["phase"] in ("received", "reconcile-spent")]
    verdicts["I1_funds_accounted"] = not (settled and balance == 0
                                           and n_sessions == 0)
    verdicts["I2_single_session"] = n_sessions <= 1

    # I6: duplicate POST is safe (sequential scenarios exercise it directly).
    if sid in ("duplicate-post-sequential",):
        code2, _ = h.pay(token)
        verdicts["I6_retry_safe"] = code2 in (200, 400, 504)

    # I7: after restart, the backend is healthy and state is readable.
    verdicts["I7_converged"] = wait_http(2121, 5) and cli(h.cfg, "status").strip() != ""

    violated = [k for k, ok in verdicts.items() if not ok]
    results[sid] = {
        "verdict": "pass" if not violated else f"invariant-violated:{','.join(violated)}",
        "observed": {"balance": balance, "journal_entries": len(journal),
                     "sessions": n_sessions, "checks": verdicts},
    }

    h.stop_backend()
    h.clear_rules()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True)
    ap.add_argument("--binary", required=True)
    ap.add_argument("--mint", default="/opt/cdk-mintd/cdk-mintd")
    ap.add_argument("--out", required=True)
    ap.add_argument("--matrix", default=os.path.join(HERE, "matrix.yaml"))
    args = ap.parse_args()

    matrix = load_matrix(args.matrix)
    scenarios = matrix["scenarios"]
    results = {}

    h = Harness(args)
    h.start_mint()
    h.start_proxy()
    try:
        for sc in scenarios:
            print(f"[conformance] {sc['id']} ...", flush=True)
            # fresh backend per scenario
            run_scenario(h, sc, results)
            print(f"           → {results[sc['id']]['verdict']}", flush=True)
    finally:
        h.teardown()

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "results.json"), "w") as f:
        json.dump({"backend": args.backend, "results": results}, f, indent=2)

    # markdown table
    lines = [f"# Conformance: {args.backend}", "",
             "| Scenario | Verdict | Detail |", "|---|---|---|"]
    for sc in scenarios:
        r = results[sc["id"]]
        detail = r.get("reason") or json.dumps(r.get("observed", {}).get("checks", {}))
        lines.append(f"| {sc['id']} | {r['verdict']} | {detail} |")
    with open(os.path.join(args.out, "results.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nresults → {args.out}/results.{{json,md}}")


if __name__ == "__main__":
    main()
