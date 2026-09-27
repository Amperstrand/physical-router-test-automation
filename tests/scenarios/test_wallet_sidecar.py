"""Wallet sidecar (process-isolated Cashu wallet) — physical router tests.

Deploys a wallet-sidecar **daemon** and a **client** to the router and drives the
`WalletPort` sidecar RPC over an AF_UNIX socket. This is the process-isolated
wallet architecture: the Go service talks to a wallet daemon, so no Cashu library
is linked into the service and CGO stays off.

Binaries are supplied via env (must match the router arch):

    WALLET_DAEMON_BIN   path to ``cdk-walletd`` (aarch64 / mipsel)
    WALLET_CLIENT_BIN   path to the sidecar client that speaks the RPC
    WALLET_SIDECAR_MINT mint URL (default https://testnut.cashu.space)

Offline tests (manifest, balance, error handling, disconnect resilience) always
run. Value-moving tests (mint/send/receive) **skip** when the router cannot reach
the mint (fresh router with no upstream).

Run:
    WALLET_DAEMON_BIN=/path/cdk-walletd-aarch64 \\
    WALLET_CLIENT_BIN=/path/cdkinterop-arm64 \\
    TOLLGATE_SSH_HOST=192.168.1.1 TOLLGATE_SSH_PASSWORD=... \\
    pytest tests/scenarios/test_wallet_sidecar.py -v -s
"""
import json
import os
import time

import pytest

pytestmark = [pytest.mark.api, pytest.mark.hardware, pytest.mark.timeout(300)]

DAEMON_BIN = os.environ.get("WALLET_DAEMON_BIN", "")
CLIENT_BIN = os.environ.get("WALLET_CLIENT_BIN", "")
MINT = os.environ.get("WALLET_SIDECAR_MINT", "https://testnut.cashu.space")

SOCK = "/tmp/tollgate-wallet.sock"
RDAEMON = "/tmp/cdk-walletd"
RCLIENT = "/tmp/cdkinterop"
WORKDIR = "/tmp/tollgate-wallet"


def run_client(router, *args, timeout=90):
    """Run the sidecar client on the router; return the parsed JSON line."""
    out = router.ssh(f"{RCLIENT} {SOCK} {args_join(args)}", timeout=timeout)
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    if not lines:
        return {"_raw": out, "ok": False}
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return {"_raw": out, "ok": False}


def args_join(args):
    return " ".join("" if a is None else str(a) for a in args)


def _mint_reachable(router):
    """A mint quote that fails with a transport/DNS error means unreachable."""
    r = run_client(router, "mintquote")
    if r.get("ok"):
        return True
    err = (r.get("error") or "").lower()
    return not any(k in err for k in ("transport", "no route", "lookup", "refused", "timed out"))


@pytest.fixture(scope="module")
def sidecar(router):
    if not DAEMON_BIN or not os.path.isfile(DAEMON_BIN):
        pytest.skip("set WALLET_DAEMON_BIN to the router-arch cdk-walletd binary")
    if not CLIENT_BIN or not os.path.isfile(CLIENT_BIN):
        pytest.skip("set WALLET_CLIENT_BIN to the router-arch client binary")

    router.scp_to(DAEMON_BIN, RDAEMON)
    router.ssh(f"chmod +x {RDAEMON}")
    router.scp_to(CLIENT_BIN, RCLIENT)
    router.ssh(f"chmod +x {RCLIENT}")

    router.ssh(f"rm -f {SOCK}; mkdir -p {WORKDIR}; kill $(pidof cdk-walletd) 2>/dev/null || true")
    # BusyBox has no nohup; the backgrounded daemon is reparented when the shell
    # exits, so a plain `&` with redirected fds is sufficient.
    router.ssh(
        f"{{ {RDAEMON} --socket {SOCK} --work-dir {WORKDIR} --mint {MINT} "
        f"</dev/null >/tmp/cdk-walletd.log 2>&1 & }} ; echo started"
    )
    for _ in range(40):
        if "srwx" in router.ssh(f"ls -l {SOCK} 2>&1"):
            break
        time.sleep(1)
    else:
        log = router.ssh("cat /tmp/cdk-walletd.log 2>&1")
        pytest.fail(f"wallet sidecar daemon did not create its socket; log: {log}")

    yield router

    router.ssh("kill $(pidof cdk-walletd) 2>/dev/null || true")


class TestWalletSidecarOffline:
    """Always-run tests (no mint needed)."""

    def test_info_manifest(self, sidecar):
        r = run_client(sidecar, "info")
        assert r.get("ok") is True, r
        assert r["backend"] == "cdk"
        assert r["kind"] == "sidecar"
        assert r["licence"], "manifest must advertise a licence"
        assert "aarch64_cortex-a53" in r["arches"]

    def test_balance_is_integer(self, sidecar):
        r = run_client(sidecar, "balance")
        assert r.get("ok") is True, r
        assert isinstance(r["balance"], int)

    def test_decode_bad_token_is_structured_error(self, sidecar):
        r = run_client(sidecar, "decode", "not-a-token")
        assert r.get("ok") is False
        assert "decode" in (r.get("error") or "").lower()

    def test_client_disconnects_do_not_kill_daemon(self, sidecar):
        # Each client invocation opens + closes the socket; the daemon must stay up.
        for _ in range(3):
            assert run_client(sidecar, "balance").get("ok") is True

    def test_missing_socket_is_structured_error(self, sidecar):
        out = sidecar.ssh(f"{RCLIENT} /tmp/does-not-exist.sock info", timeout=30)
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        r = json.loads(lines[-1])
        assert r.get("ok") is False
        assert "not connected" in (r.get("error") or "").lower()


class TestWalletSidecarValueFlow:
    """Mint-dependent tests; skip when the router cannot reach the mint."""

    def test_mint_quote_then_mint(self, sidecar):
        if not _mint_reachable(sidecar):
            pytest.skip("router cannot reach the mint (no upstream?)")
        q = run_client(sidecar, "mintquote", 100)
        assert q.get("ok") is True, q
        assert q.get("has_request") is True
        assert q.get("quote_id"), "mint quote must carry an id for polling"

        # Poll until the (test) mint settles, then mint.
        state = None
        for _ in range(15):
            st = run_client(sidecar, "mqstate", q["quote_id"])
            if st.get("ok"):
                state = st["state"]
                if state in ("PAID", "ISSUED"):
                    break
            time.sleep(2)
        if state not in ("PAID", "ISSUED"):
            pytest.skip(f"mint did not settle the quote (state={state})")

        before = run_client(sidecar, "balance").get("balance", 0)
        m = run_client(sidecar, "mint", q["quote_id"])
        assert m.get("ok") is True, m
        after = run_client(sidecar, "balance").get("balance", 0)
        assert after >= before + 100, f"balance did not increase: {before} -> {after}"

    def test_send_receive_roundtrip(self, sidecar):
        if not _mint_reachable(sidecar):
            pytest.skip("router cannot reach the mint (no upstream?)")
        bal = run_client(sidecar, "balance").get("balance", 0)
        if bal < 20:
            pytest.skip(f"wallet balance too low for a round-trip ({bal})")

        sent = run_client(sidecar, "send", 10)
        assert sent.get("ok") is True, sent
        token = sent.get("token")
        assert token and token.startswith("cashu"), "send must return a Cashu token"

        recv = run_client(sidecar, "receive", token)
        assert recv.get("ok") is True, recv
        # receive credits the post-swap-fee amount (<= face value).
        assert 0 < recv["received"] <= 10, recv

    def test_double_spend_rejected(self, sidecar):
        """A token must not be credited twice (double-spend rejection)."""
        if not _mint_reachable(sidecar):
            pytest.skip("router cannot reach the mint (no upstream?)")
        if run_client(sidecar, "balance").get("balance", 0) < 20:
            pytest.skip("wallet balance too low")
        token = run_client(sidecar, "send", 10).get("token")
        assert token, "send failed"
        first = run_client(sidecar, "receive", token)
        assert first.get("ok") is True, first
        second = run_client(sidecar, "receive", token)
        assert second.get("ok") is False, f"double-spend was accepted: {second}"


class TestWalletSidecarSecurityParity:
    """T15 parity: the gonuts fork carries two funds-safety fixes any replacement
    must reproduce (research/wallet-migration experiments/parity). The exact fork
    scenarios need a controllable mint and fault injection; they are recorded
    here as skipped acceptance cases so the gate is explicit.
    """

    def test_reject_token_from_other_mint(self, sidecar):
        """A token for a mint the wallet is not configured for must not be
        credited (no cross-mint value)."""
        if not _mint_reachable(sidecar):
            pytest.skip("router cannot reach the mint (no upstream?)")
        if run_client(sidecar, "balance").get("balance", 0) < 20:
            pytest.skip("wallet balance too low")

        token = run_client(sidecar, "send", 10).get("token")
        assert token, "send failed"

        # Second daemon configured for a DIFFERENT mint; feed it the token.
        sock2 = "/tmp/tollgate-wallet2.sock"
        sidecar.ssh(f"rm -f {sock2}; rm -rf /tmp/tollgate-wallet2; mkdir -p /tmp/tollgate-wallet2")
        sidecar.ssh(
            f"{{ {RDAEMON} --socket {sock2} --work-dir /tmp/tollgate-wallet2 "
            f"--mint https://mint.example.invalid </dev/null >/tmp/cdk-walletd2.log 2>&1 & }} ; echo started"
        )
        for _ in range(20):
            if "srwx" in sidecar.ssh(f"ls -l {sock2} 2>&1"):
                break
            time.sleep(1)

        recv_out = sidecar.ssh(f"{RCLIENT} {sock2} receive {token}", timeout=60)
        bal_out = sidecar.ssh(f"{RCLIENT} {sock2} balance", timeout=30)
        sidecar.ssh("kill $(pgrep -f 'tollgate-wallet2[.]sock') 2>/dev/null || true")

        def last_json(s):
            lines = [ln for ln in s.strip().splitlines() if ln.strip().startswith("{")]
            return json.loads(lines[-1]) if lines else {}

        res = last_json(recv_out)
        assert res.get("ok") is False, f"a foreign-mint token was accepted: {res}"
        assert last_json(bal_out).get("balance", 0) == 0, "foreign-mint token changed the balance"

    def test_signature_enforcement_p2pk(self, sidecar):
        """A P2PK-locked token (NUT-11) must not be spendable without the key —
        the signature-enforcement property behind the gonuts fork fix 296c7bf."""
        if not _mint_reachable(sidecar):
            pytest.skip("router cannot reach the mint (no upstream?)")
        if run_client(sidecar, "balance").get("balance", 0) < 20:
            pytest.skip("wallet balance too low")

        # Lock to the secp256k1 generator G (nobody here holds its key).
        g_pubkey = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
        sent = run_client(sidecar, "send_p2pk", 5, g_pubkey)
        assert sent.get("ok") is True and sent.get("token"), f"send_p2pk failed: {sent}"

        # A second wallet (same mint, different seed) must not be able to spend it.
        sock2 = "/tmp/tollgate-wallet-p2pk.sock"
        sidecar.ssh(f"rm -f {sock2}; rm -rf /tmp/tollgate-wallet-p2pk; mkdir -p /tmp/tollgate-wallet-p2pk")
        sidecar.ssh(
            f"{{ {RDAEMON} --socket {sock2} --work-dir /tmp/tollgate-wallet-p2pk "
            f"--mint {MINT} </dev/null >/tmp/cdk-walletd-p2pk.log 2>&1 & }} ; echo started"
        )
        for _ in range(20):
            if "srwx" in sidecar.ssh(f"ls -l {sock2} 2>&1"):
                break
            time.sleep(1)
        out = sidecar.ssh(f"{RCLIENT} {sock2} receive {sent['token']}", timeout=60)
        bal = sidecar.ssh(f"{RCLIENT} {sock2} balance", timeout=30)
        sidecar.ssh("kill $(pgrep -f 'tollgate-wallet-p2pk[.]sock') 2>/dev/null || true")

        def last_json(s):
            lines = [ln for ln in s.strip().splitlines() if ln.strip().startswith("{")]
            return json.loads(lines[-1]) if lines else {}

        res = last_json(out)
        assert res.get("ok") is False, f"a P2PK-locked token was spent without the key: {res}"
        assert last_json(bal).get("balance", 0) == 0, "locked token changed the receiver balance"

    def test_swap_proof_loss(self, sidecar):
        pytest.skip(
            "T15: reproduce gonuts fork fix 7dc430b — an interrupted swap must not "
            "lose proofs (fault injection)"
        )
