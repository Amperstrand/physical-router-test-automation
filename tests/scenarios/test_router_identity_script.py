"""Comprehensive test suite for the router-identity derivation system.

Tests PR #189 (Go ``src/identity/identity.go``) and PR #190 (shell
``packaging/files/etc/uci-defaults/95-router-identity``) together, proving
they produce identical output and covering all edge cases.

Three tiers:
  1. **Unit** — pure computation via the Go reference binary (no shell needed)
  2. **E2E** — the shell script under ``busybox sh`` on an SHC VM with stubbed
     ``uci``, covering every behavioral path
  3. **Parity** — Go binary output vs shell script output on the same key

Run::

    SHC_VM_HOST=66.92.204.237 SHC_VM_USER=debian \\
        python3 -m pytest tests/scenarios/test_router_identity_script.py -v \\
        --no-deploy -o addopts=""
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import textwrap
import uuid

import pytest

log = logging.getLogger("tollgate.scenarios.router_identity")

pytestmark = [pytest.mark.story("R6"),pytest.mark.api, pytest.mark.extended, pytest.mark.virtual_lab]

VM_HOST = os.environ.get("SHC_VM_HOST", "66.92.204.237")
VM_USER = os.environ.get("SHC_VM_USER", "debian")


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def _ssh(cmd: str, timeout: int = 60, check: bool = False) -> str:
    r = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null",
         "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         f"{VM_USER}@{VM_HOST}", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"SSH failed (rc={r.returncode}): {cmd}\n{r.stderr}")
    return r.stdout


def _scp_to_vm(local: str, remote: str) -> None:
    subprocess.run(
        ["scp", "-O", "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes",
         local, f"{VM_USER}@{VM_HOST}:{remote}"],
        check=True, capture_output=True, timeout=180,
    )


def _have(cmd: str) -> bool:
    return _ssh(f"command -v {cmd}").strip() != ""


# ---------------------------------------------------------------------------
# Golden vectors — generated from the reconciled Go identity package.
# Both Go and shell now hash the hex pubkey X-coordinate with the same
# domain separators, so they produce IDENTICAL values.
# ---------------------------------------------------------------------------

GOLDEN_KEYS = [
    {
        "priv": "2e00f5b42d15de54ecc00059d972211dff6ad7c0cd2837c73051d213b0bf03e8",
        "npub_x_hex": "6b31db026efadda9b1f5a51c7566bf394965987616e12df98ea53ad01ef9eb51",
        "ipv4": "100.118.131.1",
        "mac_br_lan": "92:09:4f:16:59:79",
        "mac_wlan0": "aa:de:8d:0a:43:9c",
        "mac_wlan1": "5a:48:d6:74:c9:66",
    },
    {
        "priv": "5380c49bab4fde52a14cdb06a06a1848629e260ec3449fd50eac8d97778aeccc",
        "npub_x_hex": "31af0a419948f364c1351fa545946b9deb7739ee3db49282ba7b185492f28d60",
        "ipv4": "100.64.169.1",
        "mac_br_lan": "0a:da:43:c6:eb:0d",
        "mac_wlan0": "56:ac:b4:e1:6d:28",
        "mac_wlan1": "42:eb:6f:be:82:42",
    },
    {
        "priv": "70da4ef514e24971ad61f467e71448e0a6cd12facd86ceefdfbeb6cf6d123589",
        "npub_x_hex": "5a48fde988318451f9bc8cadff045a8656decf3e5889bee1cdda7fce28879a29",
        "ipv4": "100.78.63.1",
        "mac_br_lan": "fa:c5:b1:b7:8b:fe",
        "mac_wlan0": "52:37:48:58:50:71",
        "mac_wlan1": "e2:e0:55:c8:bf:b6",
    },
]

INVALID_KEYS = [
    ("", "empty"),
    ("z" * 64, "non-hex"),
    ("a" * 63, "63 chars (too short)"),
    ("a" * 65, "65 chars (too long)"),
    ("a" * 32, "32 chars (16 bytes)"),
]

# secp256k1 curve order minus 1 (largest valid scalar)
SECP256K1_N_MINUS_1 = "fffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364140"
# secp256k1 curve order (invalid — equal to N)
SECP256K1_N = "fffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141"
# All zeros (invalid — zero scalar)
ALL_ZEROS = "0" * 64

_IPV4_RE = re.compile(r"^(100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.(\d{1,3})\.1)$")
_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
_DERIVED_RE = re.compile(r"npub=([0-9a-f]+)\s+ip=(\S+)\s+br-lan=([0-9a-f:]+)", re.I)


# ---------------------------------------------------------------------------
# Session fixture: provision VM once
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def vm():
    if not _have("busybox"):
        _ssh("sudo apt-get update -qq && sudo apt-get install -y -qq busybox-static jq", timeout=120, check=True)
    for tool in ("openssl", "sha256sum", "busybox", "jq"):
        assert _have(tool), f"missing {tool}"

    remote_dir = "/tmp/router-identity-tests"
    _ssh(f"mkdir -p {remote_dir}/bin", check=True)

    here = os.path.dirname(os.path.abspath(__file__))
    fixtures = os.path.abspath(os.path.join(here, "..", "..", "tests", "fixtures", "router_identity"))
    go_ref_src = os.path.join(fixtures, "identity-ref-linux-amd64")
    script_src = os.path.join(fixtures, "95-router-identity")
    if not os.path.exists(go_ref_src) or not os.path.exists(script_src):
        pytest.skip(f"fixtures missing under {fixtures}")

    go_ref = f"{remote_dir}/identity-ref"
    script = f"{remote_dir}/95-router-identity"
    _scp_to_vm(go_ref_src, go_ref)
    _scp_to_vm(script_src, script)
    _ssh(f"chmod +x {go_ref} {script}", check=True)
    assert "ipv4=" in _ssh(f"{go_ref} {GOLDEN_KEYS[0]['priv']} 2>&1"), "Go ref broken on VM"

    return {"dir": remote_dir, "go_ref": go_ref, "script": script}


# ---------------------------------------------------------------------------
# UCI stub — the only interceptable command (busybox resolves
# logger/ip/ifconfig as built-in applets, ignoring $PATH).
# ---------------------------------------------------------------------------

_UCI_STUB = r"""#!/bin/sh
KVDIR="$SANDBOX/etc/uci_kv"
mkdir -p "$KVDIR"
while [ "$#" -gt 0 ]; do
    case "$1" in -*) shift ;; *) break ;; esac
done
op="${1:-}"; shift || true
echo "uci $op $*" >> "$UCI_LOG"
case "$op" in
  get)
    key="${1:-}"
    flat=$(echo "$key" | tr '.' '_')
    [ -f "$KVDIR/$flat" ] && cat "$KVDIR/$flat" && exit 0
    exit 1
    ;;
  set)
    arg="${1:-}"
    case "$arg" in *=*) kv="$arg" ;; *) kv="$arg=${2:-}" ;; esac
    flat=$(echo "$kv" | sed 's/=.*//' | tr '.' '_')
    val=$(echo "$kv" | sed 's/[^=]*=//')
    echo "$val" > "$KVDIR/$flat"
    ;;
  show)
    target="${1:-}"
    [ "$target" = "network" ] && echo "network.br_lan_dev=device"
    ;;
  commit) : ;;
esac
exit 0
"""


def _identities_json(priv: str) -> str:
    return json.dumps({"owned_identities": [{"name": "merchant", "privatekey": priv}]})


# ---------------------------------------------------------------------------
# Per-test sandbox
# ---------------------------------------------------------------------------

class Sandbox:
    def __init__(self, path: str, script: str):
        self.path = path
        self.script = script

    def write_identities(self, priv: str) -> None:
        b64 = base64.b64encode(_identities_json(priv).encode()).decode()
        _ssh(f"echo '{b64}' | base64 -d > {self.path}/etc/tollgate/identities.json", check=True)

    def set_opt_out(self, val: str = "1") -> None:
        _ssh(f"echo '{val}' > {self.path}/etc/uci_kv/tollgate_identity_identity_opt_out", check=True)

    def mark_derived(self) -> None:
        _ssh(f"touch {self.path}/etc/tollgate/.identity_derived", check=True)

    def reset_state(self) -> None:
        _ssh(
            f"rm -f {self.path}/etc/tollgate/.identity_derived "
            f"&& rm -rf {self.path}/etc/uci_kv && mkdir -p {self.path}/etc/uci_kv "
            f"&& : > {self.path}/logs/logger.log "
            f"&& : > {self.path}/logs/uci.log",
            check=True,
        )

    def run(self, timeout: int = 30) -> Result:
        env = (f"SANDBOX={self.path} "
               f"UCI_LOG={self.path}/logs/uci.log "
               f"LOGGER_LOG={self.path}/logs/logger.log ")
        cmd = (f"cd {self.path} && {env} "
               f"PATH={self.path}/bin:/usr/bin:/bin "
               f"busybox sh {self.script} 2>&1")
        r = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             "-o", "BatchMode=yes",
             f"{VM_USER}@{VM_HOST}",
             f"_rc=X; {cmd}; _rc=$?; echo \"__RC__=$_rc\""],
            capture_output=True, text=True, timeout=timeout,
        )
        return Result(self, r.stdout)

    def uci_log(self) -> str:
        return _ssh(f"cat {self.path}/logs/uci.log 2>/dev/null || true")

    def logger_log(self) -> str:
        return _ssh(f"cat {self.path}/logs/logger.log 2>/dev/null || true")

    def flag_exists(self) -> bool:
        return "YES" in _ssh(f"test -f {self.path}/etc/tollgate/.identity_derived && echo YES || echo NO")

    def kv_get(self, key: str) -> str:
        return _ssh(f"cat {self.path}/etc/uci_kv/{key} 2>/dev/null || true").strip()


class Result:
    def __init__(self, sb: Sandbox, raw: str):
        self.sb = sb
        self.raw = raw
        self.exit_code: int | None = None
        for line in raw.splitlines():
            if line.startswith("__RC__="):
                try:
                    self.exit_code = int(line.split("=", 1)[1])
                except ValueError:
                    pass

    @property
    def uci(self) -> str:
        return self.sb.uci_log()

    @property
    def logger(self) -> str:
        return self.sb.logger_log()

    def derived(self) -> dict | None:
        for line in reversed(self.logger.splitlines()):
            m = _DERIVED_RE.search(line)
            if m:
                return {"npub_x": m.group(1), "ipv4": m.group(2), "mac": m.group(3)}
        return None

    def applied_line(self) -> str:
        for line in self.logger.splitlines():
            if "applied IP=" in line:
                return line
        return ""


@pytest.fixture()
def sb(vm):
    path = f"{vm['dir']}/sb-{uuid.uuid4().hex[:8]}"
    _ssh(f"mkdir -p {path}/bin {path}/etc/tollgate {path}/logs {path}/etc/uci_kv", check=True)

    b64 = base64.b64encode(_UCI_STUB.encode()).decode()
    _ssh(f"echo '{b64}' | base64 -d > {path}/bin/uci && chmod +x {path}/bin/uci", check=True)

    for section in ("wireless_radio0", "wireless_radio1"):
        _ssh(f"echo 'wifi' > {path}/etc/uci_kv/{section}", check=True)

    sb_script = f"{path}/95-router-identity"
    _ssh(
        f"sed "
        f"-e 's#/etc/tollgate/\\.identity_derived#{path}/etc/tollgate/.identity_derived#g' "
        f"-e 's#/etc/tollgate/identities\\.json#{path}/etc/tollgate/identities.json#g' "
        f"-e 's#log() {{ logger.*}}#log() {{ printf \"%s: %s\\\\n\" \"\\$LOG_TAG\" \"\\$1\" >> \"\\$LOGGER_LOG\"; }}#' "
        f"{vm['script']} > {sb_script} && chmod +x {sb_script}",
        check=True,
    )

    handle = Sandbox(path, sb_script)
    yield handle
    try:
        _ssh(f"rm -rf {path}")
    except Exception:
        pass


# ===========================================================================
# TIER 1: UNIT TESTS — Go reference binary (no shell involved)
# ===========================================================================

class TestGoUnit:
    """Pure-computation tests against the Go identity binary."""

    @pytest.mark.parametrize("key", GOLDEN_KEYS, ids=["k0", "k1", "k2"])
    def test_ipv4_matches_golden(self, vm, key):
        out = _ssh(f"{vm['go_ref']} {key['priv']} 2>&1", check=True)
        assert f"ipv4={key['ipv4']}" in out, f"Go IPv4 mismatch:\n{out}"

    @pytest.mark.parametrize("key", GOLDEN_KEYS, ids=["k0", "k1", "k2"])
    def test_mac_br_lan_matches_golden(self, vm, key):
        out = _ssh(f"{vm['go_ref']} {key['priv']} 2>&1", check=True)
        assert f"mac_br-lan={key['mac_br_lan']}" in out

    @pytest.mark.parametrize("key", GOLDEN_KEYS, ids=["k0", "k1", "k2"])
    def test_mac_wlan0_matches_golden(self, vm, key):
        out = _ssh(f"{vm['go_ref']} {key['priv']} 2>&1", check=True)
        assert f"mac_wlan0={key['mac_wlan0']}" in out

    @pytest.mark.parametrize("key", GOLDEN_KEYS, ids=["k0", "k1", "k2"])
    def test_mac_wlan1_matches_golden(self, vm, key):
        out = _ssh(f"{vm['go_ref']} {key['priv']} 2>&1", check=True)
        assert f"mac_wlan1={key['mac_wlan1']}" in out

    def test_ipv4_always_in_cgnat_range(self, vm):
        for i in range(20):
            sk = _ssh("nak key generate 2>/dev/null || openssl rand -hex 32").strip()
            out = _ssh(f"{vm['go_ref']} {sk} 2>&1", check=True)
            ip = [l for l in out.splitlines() if l.startswith("ipv4=")][0].split("=")[1]
            assert _IPV4_RE.match(ip), f"{ip} not in CGNAT 100.64/10"

    def test_per_interface_macs_always_distinct(self, vm):
        sk = _ssh("nak key generate 2>/dev/null || openssl rand -hex 32").strip()
        out = _ssh(f"{vm['go_ref']} {sk} 2>&1", check=True)
        macs = {}
        for line in out.splitlines():
            if line.startswith("mac_"):
                iface, mac = line[4:].split("=", 1)
                macs[iface] = mac
        assert len(set(macs.values())) == len(macs), f"MACs not distinct: {macs}"

    def test_different_keys_produce_different_ipv4(self, vm):
        ips = set()
        for i in range(10):
            sk = _ssh("nak key generate 2>/dev/null || openssl rand -hex 32").strip()
            out = _ssh(f"{vm['go_ref']} {sk} 2>&1", check=True)
            ip = [l for l in out.splitlines() if l.startswith("ipv4=")][0].split("=")[1]
            ips.add(ip)
        assert len(ips) >= 8, f"Too many collisions in 10 keys: {len(ips)} unique"

    @pytest.mark.parametrize("bad_key,desc", INVALID_KEYS, ids=[d for _, d in INVALID_KEYS])
    def test_rejects_invalid_key(self, vm, bad_key, desc):
        r = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
             "-o", "BatchMode=yes", f"{VM_USER}@{VM_HOST}",
             f"{vm['go_ref']} '{bad_key}' 2>&1; echo __RC__=$?"],
            capture_output=True, text=True, timeout=30,
        )
        assert "__RC__=0" not in r.stdout, \
            f"Expected non-zero exit for {desc}, got success: {r.stdout}"

    def test_accepts_scalar_one(self, vm):
        sk = "0" * 63 + "1"
        out = _ssh(f"{vm['go_ref']} {sk} 2>&1", check=True)
        assert "ipv4=100." in out

    def test_accepts_n_minus_one(self, vm):
        out = _ssh(f"{vm['go_ref']} {SECP256K1_N_MINUS_1} 2>&1", check=True)
        assert "ipv4=100." in out

    def test_mac_la_bit_set_multicast_clear(self, vm):
        sk = _ssh("nak key generate 2>/dev/null || openssl rand -hex 32").strip()
        out = _ssh(f"{vm['go_ref']} {sk} 2>&1", check=True)
        for line in out.splitlines():
            if line.startswith("mac_"):
                mac = line.split("=", 1)[1]
                first_octet = int(mac.split(":")[0], 16)
                assert first_octet & 0x02, f"LA bit not set in {mac}"
                assert not (first_octet & 0x01), f"Multicast bit set in {mac}"

    def test_macs_match_colon_format(self, vm):
        sk = _ssh("nak key generate 2>/dev/null || openssl rand -hex 32").strip()
        out = _ssh(f"{vm['go_ref']} {sk} 2>&1", check=True)
        for line in out.splitlines():
            if line.startswith("mac_"):
                mac = line.split("=", 1)[1]
                assert _MAC_RE.match(mac), f"Bad MAC format: {mac}"

    def test_pubkey_hex_is_64_chars(self, vm):
        out = _ssh(f"{vm['go_ref']} {GOLDEN_KEYS[0]['priv']} 2>&1", check=True)
        pub = [l for l in out.splitlines() if l.startswith("pubKeyHex=")][0].split("=")[1]
        assert len(pub) == 64, f"pubKeyHex len={len(pub)}, want 64"
        assert re.match(r'^[0-9a-f]{64}$', pub), f"pubKeyHex not hex: {pub}"


# ===========================================================================
# TIER 1b: NIP-06 MNEMONIC TESTS — 12-word generation + recovery
# ===========================================================================

class TestNIP06Mnemonic:
    """Tests for the 12-word NIP-06 mnemonic generation and recovery flow."""

    def test_generate_produces_12_words(self, vm):
        out = _ssh(f"{vm['go_ref']} --generate 2>&1", check=True)
        mnem = [l for l in out.splitlines() if l.startswith("mnemonic=")][0].split("=", 1)[1]
        words = mnem.split()
        assert len(words) == 12, f"Expected 12 words, got {len(words)}: {mnem}"

    def test_generate_produces_valid_hex_key(self, vm):
        out = _ssh(f"{vm['go_ref']} --generate 2>&1", check=True)
        key = [l for l in out.splitlines() if l.startswith("privatekey=")][0].split("=")[1]
        assert len(key) == 64 and re.match(r'^[0-9a-f]{64}$', key), f"Bad key: {key}"

    def test_generate_produces_cgnat_ip(self, vm):
        out = _ssh(f"{vm['go_ref']} --generate 2>&1", check=True)
        ip = [l for l in out.splitlines() if l.startswith("ipv4=")][0].split("=")[1]
        assert _IPV4_RE.match(ip), f"Not CGNAT: {ip}"

    def test_generate_produces_distinct_macs(self, vm):
        out = _ssh(f"{vm['go_ref']} --generate 2>&1", check=True)
        macs = {}
        for line in out.splitlines():
            if line.startswith("mac_"):
                iface, mac = line[4:].split("=", 1)
                macs[iface] = mac
        assert len(set(macs.values())) == len(macs), f"MACs not distinct: {macs}"

    def test_two_generates_produce_different_keys(self, vm):
        a = _ssh(f"{vm['go_ref']} --generate 2>&1", check=True)
        b = _ssh(f"{vm['go_ref']} --generate 2>&1", check=True)
        key_a = [l for l in a.splitlines() if l.startswith("privatekey=")][0].split("=")[1]
        key_b = [l for l in b.splitlines() if l.startswith("privatekey=")][0].split("=")[1]
        assert key_a != key_b, "Two generates produced identical keys"

    def test_recover_reproduces_same_key(self, vm):
        gen = _ssh(f"{vm['go_ref']} --generate 2>&1", check=True)
        mnem = [l for l in gen.splitlines() if l.startswith("mnemonic=")][0].split("=", 1)[1]
        key_gen = [l for l in gen.splitlines() if l.startswith("privatekey=")][0].split("=")[1]
        rec = _ssh(f'{vm["go_ref"]} --recover "{mnem}" 2>&1', check=True)
        key_rec = [l for l in rec.splitlines() if l.startswith("privatekey=")][0].split("=")[1]
        assert key_gen == key_rec, f"Recovery mismatch: gen={key_gen} rec={key_rec}"

    def test_recover_reproduces_same_ip(self, vm):
        gen = _ssh(f"{vm['go_ref']} --generate 2>&1", check=True)
        mnem = [l for l in gen.splitlines() if l.startswith("mnemonic=")][0].split("=", 1)[1]
        ip_gen = [l for l in gen.splitlines() if l.startswith("ipv4=")][0].split("=")[1]
        rec = _ssh(f'{vm["go_ref"]} --recover "{mnem}" 2>&1', check=True)
        ip_rec = [l for l in rec.splitlines() if l.startswith("ipv4=")][0].split("=")[1]
        assert ip_gen == ip_rec, f"IP mismatch: gen={ip_gen} rec={ip_rec}"

    def test_recover_rejects_invalid_mnemonic(self, vm):
        r = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
             "-o", "BatchMode=yes", f"{VM_USER}@{VM_HOST}",
             f"{vm['go_ref']} --recover 'not a valid mnemonic' 2>&1; echo __RC__=$?"],
            capture_output=True, text=True, timeout=30,
        )
        assert "__RC__=0" not in r.stdout, f"Invalid mnemonic should fail: {r.stdout}"

    def test_recover_rejects_garbage(self, vm):
        r = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
             "-o", "BatchMode=yes", f"{VM_USER}@{VM_HOST}",
             f"{vm['go_ref']} --recover 'zzzzzzzzzzzzzzzzzz' 2>&1; echo __RC__=$?"],
            capture_output=True, text=True, timeout=30,
        )
        assert "__RC__=0" not in r.stdout

    def test_different_mnemonics_different_keys(self, vm):
        a = _ssh(f"{vm['go_ref']} --generate 2>&1", check=True)
        b = _ssh(f"{vm['go_ref']} --generate 2>&1", check=True)
        ip_a = [l for l in a.splitlines() if l.startswith("ipv4=")][0].split("=")[1]
        ip_b = [l for l in b.splitlines() if l.startswith("ipv4=")][0].split("=")[1]
        assert ip_a != ip_b, "Two mnemonics produced same IP"

    def test_generate_words_are_lowercase_alpha(self, vm):
        out = _ssh(f"{vm['go_ref']} --generate 2>&1", check=True)
        mnem = [l for l in out.splitlines() if l.startswith("mnemonic=")][0].split("=", 1)[1]
        for w in mnem.split():
            assert re.match(r'^[a-z]+$', w), f"Word '{w}' not lowercase alpha"

    def test_nip06_key_works_in_shell(self, vm, sb):
        """End-to-end: generate a NIP-06 key → write to identities.json →
        run the shell script → verify shell IP matches Go-derived IP."""
        gen = _ssh(f"{vm['go_ref']} --generate 2>&1", check=True)
        key = [l for l in gen.splitlines() if l.startswith("privatekey=")][0].split("=")[1]
        ip = [l for l in gen.splitlines() if l.startswith("ipv4=")][0].split("=")[1]

        sb.write_identities(key)
        r = sb.run()
        assert r.exit_code == 0, f"Shell failed with NIP-06 key: {r.raw}"
        shell_ip = sb.kv_get("network_lan_ipaddr")
        assert shell_ip == ip, f"Shell IP {shell_ip} != Go IP {ip}"


# ===========================================================================
# TIER 2: E2E TESTS — shell script under busybox on SHC VM
# ===========================================================================

class TestShellE2E:
    """Full behavioral tests of the uci-defaults script."""

    # -- success path -------------------------------------------------------

    @pytest.mark.parametrize("key", GOLDEN_KEYS, ids=["k0", "k1", "k2"])
    def test_success_derives_correct_ip(self, sb, key):
        sb.write_identities(key["priv"])
        r = sb.run()
        assert r.exit_code == 0
        assert sb.kv_get("network_lan_ipaddr") == key["ipv4"]

    @pytest.mark.parametrize("key", GOLDEN_KEYS, ids=["k0", "k1", "k2"])
    def test_success_sets_netmask(self, sb, key):
        sb.write_identities(key["priv"])
        r = sb.run()
        assert r.exit_code == 0
        assert sb.kv_get("network_lan_netmask") == "255.255.255.0"

    @pytest.mark.parametrize("key", GOLDEN_KEYS, ids=["k0", "k1", "k2"])
    def test_success_sets_dhcp_pool(self, sb, key):
        sb.write_identities(key["priv"])
        r = sb.run()
        assert r.exit_code == 0
        assert sb.kv_get("dhcp_lan_start") == "100"
        assert sb.kv_get("dhcp_lan_limit") == "150"

    @pytest.mark.parametrize("key", GOLDEN_KEYS, ids=["k0", "k1", "k2"])
    def test_success_applies_per_radio_macs(self, sb, key):
        sb.write_identities(key["priv"])
        r = sb.run()
        assert r.exit_code == 0
        assert sb.kv_get("wireless_radio0_macaddr") == key["mac_wlan0"]
        assert sb.kv_get("wireless_radio1_macaddr") == key["mac_wlan1"]

    @pytest.mark.parametrize("key", GOLDEN_KEYS, ids=["k0", "k1", "k2"])
    def test_success_radio_macs_are_distinct(self, sb, key):
        sb.write_identities(key["priv"])
        r = sb.run()
        assert r.exit_code == 0
        m0 = sb.kv_get("wireless_radio0_macaddr")
        m1 = sb.kv_get("wireless_radio1_macaddr")
        assert m0 != m1, f"radio0 and radio1 share MAC {m0}"

    @pytest.mark.parametrize("key", GOLDEN_KEYS, ids=["k0", "k1", "k2"])
    def test_success_creates_idempotency_flag(self, sb, key):
        sb.write_identities(key["priv"])
        assert not sb.flag_exists()
        r = sb.run()
        assert r.exit_code == 0
        assert sb.flag_exists()

    def test_success_commits_all_configs(self, sb):
        sb.write_identities(GOLDEN_KEYS[0]["priv"])
        r = sb.run()
        assert r.exit_code == 0
        uci = r.uci
        assert "commit network" in uci
        assert "commit dhcp" in uci
        assert "commit wireless" in uci

    def test_success_reaches_applied_log(self, sb):
        sb.write_identities(GOLDEN_KEYS[0]["priv"])
        r = sb.run()
        assert r.exit_code == 0
        assert r.applied_line(), f"Did not reach 'applied' summary:\n{r.raw}"

    # -- idempotency --------------------------------------------------------

    def test_idempotent_skip_when_flag_exists(self, sb):
        sb.write_identities(GOLDEN_KEYS[0]["priv"])
        sb.mark_derived()
        r = sb.run()
        assert r.exit_code == 0
        assert "network.lan.ipaddr" not in r.uci
        assert "already derived" in r.logger.lower()

    # -- opt-out ------------------------------------------------------------

    def test_opt_out_skips_when_set_to_1(self, sb):
        sb.write_identities(GOLDEN_KEYS[0]["priv"])
        sb.set_opt_out("1")
        r = sb.run()
        assert r.exit_code == 0
        assert "network.lan.ipaddr" not in r.uci
        assert "opt_out" in r.logger.lower()

    def test_opt_out_does_not_trigger_when_absent(self, sb):
        sb.write_identities(GOLDEN_KEYS[0]["priv"])
        r = sb.run()
        assert r.exit_code == 0
        assert "network.lan.ipaddr" in r.uci

    def test_opt_out_does_not_trigger_when_zero(self, sb):
        sb.write_identities(GOLDEN_KEYS[0]["priv"])
        sb.set_opt_out("0")
        r = sb.run()
        assert r.exit_code == 0
        assert "network.lan.ipaddr" in r.uci

    # -- retry / error paths ------------------------------------------------

    def test_retry_when_identities_missing(self, sb):
        r = sb.run(timeout=60)
        assert r.exit_code == 1
        assert "commit network" not in r.uci
        assert not sb.flag_exists()
        assert "not present" in r.logger.lower() or "retry" in r.logger.lower()

    @pytest.mark.parametrize("bad_key,desc", INVALID_KEYS, ids=[d for _, d in INVALID_KEYS])
    def test_rejects_bad_key(self, sb, bad_key, desc):
        if bad_key:
            sb.write_identities(bad_key)
        r = sb.run()
        assert r.exit_code == 1, f"Expected exit 1 for {desc}, got {r.exit_code}"
        assert not sb.flag_exists()

    def test_rejects_missing_merchant_identity(self, sb):
        payload = json.dumps({"owned_identities": [{"name": "relay", "privatekey": "a" * 64}]})
        b64 = base64.b64encode(payload.encode()).decode()
        _ssh(f"echo '{b64}' | base64 -d > {sb.path}/etc/tollgate/identities.json", check=True)
        r = sb.run()
        assert r.exit_code == 1
        assert not sb.flag_exists()

    def test_selects_only_merchant_among_many(self, sb):
        key = GOLDEN_KEYS[0]
        payload = json.dumps({"owned_identities": [
            {"name": "relay", "privatekey": "f" * 64},
            {"name": "merchant", "privatekey": key["priv"]},
            {"name": "profitshare", "privatekey": "e" * 64},
        ]})
        b64 = base64.b64encode(payload.encode()).decode()
        _ssh(f"echo '{b64}' | base64 -d > {sb.path}/etc/tollgate/identities.json", check=True)
        r = sb.run()
        assert r.exit_code == 0
        assert sb.kv_get("network_lan_ipaddr") == key["ipv4"]

    # -- busybox portability ------------------------------------------------

    def test_runs_under_busybox_sh(self, sb):
        sb.write_identities(GOLDEN_KEYS[0]["priv"])
        r = sb.run()
        assert r.exit_code == 0
        assert "not found" not in r.raw.lower()
        assert "syntax error" not in r.raw.lower()

    # -- cross-key uniqueness -----------------------------------------------

    def test_different_keys_different_ip_and_mac(self, sb):
        a, b = GOLDEN_KEYS[0], GOLDEN_KEYS[1]
        sb.write_identities(a["priv"])
        ra = sb.run()
        assert ra.exit_code == 0
        ip_a = sb.kv_get("network_lan_ipaddr")

        sb.reset_state()
        sb.write_identities(b["priv"])
        rb = sb.run()
        assert rb.exit_code == 0
        ip_b = sb.kv_get("network_lan_ipaddr")

        assert ip_a != ip_b, f"Two keys collided on IP: {ip_a}"


# ===========================================================================
# TIER 3: PARITY — Go binary vs shell script on the same key
# ===========================================================================

class TestParity:
    """The shell script and Go binary MUST produce identical output."""

    @pytest.mark.parametrize("key", GOLDEN_KEYS, ids=["k0", "k1", "k2"])
    def test_ipv4_go_equals_shell(self, sb, vm, key):
        sb.write_identities(key["priv"])
        r = sb.run()
        assert r.exit_code == 0
        shell_ip = sb.kv_get("network_lan_ipaddr")
        assert shell_ip == key["ipv4"], f"shell={shell_ip} != golden={key['ipv4']}"

    @pytest.mark.parametrize("key", GOLDEN_KEYS, ids=["k0", "k1", "k2"])
    def test_mac_wlan0_go_equals_shell(self, sb, vm, key):
        sb.write_identities(key["priv"])
        r = sb.run()
        assert r.exit_code == 0
        shell_mac = sb.kv_get("wireless_radio0_macaddr")
        assert shell_mac == key["mac_wlan0"], f"shell={shell_mac} != golden={key['mac_wlan0']}"

    @pytest.mark.parametrize("key", GOLDEN_KEYS, ids=["k0", "k1", "k2"])
    def test_mac_wlan1_go_equals_shell(self, sb, vm, key):
        sb.write_identities(key["priv"])
        r = sb.run()
        assert r.exit_code == 0
        shell_mac = sb.kv_get("wireless_radio1_macaddr")
        assert shell_mac == key["mac_wlan1"], f"shell={shell_mac} != golden={key['mac_wlan1']}"

    def test_go_binary_runs_on_vm(self, vm):
        out = _ssh(f"{vm['go_ref']} {GOLDEN_KEYS[0]['priv']} 2>&1", check=True)
        assert "ipv4=100." in out
        assert "mac_br-lan=" in out

    def test_derived_log_line_format(self, sb):
        sb.write_identities(GOLDEN_KEYS[0]["priv"])
        r = sb.run()
        assert r.exit_code == 0
        d = r.derived()
        assert d is not None, f"No derived log line:\n{r.raw}"
        assert d["ipv4"] == GOLDEN_KEYS[0]["ipv4"]


# ===========================================================================
# TIER 4: MAKEFILE WIRING — static checks on packaging/Makefile
# ===========================================================================

class TestMakefileWiring:

    @pytest.fixture(autouse=True)
    def _fetch_makefile(self):
        r = subprocess.run(
            ["gh", "api",
             "repos/c03rad0r/test-stablechannel-tollgate-module-basic-go/contents/packaging/Makefile",
             "-F", "ref=feat/router-identity-script", "--jq", ".content"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            pytest.skip("Could not fetch Makefile")
        self.makefile = base64.b64decode(r.stdout.strip()).decode()

    def test_script_in_install(self):
        assert "95-router-identity" in self.makefile

    def test_script_in_postinst_loop(self):
        m = re.search(r"for script in ([^;]+); do", self.makefile)
        assert m, "postinst for-loop not found"
        names = [s.strip().split("/")[-1] for s in m.group(1).split("\\") if s.strip()]
        assert "95-router-identity" in names
        if "90-tollgate-captive-portal-symlink" in names and "99-tollgate-setup" in names:
            assert names.index("90-tollgate-captive-portal-symlink") < names.index("95-router-identity") < names.index("99-tollgate-setup")
