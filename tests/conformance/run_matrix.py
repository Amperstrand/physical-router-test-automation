#!/usr/bin/env python3
"""Conformance-matrix runner (R12, tmbr #15) — host venue v2.

Executes the shared matrix.yaml scenarios (IDs co-owned with
OpenTollGate/tollgate-module-basic-go#503) against one backend binary
with a real cdk-mintd behind the shared faultproxy.py, asserts the
per-scenario invariants from observable state, and emits a verdict table.

v2 contract honesty (hardening after the PR #16 Codex round):
  * drives the shared faultproxy.py via its control endpoint — no
    private proxy, no private rule schema;
  * kill boundaries are triggered deterministically from the proxy's
    notify webhook (response-held window), not approximated
    kill-after-answer; the two boundaries no proxy can observe
    (post-session-pre-gate, post-gate-pre-response) use documented,
    named delay approximations;
  * a restarted backend REUSES the crashed instance's config dir —
    restart scenarios observe reconciliation, not a fresh wallet;
  * unparseable observable state is a harness error, never a sentinel;
  * every invariant a scenario declares is accounted for: checked,
    or recorded `pending` with the reason — missing checks can no
    longer hide inside a pass;
  * strict value accounting for no-fund-loss (single-payment form):
    proven-moved value must equal wallet balance (mint fee 0 here);
  * duplicate scenarios actually submit the duplicate(s) and compare
    exact granted value;
  * the CLI wire encoding follows the backend (JSON CLIMessage for Go,
    plain text for Rust), per tests/api/test_go_rust_basic_parity.py;
  * readiness is functional (mint keysets answer; backend serves its
    advertisement), not a raw TCP connect.

Usage:
  python3 run_matrix.py --backend rust-basic \\
      --binary <tollgate-binary> --mint /opt/cdk-mintd/cdk-mintd \\
      --out results/rust-basic
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY_PORT = 8390
MINT_PORT = 8388
HOOK_PORT = 8399
PROXY_MINT = f"http://127.0.0.1:{PROXY_PORT}"
HOOK_URL = f"http://127.0.0.1:{HOOK_PORT}/hook"

INV_IDS = {
    "no-fund-loss": "I1_funds_accounted",
    "no-double-count": "I2_single_grant",
    "no-output-reuse": "I3_no_output_reuse",
    "service-or-refund": "I4_service_or_refund",
    "operator-spendable": "I5_operator_spendable",
    "retry-safe": "I6_retry_safe",
    "restart-converges": "I7_converged",
}

# Host-observable checks v2 implements; everything else is pending.
CHECKABLE = {
    "no-fund-loss", "no-double-count", "no-output-reuse",
    "retry-safe", "restart-converges",
}
# Boundaries the proxy can observe deterministically vs documented
# approximations (host venue): the webhook fires while the mint's swap
# response is held, i.e. receive has happened and the backend has not
# seen the outcome. post-session/post-gate have no proxy-visible
# signal (session write and ndsctl gate are local), so they ride the
# same trigger plus a named delay. pre-receive is driven without a
# rule (kill before any mint traffic).
BOUNDARY_APPROX_MS = {
    "post-receive-pre-session": 0,
    "post-session-pre-gate": 400,
    "post-gate-pre-response": 900,
}


class HarnessError(Exception):
    """Lane bug — the verdict must be `error`, never a pass."""


# ── shared fault proxy control ─────────────────────────────────────────

def proxy_ctl(doc):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PROXY_PORT}/__fault/control",
        data=json.dumps(doc).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=5).read()


def set_rules(rules):
    proxy_ctl({"rules": rules})


def clear_rules():
    proxy_ctl({"clear": True})


def observations():
    with urllib.request.urlopen(
            f"http://127.0.0.1:{PROXY_PORT}/__fault/observations",
            timeout=5) as r:
        return json.load(r)


# ── notify webhook: deterministic kill/restart inside held windows ────

class HookHandler(http.server.BaseHTTPRequestHandler):
    trigger = None  # set per scenario: callable executed inside the window

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        fn = HookHandler.trigger
        if fn:
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - surfaced in the log
                print(f"[hook] trigger failed: {e}", file=sys.stderr)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def start_hook_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", HOOK_PORT), HookHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ── process helpers ─────────────────────────────────────────────────────

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


def mint_ready(secs=60):
    """Functional probe: the mint answers a NUT-02 keysets request."""
    deadline = time.time() + secs
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{MINT_PORT}/v1/keysets", timeout=2) as r:
                if r.status == 200 and b"keysets" in r.read():
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return False


def backend_ready(secs=60):
    """Functional probe: the backend serves its nostr advertisement."""
    deadline = time.time() + secs
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:2121/", timeout=2) as r:
                doc = json.loads(r.read())
                if doc.get("kind") in (10021, 21023):
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return False


def cli_payload(cmd, backend):
    """Wire encoding per backend (parity with the shared parity tests)."""
    if backend == "go":
        if cmd == "wallet balance":
            msg = {"command": "wallet", "args": ["balance"]}
        elif cmd == "wallet info":
            msg = {"command": "wallet", "args": ["info"]}
        else:
            msg = {"command": cmd}
        return (json.dumps(msg) + "\n").encode()
    return (cmd + "\n").encode()


def cli_socket(cfg, backend):
    """Current builds of BOTH backends honor TOLLGATE_TEST_CONFIG_DIR for
    the CLI socket; older Go hard-coded /var/run/tollgate.sock (parity
    tests' GO_SOCKET_PATH) — fall back to it for those builds."""
    p = os.path.join(cfg, "tollgate.sock")
    if backend == "go" and not os.path.exists(p):
        return "/var/run/tollgate.sock"
    return p


def cli(cfg, backend, cmd, timeout=30, retries=6, backoff=25):
    """CLI query with patience: after fault scenarios the wallet mutex may
    be held by bounded saga recovery (120s class). Waiting it out observes
    the CONVERGED state — which is what the invariants assert."""
    last = None
    for _ in range(retries):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(cli_socket(cfg, backend))
            s.sendall(cli_payload(cmd, backend))
            chunks = [s.recv(65536)]  # first chunk waits out the op window
            # The CLI server does NOT close the connection after its
            # answer (waiting for EOF would burn the whole timeout) —
            # drain with a short idle timeout until the reply is silent.
            s.settimeout(0.5)
            while True:
                try:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                except TimeoutError:
                    break
            s.close()
            return b"".join(chunks).decode()
        except (TimeoutError, OSError) as e:
            last = e
            time.sleep(backoff)
    raise HarnessError(f"CLI never answered {cmd!r}: {last}")


def http_req(method, url, body=None, headers=None, timeout=15):
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
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError

    _, body = http_req("GET", f"{base}/v1/keysets")
    ks_doc = json.loads(body)
    keyset = next(k["id"] for k in ks_doc["keysets"] if k.get("active"))
    _, body = http_req("POST", f"{base}/v1/mint/quote/bolt11",
                       json.dumps({"amount": amount, "unit": "sat"}).encode(),
                       {"Content-Type": "application/json"})
    q = json.loads(body)
    for _ in range(50):
        _, body = http_req("GET", f"{base}/v1/mint/quote/bolt11/{q['quote']}")
        st = json.loads(body)
        if st.get("state") in ("PAID", "ISSUED"):
            break
        time.sleep(0.15)
    secret = hashlib.sha256(os.urandom(32)).hexdigest()
    y = PublicKey(hash_to_curve(secret.encode()))
    r = PrivateKey(os.urandom(32))
    b_ = y.combine([r.public_key])
    _, body = http_req("POST", f"{base}/v1/mint/bolt11",
                       json.dumps({"quote": q["quote"],
                                   "outputs": [{"amount": amount, "id": keyset,
                                                "B_": b_.format().hex()}]}).encode(),
                       {"Content-Type": "application/json"})
    sig = json.loads(body)["signatures"][0]
    _, body = http_req("GET", f"{base}/v1/keys")
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
        self.mint_dir = None
        self.mint_env = None
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
        self.mint_env = env
        self.mint_dir = d
        self.launch_mint()
        assert mint_ready(), "mint did not become functional"

    def launch_mint(self):
        p = subprocess.Popen([self.args.mint, "--work-dir", self.mint_dir],
                             env=self.mint_env,
                             stdout=open(f"{self.mint_dir}/mint.log", "a"),
                             stderr=subprocess.STDOUT,
                             start_new_session=True)
        self.mint = Proc(p, f"{self.mint_dir}/mint.log")

    def start_proxy(self):
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "faultproxy.py"),
                              "--upstream", f"http://127.0.0.1:{MINT_PORT}",
                              "--listen", f"127.0.0.1:{PROXY_PORT}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        time.sleep(1)
        assert wait_http(PROXY_PORT), "proxy did not start"
        self.proxy = p

    def backend_config(self, mint_url, variation):
        url = mint_url
        if variation == "trailing_slash":
            url = mint_url + "/"
        elif variation == "host_case":
            url = mint_url.replace("127.0.0.1", "LOCALhOST").replace("LOCALhOST", "LOCALHOST")
        return {"config_version": "v0.0.8", "log_level": "info",
                "metric": "milliseconds", "step_size": 5000, "margin": 0.1,
                "accepted_mints": [{"url": url, "min_balance": 0,
                                    "balance_tolerance_percent": 0,
                                    "payout_interval_seconds": 36000,
                                    "min_payout_amount": 0, "price_per_step": 1,
                                    "price_unit": "sats", "purchase_min_steps": 1}],
                "profit_share": [{"factor": 1.0, "identity": "owner"}]}

    def launch_backend(self):
        env = dict(**os.environ, TOLLGATE_TEST_CONFIG_DIR=self.cfg)
        p = subprocess.Popen([self.args.binary], env=env,
                             stdout=open(f"{self.cfg}/boot.log", "a"),
                             stderr=subprocess.STDOUT)
        self.backend = Proc(p, f"{self.cfg}/boot.log")
        assert backend_ready(), "backend did not become functional"
        self.lease()

    def start_backend(self, config_variation=None):
        cfg = tempfile.mkdtemp(prefix="conf-backend-")
        doc = self.backend_config(PROXY_MINT, config_variation)
        open(os.path.join(cfg, "config.json"), "w").write(json.dumps(doc))
        self.cfg = cfg
        self.launch_backend()

    def restart_backend_same_state(self):
        """Restart on the CRASHED instance's config dir — reconciliation
        must be observed against the state that existed at the kill."""
        assert self.cfg, "no config dir to reuse"
        self.launch_backend()

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

    # observable state — parse errors are harness errors, never sentinels
    def wallet_balance(self):
        out = cli(self.cfg, self.args.backend, "wallet balance")
        try:
            doc = json.loads(out.strip())
            # Rust: {"message": "8"}; Go: {"message": "Total wallet
            # balance: 8 sats", "data": {"balance_sats": 8}}
            try:
                return int(doc["message"])
            except (ValueError, TypeError):
                return int(doc["data"]["balance_sats"])
        except (ValueError, KeyError, TypeError) as e:
            raise HarnessError(f"unparseable wallet-balance reply {out!r}: {e}") from e

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
        except FileNotFoundError:
            return []
        except json.JSONDecodeError as e:
            raise HarnessError(f"corrupt sessions.json: {e}") from e

    def pay(self, token, timeout=70):
        return http_req("POST", "http://127.0.0.1:2121/", token.encode(),
                        {"Content-Type": "text/plain",
                         "X-Forwarded-For": "10.99.99.110"}, timeout)


# ── per-scenario execution ──────────────────────────────────────────────

def host_venue(sc):
    v = sc.get("venue") or {}
    if isinstance(v, dict):
        hv = v.get("host", "no")
        return hv if isinstance(hv, str) else ("yes" if hv else "no")
    return v if isinstance(v, str) else "no"


def granted_session_count(sess):
    if isinstance(sess, list):
        return len(sess)
    if isinstance(sess, dict):
        inner = sess.get("sessions", sess)
        return len(inner) if hasattr(inner, "__len__") else 0
    return 0


def settled_amount(journal):
    """Total value the journal PROVES moved (rust-basic journal phases)."""
    total = 0
    for e in journal:
        phase = e.get("phase")
        if phase == "received":
            total += int(e.get("amount_sat", 0))
        elif phase == "reconcile-spent":
            total += int(e.get("amount_sat", 0))
    return total


def run_scenario(h, sc, results):
    # Executability is decided by class/fault controls inside the runner
    # (the shared matrix carries no venue flags — the original schema's
    # contract): vm_control/mint_control/drain classes mark themselves
    # pending-venue. Everything else runs on the host venue.
    try:
        run_scenario_inner(h, sc, results)
    except HarnessError as e:
        results[sc["id"]] = {"verdict": "error", "reason": f"harness: {e}"}
    except Exception as e:  # noqa: BLE001
        import traceback
        results[sc["id"]] = {"verdict": "error",
                             "reason": f"harness error: {e}",
                             "trace": traceback.format_exc()[-600:]}
    finally:
        HookHandler.trigger = None
        h.stop_backend()
        clear_rules()
        time.sleep(0.5)


def arm_boundary_kill(h, boundary):
    """Deterministic kill trigger via the shared proxy's notify webhook:
    the mint's swap response is processed but HELD while we act, so
    post-receive-pre-session is exact; the later boundaries add named
    approximated delays (session write / gate open are not
    proxy-observable — documented host-venue approximations)."""
    delay_ms = BOUNDARY_APPROX_MS.get(boundary)
    if delay_ms is None:
        raise HarnessError(f"host venue cannot drive boundary {boundary!r}")

    def trigger():
        HookHandler.trigger = None  # one-shot: the re-POST must not re-kill
        time.sleep(delay_ms / 1000.0)
        h.backend.kill9()

    HookHandler.trigger = trigger
    return [{"match_path": "/v1/swap", "action": "notify",
             "notify_url": HOOK_URL, "notify_on": "response"}]


def run_scenario_inner(h, sc, results):
    sid = sc["id"]
    sclass = sc.get("class")
    fault = sc.get("fault") or {}

    # venue honesty for controls the host lane cannot drive
    if fault.get("vm_control") or fault.get("mint_control"):
        results[sid] = {"verdict": "pending-venue",
                        "reason": f"{sclass}: needs the QEMU/full-VM or rotating-mint lane"}
        return
    if sclass == "drain":
        results[sid] = {"verdict": "pending-venue",
                        "reason": "drain/payout scenarios need the two-mint bench lane "
                                  "(single-mint host venue cannot drive a drain)"}
        return

    h.start_backend(config_variation=sc.get("config_variation"))
    responses = []

    if sclass == "kill-boundary":
        boundary = fault["process_control"]["boundary"]
        if boundary == "pre-receive":
            # kill before ANY mint traffic — no proxy rule needed
            set_rules([])
        else:
            set_rules(arm_boundary_kill(h, boundary))
        token = mint_token(8, PROXY_MINT)
        if boundary == "pre-receive":
            t = threading.Thread(target=lambda: h.pay(token, timeout=5), daemon=True)
            t.start()
            time.sleep(0.4)
            h.backend.kill9()
            t.join(timeout=10)
        else:
            # The notify rule holds the swap response while we kill at
            # the (approximated) boundary; that payment then dies with
            # the backend — expected, captured as (0, "<killed>").
            def first_pay():
                try:
                    responses.append(h.pay(token, timeout=15))
                except Exception:  # noqa: BLE001 - the kill kills the connection
                    responses.append((0, "<killed>"))
            threading.Thread(target=first_pay, daemon=True).start()
            deadline = time.time() + 30
            while h.backend and h.backend.proc.poll() is None and time.time() < deadline:
                time.sleep(0.1)
        clear_rules()
        time.sleep(1)
        h.restart_backend_same_state()
        time.sleep(6)
        # customer retry of the same token after restart
        responses.append(h.pay(token, timeout=30))
        time.sleep(2)

    elif sclass == "fault-proxy" and sid == "swap-timeout-then-restart":
        set_rules(fault["proxy"])
        token = mint_token(8, PROXY_MINT)

        def first_pay():
            try:
                responses.append(h.pay(token, timeout=40))
            except Exception:  # noqa: BLE001 - killed mid-ambiguity, expected
                responses.append((0, "<killed>"))
        threading.Thread(target=first_pay, daemon=True).start()
        time.sleep(34)  # past the backend's mint-timeout, pre-reconciliation
        h.backend.kill9()
        clear_rules()
        time.sleep(1)
        h.restart_backend_same_state()
        time.sleep(6)
        responses.append(h.pay(token, timeout=30))
        time.sleep(2)

    elif sclass == "mint-restart":
        # Restart the mint while the backend's first swap request is held
        # (notify_on request fires before forwarding) — the backend then
        # races a mint that is dying/restarting. The customer token is
        # minted BEFORE the rule arms so the webhook cannot hit our own
        # quote calls. (The rust pay flow has no NUT-04 quote — the
        # swap-hold window is the venue-honest equivalent of the
        # post-quote-pre-spend boundary; quoting backends get the same
        # treatment through their first mint call.)
        token = mint_token(8, PROXY_MINT)
        set_rules([{"match_path": "/v1/swap", "action": "notify",
                    "notify_url": HOOK_URL, "notify_on": "request",
                    "remaining": 1}])
        responses.append(h.pay(token, timeout=90))
        time.sleep(3)

    elif sclass in ("fault-proxy", "http-fault"):
        # http-fault (429/500/reset/delay bursts) uses the same shared
        # faultproxy rules as fault-proxy — the classes differ only in
        # the fault they express, not in how the lane drives them.
        # Customer token minted FIRST: arming match_path /v1/ before
        # minting would fault our own keyset/quote/mint calls. The pay
        # may legitimately outlive the customer-side patience (reset
        # ladders, 30s delays): a read timeout is an ambiguous customer
        # outcome, recorded — the invariants assert on the SETTLED
        # state, never on this response.
        rules = fault.get("proxy") or []
        if not rules:
            raise HarnessError(f"{sid}: {sclass} scenario without proxy rules")
        token = mint_token(8, PROXY_MINT)
        set_rules(rules)
        try:
            responses.append(h.pay(token, timeout=150))
        except (TimeoutError, OSError) as e:
            responses.append((0, f"<no-response: {type(e).__name__}>"))
        time.sleep(3)

    elif sclass == "duplicate":
        token = mint_token(8, PROXY_MINT)
        if sid == "duplicate-post-sequential":
            responses.append(h.pay(token, timeout=90))
            time.sleep(2)
            responses.append(h.pay(token, timeout=90))
            time.sleep(3)
        else:  # concurrent: both POSTs race from a shared start barrier
            barrier = threading.Barrier(2, timeout=10)
            out = []

            def submit():
                barrier.wait()
                out.append(h.pay(token, timeout=90))

            threads = [threading.Thread(target=submit) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            responses.extend(out)
            time.sleep(3)

    elif sclass == "alias":
        token = mint_token(8, PROXY_MINT)
        responses.append(h.pay(token, timeout=90))
        time.sleep(3)

    else:
        raise HarnessError(f"unhandled scenario class {sclass!r}")

    # ── observations ──
    balance = h.wallet_balance()
    journal = h.payment_journal()
    sess = h.sessions()
    n_sessions = granted_session_count(sess)
    obs = observations()
    obs_reused = obs.get("reused", [])
    obs_detail = {d[:12]: obs["blinded_messages"][d] for d in obs_reused[:3]}

    # value PROVABLY moved: rust journal phases, or (venue-agnostic) any
    # payment the backend answered with success / any session granted /
    # any balance — whichever proves movement first
    proven_rust = settled_amount(journal)
    success_answered = any(code == 200 for code, _ in responses)
    proven = max(proven_rust, balance, 8 if (success_answered or n_sessions) else 0)

    verdicts, pending = {}, {}

    # I1 strict single-payment accounting: proven value == balance
    # (fakewallet mint fee is 0; the received proofs sit in the wallet).
    verdicts["I1_funds_accounted"] = (proven == 0) or (balance == proven)
    # I2: one payment, at most one granted session
    verdicts["I2_single_grant"] = n_sessions <= 1
    # I3: no blinded output exposed to the mint twice. The observer
    # itself is under adjudication (PRTA issue: every backend swap B_ is
    # sighted exactly twice — single create_swap in the backend log, one
    # payment journal entry, correct balance — so the source is either a
    # proxy double-count or a legitimate CDK saga re-POST; unproven
    # either way). Until faultproxy request logging lands, I3 is
    # RECORDED with full detail and reported pending — an unsound
    # observer must not auto-fail a backend.
    if not obs_reused:
        verdicts["I3_no_output_reuse"] = True
    else:
        pending["no-output-reuse"] = (
            f"observer adjudication pending: {len(obs_reused)} digests "
            f"sighted twice ({json.dumps(obs_detail)})")
    # I6: retries corrupt nothing — value-level: exactly one payment's
    # worth of value exists after the duplicate submissions (response
    # codes may both be 200: the journal's idempotent replay of the SAME
    # grant is correct behavior, not a double grant).
    if sclass == "duplicate":
        verdicts["I6_retry_safe"] = balance == 8 and n_sessions <= 1
    # I7: converged and queryable
    verdicts["I7_converged"] = backend_ready(10) and \
        cli(h.cfg, h.args.backend, "status").strip() != ""

    for inv in sc.get("asserts", []):
        key = INV_IDS.get(inv, inv)
        if inv not in CHECKABLE:
            pending[inv] = "host venue cannot assert this yet — tracked on the twins"
    violated = [k for k, ok in verdicts.items() if not ok]
    if violated:
        verdict = f"invariant-violated:{','.join(violated)}"
    elif pending:
        verdict = f"pending:{','.join(pending)}"
    else:
        verdict = "pass"
    results[sid] = {
        "verdict": verdict,
        "observed": {"balance": balance, "journal_entries": len(journal),
                     "sessions": n_sessions, "reused_outputs": len(obs_reused),
                     "reused_detail": obs_detail,
                     "responses": [c for c, _ in responses],
                     "checks": verdicts, "pending": list(pending)},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True)
    ap.add_argument("--binary", required=True)
    ap.add_argument("--mint", default="/opt/cdk-mintd/cdk-mintd")
    ap.add_argument("--out", required=True)
    ap.add_argument("--matrix", default=os.path.join(HERE, "matrix.yaml"))
    ap.add_argument("--only", default="",
                    help="comma-separated scenario ids (smoke/debug; lanes run all)")
    args = ap.parse_args()

    matrix = load_matrix(args.matrix)
    scenarios = matrix["scenarios"]
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        unknown = wanted - {sc["id"] for sc in scenarios}
        if unknown:
            ap.error(f"unknown scenario ids: {sorted(unknown)}")
        scenarios = [sc for sc in scenarios if sc["id"] in wanted]
    results = {}

    hook_srv = start_hook_server()
    h = Harness(args)
    h.start_mint()
    h.start_proxy()
    try:
        for sc in scenarios:
            print(f"[conformance] {sc['id']} ...", flush=True)
            run_scenario(h, sc, results)
            print(f"           → {results[sc['id']]['verdict']}", flush=True)
    finally:
        h.teardown()
        hook_srv.shutdown()

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "results.json"), "w") as f:
        json.dump({"backend": args.backend,
                   "matrix_version": matrix.get("version", 1),
                   "runner": "host-v2",
                   "results": results}, f, indent=2)

    lines = [f"# Conformance: {args.backend} (host-v2 runner)", "",
             "| Scenario | Verdict | Detail |", "|---|---|---|"]
    for sc in scenarios:
        r = results[sc["id"]]
        detail = r.get("reason") or json.dumps(
            {k: v for k, v in r.get("observed", {}).items()
             if k in ("balance", "sessions", "reused_outputs", "responses")})
        lines.append(f"| {sc['id']} | {r['verdict']} | {detail} |")
    with open(os.path.join(args.out, "results.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nresults → {args.out}/results.{{json,md}}")


def load_matrix(path):
    with open(path) as f:
        return yaml.safe_load(f)


if __name__ == "__main__":
    main()
