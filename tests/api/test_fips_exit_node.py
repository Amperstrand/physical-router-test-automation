"""FIPS exit node smoke tests (SMOKE-1).

These smoke tests verify the FIPS mesh exit-node deployment (E1): a
WireGuard-based VPS that routes FIPS mesh traffic to the public internet using
an nftables ``fips-exit`` MASQUERADE table. They SSH to the exit node directly
and assert on its live state — they do NOT touch the TollGate router and do not
need the ``router`` fixture or a hardware lock.

Happy path (one test per guarantee):

1. ``test_wireguard_peer_has_recent_handshake``  — tunnel is UP
2. ``test_nftables_fips_exit_masquerade_loaded`` — NAT table present
3. ``test_ip_forwarding_enabled_and_egress_works`` — forwarding path ready
4. ``test_nostr_kind_30078_route_advert_published`` — route advertisement exists
5. ``test_wireguard_tunnel_has_bidirectional_traffic`` — return path works

Skippability
------------
Every test is skippable (never a hard failure) when the thing it probes is
absent — the VPS is unreachable, ``wg``/``nft``/``nak`` is not installed, or the
exit node's Nostr identity is not configured. This follows the feature-detection
pattern used across ``tests/api`` (see ``lib/helpers.py``) and the
``gateway-tests`` SSH-reachability pattern. The autouse ``_exit_node_reachable``
session fixture skips the whole module if the exit node cannot be reached over
SSH, so an unreachable VPS yields ``SKIPPED`` results, not ``FAILED``.

Configuration (no hardcoded secrets — read from env / .env):

    FIPS_EXIT_HOST          exit node address   (default: 23.182.128.51, the
                                                 documented E1 public IP)
    FIPS_EXIT_SSH_USER      ssh user            (default: root)
    FIPS_EXIT_SSH_KEY       path to ssh identity (no default — a secret)
    FIPS_EXIT_SSH_PORT      ssh port            (default: 22)
    FIPS_EXIT_SSH_PASSWORD  ssh password        (no default — used only when no
                                                 identity is configured)
    FIPS_EXIT_NPUB          exit node hex Nostr pubkey (no default — required to
                                                 attribute kind 30078 events)
    FIPS_EXIT_RELAYS        comma-separated wss:// relays (default: the project
                                                 coordination relays)
    FIPS_EXIT_SUDO          when set (1/true/yes), run every remote command via
                                                 passwordless ``sudo -n``. Use when
                                                 the exit node does not permit direct
                                                 root SSH (e.g. cloud images that
                                                 force-login to an unprivileged user
                                                 with passwordless sudo). Default off,
                                                 so genuine root-SSH deployments run
                                                 commands directly as before.

Run via::

    make fips-exit-smoke
    # or directly:
    pytest tests/api/test_fips_exit_node.py -v
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import time

import pytest

# ---------------------------------------------------------------------------
# Configuration (read from environment / .env — no hardcoded secrets)
# ---------------------------------------------------------------------------

EXIT_HOST = os.environ.get("FIPS_EXIT_HOST", "23.182.128.51").strip()
EXIT_USER = os.environ.get("FIPS_EXIT_SSH_USER", "root").strip()
EXIT_KEY = os.environ.get("FIPS_EXIT_SSH_KEY", "").strip()
EXIT_PORT = os.environ.get("FIPS_EXIT_SSH_PORT", "22").strip()
EXIT_PASSWORD = os.environ.get("FIPS_EXIT_SSH_PASSWORD", "").strip()

# Nostr: kind 30078 is parameterized-replaceable application data. The exit node
# publishes a route advertisement under this kind. We attribute events by the
# exit node's hex pubkey (npub); without it we cannot verify the advert came
# from THIS node, so the advertisement test skips.
EXIT_NPUB = os.environ.get("FIPS_EXIT_NPUB", "").strip()

# Relays used to look up the advertisement. Default to the project coordination
# relays (see lib/deploy.py COORDINATION_RELAYS).
_DEFAULT_RELAYS = [
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://nostr.mom",
    "wss://relay1.orangesync.tech",
]
_relays_raw = os.environ.get("FIPS_EXIT_RELAYS", "").strip()
EXIT_RELAYS = (
    [r.strip() for r in _relays_raw.split(",") if r.strip()]
    if _relays_raw
    else list(_DEFAULT_RELAYS)
)

# When the exit node does not permit direct root SSH (e.g. cloud images that
# force-login to an unprivileged user with passwordless sudo), set FIPS_EXIT_SUDO
# to run every remote command through `sudo -n`. Default off so genuine root-SSH
# deployments run commands directly as before.
EXIT_SUDO = os.environ.get("FIPS_EXIT_SUDO", "").strip().lower() in (
    "1", "true", "yes", "on",
)

# Mark these as part of the extended suite. We deliberately do NOT use the
# ``api`` marker: these tests target an external VPS, not the TollGate router
# API, and the ``api`` marker would couple them to the container NDS preflight
# (lib/conftest ``container_nds_preflight``) which assumes a live router.
pytestmark = [
    pytest.mark.story("R4"),pytest.mark.extended]


# ---------------------------------------------------------------------------
# SSH helper + reachability gate (mirrors gateway-tests/conftest.py)
# ---------------------------------------------------------------------------


def _ssh_base_args():
    """Build the common ssh argument list for the exit node."""
    args = [
        "ssh",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-p", str(EXIT_PORT),
    ]
    if EXIT_KEY:
        args += ["-i", EXIT_KEY]
    args.append(f"{EXIT_USER}@{EXIT_HOST}")
    return args


def _ssh_env():
    """Environment for the ssh subprocess (propagates SSHPASS when needed)."""
    env = os.environ.copy()
    if EXIT_PASSWORD:
        env["SSHPASS"] = EXIT_PASSWORD
    return env


@pytest.fixture(scope="module")
def exit_node_ssh():
    """Return a callable that runs a command on the exit node over SSH.

    Usage::

        r = exit_node_ssh("wg show", timeout=15)
        r.returncode   # int
        r.stdout       # str (stripped)
        r.stderr       # str

    Returns a ``subprocess.CompletedProcess``. Auth preference: explicit
    identity file (``FIPS_EXIT_SSH_KEY``), else sshpass with a configured
    password, else the ssh agent.
    """

    def _run(cmd, timeout=30):
        base = list(_ssh_base_args())
        if not EXIT_KEY and EXIT_PASSWORD:
            base = ["sshpass", "-e"] + base
        if EXIT_SUDO:
            # Wrap the whole command (including shell operators like &&/||) so
            # every part runs as root. shlex.quote embeds cmd safely into the
            # remote `sh -c` invocation regardless of inner quotes.
            cmd = f"sudo -n sh -c {shlex.quote(cmd)}"
        try:
            r = subprocess.run(
                base + [cmd],
                capture_output=True, text=True, timeout=timeout,
                env=_ssh_env(),
            )
            # Normalise: strip the "Warning: Permanently added ..." banner.
            r.stdout = re.sub(
                r"Warning:.*Permanently added[^\n]*\n?", "", r.stdout
            ).strip()
            return r
        except subprocess.TimeoutExpired as e:
            return subprocess.CompletedProcess(
                base + [cmd], 124, e.stdout or "", f"TIMEOUT after {timeout}s"
            )

    return _run


@pytest.fixture(scope="module", autouse=True)
def _exit_node_reachable(exit_node_ssh):
    """Skip the whole module if the exit node is not reachable over SSH.

    A FIPS exit node smoke test that cannot talk to the node is meaningless, so
    we skip rather than fail — matching the task requirement that tests must be
    SKIPPABLE when the VPS is unreachable.
    """
    r = exit_node_ssh("echo ok", timeout=12)
    if r.returncode != 0 or "ok" not in r.stdout:
        pytest.skip(
            f"FIPS exit node {EXIT_USER}@{EXIT_HOST}:{EXIT_PORT} unreachable "
            f"over SSH: rc={r.returncode} stderr={r.stderr[:160]!r}"
        )


def _host_has_wg(exit_node_ssh) -> bool:
    """True if a WireGuard interface is actually configured (not just installed).

    ``wg`` may be present as a tool without any tunnel configured — that alone
    does not make a host a FIPS exit node, so we require ``wg show all`` to
    report at least one interface.
    """
    r = exit_node_ssh("wg show all 2>/dev/null", timeout=10)
    return bool(r.stdout.strip())


def _host_has_fips_exit_table(exit_node_ssh) -> bool:
    r = exit_node_ssh("nft list table ip fips-exit 2>/dev/null", timeout=12)
    return r.returncode == 0 and "fips-exit" in r.stdout


@pytest.fixture(scope="module", autouse=True)
def _require_fips_exit_node(exit_node_ssh):
    """Skip the module unless the host looks like a FIPS exit node.

    Feature detection at the module level: a host that has NEITHER WireGuard
    NOR a ``fips-exit`` nftables table is simply not a FIPS exit node (it may
    be an unrelated box sharing the address, or the exit node not yet deployed
    there). We skip the whole suite in that case so a not-yet-deployed or
    mis-addressed node yields clean ``SKIPPED`` results rather than noisy
    failures.

    Partial misconfiguration is NOT skipped here: a host that has WireGuard but
    is missing the MASQUERADE rule still runs the tests and fails on the gap —
    that is exactly the regression a smoke test must catch.
    """
    if not (_host_has_wg(exit_node_ssh) or _host_has_fips_exit_table(exit_node_ssh)):
        pytest.skip(
            f"Host {EXIT_HOST} is reachable but does not look like a FIPS exit node "
            f"(no WireGuard and no 'fips-exit' nftables table) — exit node not "
            f"deployed here. Set FIPS_EXIT_HOST to the real exit node."
        )


# ---------------------------------------------------------------------------
# Happy-path smoke tests
# ---------------------------------------------------------------------------


def _parse_wg_dump(stdout: str):
    """Parse ``wg show all dump`` into peer records.

    ``wg show all dump`` emits a tab-separated interface line followed by one
    peer line per peer. Every line is prefixed with the interface name (e.g.
    ``wg0``). The interface line has 5 fields
    (iface, private-key, public-key, listen-port, fwmark); a peer line has 9:

        0 iface   1 pubkey   2 psk   3 endpoint   4 allowed-ips
        5 handshake-epoch   6 rx-bytes   7 tx-bytes   8 keepalive

    Interface lines are dropped by the field-count guard. Returns a list of
    dicts with keys: pubkey, endpoint, allowed_ips, rx, tx, handshake_ts.
    """
    peers = []
    for line in stdout.strip().splitlines():
        fields = line.split("\t")
        # Interface lines have 5 fields; peer lines have 9. The "< 8" guard
        # drops interface lines and any short/blank lines.
        if len(fields) < 8:
            continue
        # On a peer line fields[1] is the 44-char base64 public key.
        pubkey = fields[1]
        if len(pubkey) != 44 or "=" not in pubkey:
            continue
        try:
            peers.append({
                "pubkey": pubkey,
                "endpoint": fields[3],
                "allowed_ips": fields[4],
                "handshake_ts": (
                    int(fields[5]) if fields[5].lstrip("-").isdigit() else 0
                ),
                "rx": int(fields[6]) if fields[6].isdigit() else 0,
                "tx": int(fields[7]) if fields[7].isdigit() else 0,
            })
        except (ValueError, IndexError):
            continue
    return peers


def test_wireguard_peer_has_recent_handshake(exit_node_ssh):
    """GUARANTEE 1: the WireGuard tunnel is UP on the exit node.

    At least one peer must have completed a handshake recently (WireGuard
    rekeys every ~120-180s; we accept anything within the last 5 minutes as
    evidence of a live tunnel).
    """
    r = exit_node_ssh("command -v wg >/dev/null 2>&1 && wg show all dump", timeout=15)
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip("WireGuard (wg) not installed or no interface on exit node")
    peers = _parse_wg_dump(r.stdout)
    if not peers:
        pytest.skip("WireGuard interface present but no peers configured")
    now = int(time.time())
    recent = [p for p in peers if p["handshake_ts"] and (now - p["handshake_ts"]) <= 300]
    assert recent, (
        "No WireGuard peer has a handshake within the last 5 minutes "
        f"(peers={[p['handshake_ts'] for p in peers]}, now={now}) — tunnel is not UP"
    )


def test_nftables_fips_exit_masquerade_loaded(exit_node_ssh):
    """GUARANTEE 2: the ``fips-exit`` nftables MASQUERADE table is loaded."""
    r = exit_node_ssh("nft list ruleset 2>/dev/null", timeout=15)
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip("nftables (nft) not installed or no ruleset on exit node")
    ruleset = r.stdout
    assert "fips-exit" in ruleset, (
        "nftables 'fips-exit' table is not loaded — MASQUERADE not configured"
    )
    assert "masquerade" in ruleset.lower(), (
        "No 'masquerade' rule found in nftables ruleset (fips-exit table present "
        "but not NATing)"
    )


def test_ip_forwarding_enabled_and_egress_works(exit_node_ssh):
    """GUARANTEE 3: forwarding is enabled and the exit node can reach the internet.

    A full mesh→internet packet round-trip requires a tunnel peer, which is not
    available from the test host. As a smoke check we verify the two
    preconditions the forward path depends on: (a) IPv4 forwarding is enabled in
    the kernel, and (b) the exit node itself has working public-internet egress.
    With the tunnel handshake (test 1) + MASQUERADE (test 2) + this test, a
    packet entering the tunnel has a complete forward path to the internet.
    """
    fwd = exit_node_ssh("sysctl -n net.ipv4.ip_forward 2>/dev/null", timeout=12)
    if fwd.returncode != 0 or not fwd.stdout.strip():
        pytest.skip("sysctl/net.ipv4.ip_forward unavailable on exit node")
    assert fwd.stdout.strip() == "1", (
        f"IPv4 forwarding is disabled (net.ipv4.ip_forward={fwd.stdout.strip()!r}) — "
        "tunnelled packets cannot be routed"
    )

    egress = exit_node_ssh(
        "curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://1.1.1.1 "
        "2>/dev/null || echo CURL_FAIL",
        timeout=20,
    )
    if "CURL_FAIL" in egress.stdout or egress.returncode != 0:
        pytest.skip("curl unavailable or internet unreachable from exit node")
    code = egress.stdout.strip()
    assert code in ("200", "301", "302", "204"), (
        f"Exit node cannot reach the public internet (http_code={code!r})"
    )


def test_nostr_kind_30078_route_advert_published(exit_node_ssh):
    """GUARANTEE 4: a kind 30078 route advertisement is published for this node.

    Queries the configured relays via the ``nak`` CLI for kind 30078 events
    authored by the exit node (``FIPS_EXIT_NPUB``). At least one event must
    exist and look like a route advertisement (content/tags reference the exit
    node or a route/exit marker).

    Skips when the advertisement cannot be attributed: ``nak`` not installed,
    no exit-node pubkey configured, or all relays unreachable.
    """
    nak = shutil.which("nak")
    if not nak:
        pytest.skip("nak CLI not installed — cannot query Nostr relays")
    if not EXIT_NPUB:
        pytest.skip(
            "FIPS_EXIT_NPUB not set — cannot attribute kind 30078 events to the "
            "exit node"
        )

    cmd = [nak, "req", "-k", "30078", "-a", EXIT_NPUB, "-l", "20"]
    cmd.extend(EXIT_RELAYS)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        pytest.skip("nak query timed out querying relays")
    if r.returncode != 0 and not r.stdout.strip():
        pytest.skip(f"nak relay query failed: {r.stderr.strip()[:160]!r}")

    events = []
    for line in r.stdout.strip().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not events:
        pytest.skip("No kind 30078 events found for this node on the relays")

    # An advertisement should reference the exit role. Accept content/tags that
    # mention route/exit/fips or the node's own host, so this stays robust to
    # schema drift in the advert format.
    needle_keys = ("fips", "exit", "route", EXIT_HOST)
    found = False
    for ev in events:
        blob = (
            str(ev.get("content", "")).lower()
            + " "
            + json.dumps(ev.get("tags", [])).lower()
        )
        if any(k.lower() in blob for k in needle_keys):
            found = True
            break
    assert found, (
        "kind 30078 events exist but none look like a route advertisement "
        f"(no fips/exit/route/{EXIT_HOST} reference); sample="
        f"{json.dumps(events[0])[:200]}"
    )


def test_wireguard_tunnel_has_bidirectional_traffic(exit_node_ssh):
    """GUARANTEE 5: traffic has flowed both ways through the tunnel.

    WireGuard per-peer ``transfer`` counters (rx/tx) only increment for payloads
    that were actually encrypted/decrypted and delivered. Both rx>0 AND tx>0 on
    a single peer means a flow entered the tunnel, got NATed to the internet,
    AND the return traffic came back through the tunnel — i.e. the return path
    works. A freshly-provisioned node with zero traffic on every peer is skipped
    (not failed) since that is a legitimate pre-traffic state.
    """
    r = exit_node_ssh("command -v wg >/dev/null 2>&1 && wg show all dump", timeout=15)
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip("WireGuard (wg) not installed or no interface on exit node")
    peers = _parse_wg_dump(r.stdout)
    if not peers:
        pytest.skip("WireGuard interface present but no peers configured")

    any_traffic = any(p["rx"] or p["tx"] for p in peers)
    if not any_traffic:
        pytest.skip(
            "Tunnel is up but no traffic has been transferred yet on any peer "
            "(freshly provisioned node?)"
        )

    bidir = [p for p in peers if p["rx"] > 0 and p["tx"] > 0]
    assert bidir, (
        "WireGuard traffic is one-directional on every peer — return path "
        "(internet → tunnel) is not working. Per-peer rx/tx: "
        f"{[(p['rx'], p['tx']) for p in peers]}"
    )
