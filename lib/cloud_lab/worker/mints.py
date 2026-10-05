"""Cloud lab worker — local Cashu mints."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import textwrap
import time
import urllib.request
from pathlib import Path

from lib.cloud_lab.constants import (
    CDK_MINT_DIR,
    CDK_MINT_INTERNAL_PORT,
    CDK_MINT_PORT,
    CDK_MINT_URL,
    CDK_VERSION,
    LOCAL_MINT_HOST,
    NUTSHELL_V1_MINT_LAN,
    NUTSHELL_V1_MINT_PORT,
    NUTSHELL_V1_MINT_URL,
    NUTSHELL_V2_MINT_PORT,
    NUTSHELL_V2_MINT_URL,
    OPENWRT_IP,
    TEST_DIR,
    TOXIPROXY_DIR,
    TOXIPROXY_MGMT_PORT,
    TOXIPROXY_VERSION,
    V1_TESTNUT_NUTSHELL_LAN,
    V2_TESTNUT_CDK_LAN,
    V2_TESTNUT_NUTSHELL_LAN,
    VIRT_LAB_PASSWORD,
)
from lib.cloud_lab.worker.config import WorkerConfig
from lib.cloud_lab.worker.deploy import wait_for_backend
from lib.cloud_lab.worker.shell import _run, log

def ensure_cdk_binary() -> None:
    binary = f"{CDK_MINT_DIR}/cdk-mintd"
    cli_binary = f"{CDK_MINT_DIR}/cdk-cli"
    r = _run(f"test -x {shlex.quote(binary)} && echo CDK_BINARY_OK", timeout=10, check=False)
    if "CDK_BINARY_OK" in r.stdout:
        log.info("CDK mintd binary already cached")
    else:
        _run(f"mkdir -p {shlex.quote(CDK_MINT_DIR)}", timeout=10)
        _download_cdk_cached(binary, "cdk-mintd")

        _run(f"chmod +x {shlex.quote(binary)}", timeout=10)
        r = _run(f"{shlex.quote(binary)} --version 2>&1 || {shlex.quote(binary)} --help 2>&1 | head -1", timeout=10, check=False)
        log.info("CDK binary verified: %s", (r.stdout or "").strip()[:80])

    r = _run(f"test -x {shlex.quote(cli_binary)} && echo CDK_CLI_OK", timeout=10, check=False)
    if "CDK_CLI_OK" in r.stdout:
        log.info("CDK CLI binary already cached")
    else:
        _run(f"mkdir -p {shlex.quote(CDK_MINT_DIR)}", timeout=10)
        _download_cdk_cached(cli_binary, "cdk-cli")

        _run(f"chmod +x {shlex.quote(cli_binary)}", timeout=10)
        _run(f"ln -sf {shlex.quote(cli_binary)} /usr/local/bin/cdk-cli 2>/dev/null || true", timeout=10)
        r = _run(f"{shlex.quote(cli_binary)} --version 2>&1 || {shlex.quote(cli_binary)} --help 2>&1 | head -1", timeout=10, check=False)
        log.info("CDK CLI binary verified: %s", (r.stdout or "").strip()[:80])


def _download_cdk_cached(dest: str, binary_name: str) -> None:
    nsec_file = os.environ.get("NSEC_FILE", "")
    if not nsec_file or not Path(nsec_file).exists():
        for candidate in [os.path.expanduser("~/nsec"), "/root/nsec"]:
            if Path(candidate).exists():
                nsec_file = candidate
                break

    if nsec_file and Path(nsec_file).exists():
        from lib.cloud_lab.worker.blossom_cache import cached_build, compute_cache_key
        cache_key = compute_cache_key("cdk", CDK_VERSION, binary_name, "x86_64")
        url = f"https://github.com/cashubtc/cdk/releases/download/v{CDK_VERSION}/{binary_name}-{CDK_VERSION}-x86_64"
        def _wget():
            _run(f"wget -q -O {shlex.quote(dest)} {shlex.quote(url)}", timeout=120)
            return dest
        log.info("CDK %s: checking Blossom cache (key=%s...)", binary_name, cache_key[:12])
        cached_build(cache_key, _wget, dest, nsec_file)
    else:
        log.info("Downloading CDK %s v%s (no cache — no nsec)", binary_name, CDK_VERSION)
        url = f"https://github.com/cashubtc/cdk/releases/download/v{CDK_VERSION}/{binary_name}-{CDK_VERSION}-x86_64"
        _run(f"wget -q -O {shlex.quote(dest)} {shlex.quote(url)}", timeout=120)


def ensure_toxiproxy() -> None:
    binary = f"{TOXIPROXY_DIR}/toxiproxy-server"
    r = _run(f"test -x {shlex.quote(binary)} && echo TOXIPROXY_OK", timeout=10, check=False)
    if "TOXIPROXY_OK" in r.stdout:
        log.info("Toxiproxy binary already cached")
        return
    _run(f"mkdir -p {shlex.quote(TOXIPROXY_DIR)}", timeout=10)
    url = f"https://github.com/Shopify/toxiproxy/releases/download/v{TOXIPROXY_VERSION}/toxiproxy-server-linux-amd64"
    log.info("Downloading Toxiproxy v%s", TOXIPROXY_VERSION)
    _run(f"wget -q -O {shlex.quote(binary)} {shlex.quote(url)}", timeout=60)
    _run(f"chmod +x {shlex.quote(binary)}", timeout=10)


def start_toxiproxy() -> subprocess.Popen[str] | None:
    import urllib.request as ur
    try:
        ur.urlopen(f"http://127.0.0.1:{TOXIPROXY_MGMT_PORT}/version", timeout=2)
        log.info("Toxiproxy already running on port %d", TOXIPROXY_MGMT_PORT)
    except Exception:
        binary = f"{TOXIPROXY_DIR}/toxiproxy-server"
        tox_log = Path("/tmp/toxiproxy.log")
        proc = subprocess.Popen(
            [binary, "-host", "0.0.0.0", "-port", str(TOXIPROXY_MGMT_PORT)],
            stdout=tox_log.open("w"),
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        for attempt in range(10):
            try:
                ur.urlopen(f"http://127.0.0.1:{TOXIPROXY_MGMT_PORT}/version", timeout=2)
                log.info("Toxiproxy started (pid=%d, mgmt port=%d)", proc.pid, TOXIPROXY_MGMT_PORT)
                break
            except Exception:
                if proc.poll() is not None:
                    log.error("Toxiproxy exited early (rc=%d)", proc.returncode)
                    return None
                time.sleep(1)
        else:
            log.error("Toxiproxy did not become healthy")
            return None

    create_toxiproxy_proxy(CDK_PROXY_NAME, LOCAL_MINT_HOST, CDK_MINT_PORT, CDK_MINT_INTERNAL_PORT)
    return None


def create_toxiproxy_proxy(name: str, host: str, listen_port: int, upstream_port: int) -> None:
    import requests
    base = f"http://127.0.0.1:{TOXIPROXY_MGMT_PORT}"
    try:
        requests.delete(f"{base}/proxies/{name}", timeout=3)
    except Exception:
        pass
    resp = requests.post(
        f"{base}/proxies",
        json={
            "name": name,
            "listen": f"{host}:{listen_port}",
            "upstream": f"{host}:{upstream_port}",
        },
        timeout=5,
    )
    if resp.status_code in (200, 201):
        log.info("Toxiproxy proxy '%s': %s:%d → %s:%d", name, host, listen_port, host, upstream_port)
    else:
        log.error("Failed to create Toxiproxy proxy: %s %s", resp.status_code, resp.text[:200])


CDK_PROXY_NAME = "cdk_mint"


def _cdk_minor_version(binary: str) -> int:
    import re
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", out.stdout + out.stderr)
        return int(m.group(2)) if m else 0
    except Exception:
        return 0


def start_local_mints(config: WorkerConfig) -> dict[str, subprocess.Popen[str]]:
    mints: dict[str, subprocess.Popen[str]] = {}

    # cashu v0.20+ uses "fixed-window-elastic-expiry" strategy which was
    # removed in limits>=5.0. Pin to 3.14.1 to keep Nutshell mints working.
    _run(
        "/opt/cashu-venv/bin/pip install -q 'limits==3.14.1' 2>/dev/null",
        timeout=60,
        check=False,
    )

    ensure_toxiproxy()

    # --- CDK V2 Mint (internal port 18383, behind Toxiproxy on 8383) ---
    cdk_minor = _cdk_minor_version(f"{CDK_MINT_DIR}/cdk-mintd")
    if cdk_minor >= 18:
        # v0.18+ stores config in the mint DB ([ln] renamed to
        # [payment_backend]; config.toml is ignored at start). Fakewallet
        # value is disposable and the mnemonic is fixed, so always start
        # from a fresh work dir — no legacy-DB migration edge cases.
        cdk_config = f"""\
[info]
url = "http://{LOCAL_MINT_HOST}:{CDK_MINT_PORT}/"
listen_host = "{LOCAL_MINT_HOST}"
listen_port = {CDK_MINT_INTERNAL_PORT}
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
        _run(f"mkdir -p {shlex.quote(CDK_MINT_DIR)}", timeout=10)
        Path(f"{CDK_MINT_DIR}/config.toml").write_text(cdk_config)

        cdk_env = {
            **os.environ,
            "CDK_MINTD_MNEMONIC": "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        }
        _run(
            f"{CDK_MINT_DIR}/cdk-mintd --work-dir {shlex.quote(CDK_MINT_DIR)} "
            f"config validate --file {CDK_MINT_DIR}/config.toml",
            timeout=30,
            env=cdk_env,
        )
        _run(
            f"{CDK_MINT_DIR}/cdk-mintd --work-dir {shlex.quote(CDK_MINT_DIR)} "
            f"config init --new-mint --file {CDK_MINT_DIR}/config.toml",
            timeout=30,
            env=cdk_env,
        )

        cdk_log = Path("/tmp/cdk-mintd.log")
        cdk_proc = subprocess.Popen(
            [f"{CDK_MINT_DIR}/cdk-mintd", "--work-dir", CDK_MINT_DIR],
            cwd=CDK_MINT_DIR,
            env=cdk_env,
            stdout=cdk_log.open("w"),
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    else:
        cdk_config = f"""\
[info]
url = "http://{LOCAL_MINT_HOST}:{CDK_MINT_PORT}/"
listen_host = "{LOCAL_MINT_HOST}"
listen_port = {CDK_MINT_INTERNAL_PORT}
mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"

[database]
engine = "sqlite"

[ln]
ln_backend = "fakewallet"

[fake_wallet]
supported_units = ["sat"]
fee_percent = 0
reserve_fee_min = 0
min_delay_time = 0
max_delay_time = 0
"""
        _run(f"mkdir -p {shlex.quote(CDK_MINT_DIR)}", timeout=10)
        Path(f"{CDK_MINT_DIR}/config.toml").write_text(cdk_config)

        cdk_log = Path("/tmp/cdk-mintd.log")
        cdk_proc = subprocess.Popen(
            [f"{CDK_MINT_DIR}/cdk-mintd", "-c", f"{CDK_MINT_DIR}/config.toml"],
            cwd=CDK_MINT_DIR,
            stdout=cdk_log.open("w"),
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    mints["cdk-v2"] = cdk_proc
    log.info("Started CDK V2 mint (v%d.x, pid=%d, internal port=%d, public port=%d via Toxiproxy)",
             cdk_minor, cdk_proc.pid, CDK_MINT_INTERNAL_PORT, CDK_MINT_PORT)

    start_toxiproxy()

    # --- Nutshell V2 Mint (port 8384) ---
    _run("rm -rf /tmp/nutshell-v2-mint-data && mkdir -p /tmp/nutshell-v2-mint-data", timeout=10)
    ns_v2_log = Path("/tmp/nutshell-v2-mint.log")
    ns_v2_env = {
        **os.environ,
        "CASHU_DIR": "/tmp/nutshell-v2-cashu",
        "MINT_DATABASE": "/tmp/nutshell-v2-mint-data",
        "MINT_BACKEND_BOLT11_SAT": "FakeWallet",
        "MINT_PRIVATE_KEY": "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        "FAKEWALLET_DELAY_OUTGOING_PAYMENT": "0",
        "FAKEWALLET_DELAY_INCOMING_PAYMENT": "0",
        "MINT_RATE_LIMIT": "False",
    }
    ns_v2_proc = subprocess.Popen(
        ["/opt/cashu-venv/bin/python", "-m", "cashu.mint", "--port", str(NUTSHELL_V2_MINT_PORT), "--host", LOCAL_MINT_HOST],
        stdout=ns_v2_log.open("w"),
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
        env=ns_v2_env,
    )
    mints["nutshell-v2"] = ns_v2_proc
    log.info("Started Nutshell V2 mint (pid=%d, port=%d)", ns_v2_proc.pid, NUTSHELL_V2_MINT_PORT)

    # --- Nutshell V1 Mint (port 8385) ---
    _run("rm -rf /tmp/nutshell-v1-mint-data && mkdir -p /tmp/nutshell-v1-mint-data", timeout=10)
    ns_v1_log = Path("/tmp/nutshell-v1-mint.log")
    ns_v1_env = {
        **os.environ,
        "CASHU_DIR": "/tmp/nutshell-v1-cashu",
        "VERSION": "0.19.0",
        "MINT_DATABASE": "/tmp/nutshell-v1-mint-data",
        "MINT_AUTH_DATABASE": "/tmp/nutshell-v1-mint-data",
        "MINT_BACKEND_BOLT11_SAT": "FakeWallet",
        "MINT_PRIVATE_KEY": "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about about",
        "FAKEWALLET_DELAY_OUTGOING_PAYMENT": "0",
        "FAKEWALLET_DELAY_INCOMING_PAYMENT": "0",
        "MINT_RATE_LIMIT": "False",
    }
    ns_v1_proc = subprocess.Popen(
        ["/opt/cashu-venv/bin/python", "-m", "cashu.mint", "--port", str(NUTSHELL_V1_MINT_PORT), "--host", LOCAL_MINT_HOST],
        stdout=ns_v1_log.open("w"),
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
        env=ns_v1_env,
    )
    mints["nutshell-v1"] = ns_v1_proc
    log.info("Started Nutshell V1 mint (pid=%d, port=%d)", ns_v1_proc.pid, NUTSHELL_V1_MINT_PORT)

    # /etc/hosts entries for local mint DNS (idempotent-by-value: remove stale, add correct)
    mint_hosts_line = (
        f"{LOCAL_MINT_HOST} v1.testnut.nutshell.lan v2.testnut.cdk.lan v2.testnut.nutshell.lan "
        f"testnut.cdk.lan testnut.nutshell.lan testnut.v1.nutshell.lan v1.testnut.lan"
    )
    _run(
        f"sed -i '/v1\\.testnut\\.lan/d' /etc/hosts; "
        f"echo '{mint_hosts_line}' >> /etc/hosts",
        check=False,
    )

    # Health checks with early-exit detection
    mint_procs = {"cdk-v2": cdk_proc, "nutshell-v2": ns_v2_proc, "nutshell-v1": ns_v1_proc}
    for name, url in [("cdk-v2", CDK_MINT_URL), ("nutshell-v2", NUTSHELL_V2_MINT_URL), ("nutshell-v1", NUTSHELL_V1_MINT_URL)]:
        for attempt in range(15):
            try:
                req = urllib.request.Request(f"{url}/v1/keys")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status == 200:
                        log.info("Local mint %s healthy at %s", name, url)
                        break
            except Exception:
                pass
            proc = mint_procs[name]
            poll = proc.poll()
            if poll is not None:
                log.error("Local mint %s exited early (rc=%d)", name, poll)
                log_path = {
                    "cdk-v2": "/tmp/cdk-mintd.log",
                    "nutshell-v2": "/tmp/nutshell-v2-mint.log",
                    "nutshell-v1": "/tmp/nutshell-v1-mint.log",
                }[name]
                try:
                    tail = Path(log_path).read_text()[-2000:] if Path(log_path).exists() else "(no log)"
                    log.error("Local mint %s log tail:\n%s", name, tail)
                except Exception:
                    pass
                break
            time.sleep(2)
        else:
            log.warning("Local mint %s not healthy after 30s, continuing anyway", name)

    # Router-side /etc/hosts (idempotent-by-value: remove stale, add correct)
    router_hosts_line = (
        f"{LOCAL_MINT_HOST} v1.testnut.nutshell.lan v2.testnut.cdk.lan v2.testnut.nutshell.lan "
        f"testnut.cdk.lan testnut.nutshell.lan testnut.v1.nutshell.lan v1.testnut.lan"
    )
    _run(
        f"sshpass -p {shlex.quote(VIRT_LAB_PASSWORD)} "
        f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-o ControlPath=none root@{OPENWRT_IP} "
        f"'sed -i \\\"/v1\\\\.testnut\\\\.lan/d\\\" /etc/hosts; "
        f"echo \\\"{router_hosts_line}\\\" >> /etc/hosts; "
        f"killall -HUP dnsmasq 2>/dev/null || true'",
        timeout=15,
        check=False,
    )

    return mints
def stop_local_mints(mints: dict[str, subprocess.Popen[str]]) -> None:
    for name, proc in mints.items():
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except OSError:
                pass
        log.info("Stopped local mint: %s", name)
def _configure_mint(mint_url: str) -> None:
    configure_script = textwrap.dedent(f"""\
        import os, sys
        sys.path.insert(0, '{TEST_DIR}')
        from lib.router import Router
        from lib.backend import BackendConfig
        r = Router(host=os.environ['TOLLGATE_SSH_HOST'], phone_ip='', phone_mac='', domain='',
                   backend=BackendConfig(os.environ.get('TOLLGATE_BACKEND', 'go')))
        r.ssh('cp /tmp/config.json.bak /etc/tollgate/config.json 2>/dev/null || true')
        r.replace_mints(['{mint_url}'], force=True)
    """)
    script_path = "/tmp/configure-mint.py"
    Path(script_path).write_text(configure_script)

    r = _run(
        f"cd {TEST_DIR} && source /opt/tollgate-venv/bin/activate && set -a && source .env && set +a && "
        f"python3 {script_path} 2>&1",
        timeout=120,
        check=False,
    )
    if r.returncode != 0:
        log.warning("Mint configure script failed (rc=%d): %s", r.returncode, (r.stdout or "").strip()[-500:])
    wait_for_backend()
def _quick_mint_cycle_check(mint_url: str, timeout: int = 60) -> bool:
    """Try a quick mint cycle (warmup + mint 4 sats). Returns True if successful."""
    check_script = textwrap.dedent(f"""\
        import os, sys
        sys.path.insert(0, '{TEST_DIR}')
        from lib.cashu import create_minter
        try:
            m = create_minter(mint_url='{mint_url}', venv_path='/opt/cashu-venv')
            m.ensure_mint_available()
            m.warmup(timeout=20)
            token = m.mint(4, timeout={timeout})
            assert token.startswith(('cashuA', 'cashuB')), f'bad token: {{token[:20]}}'
            print('MINT_CYCLE_OK')
        except Exception as e:
            print(f'MINT_CYCLE_FAIL: {{e}}')
    """)
    script_path = "/tmp/mint-cycle-check.py"
    Path(script_path).write_text(check_script)

    r = _run(
        f"cd {TEST_DIR} && source /opt/tollgate-venv/bin/activate && "
        f"set -a && source .env && set +a && "
        f"python3 {script_path} 2>&1",
        timeout=timeout + 30,
        check=False,
    )
    ok = "MINT_CYCLE_OK" in (r.stdout or "")
    if not ok:
        log.warning("Mint cycle check FAILED for %s: %s", mint_url, (r.stdout or "").strip()[-300:])
    return ok


def select_test_mint(forced_mint: str = "auto") -> str:
    """Probe the backend and select the best mint with failover.

    Strategy (backend-aware):
    1. If forced, use that mint directly (no failover).
    2. Try CDK V2 first (all backends). V2 keyset swap verified working
       on Go backend (PR #167, issue #176). Falls back if probe fails.
    3. Try Nutshell V1 (V1 keysets, works with all backends) with mint_cycle
       validation.
    4. Fall back to public testnut.cashu.exchange.

    At each step, if the mint_cycle fails, we move to the next option.
    """
    MINT_ALIASES = {
        "cdk-v2": (CDK_MINT_URL, V2_TESTNUT_CDK_LAN),
        "nutshell-v2": (NUTSHELL_V2_MINT_URL, V2_TESTNUT_NUTSHELL_LAN),
        "nutshell-v1": (NUTSHELL_V1_MINT_URL, V1_TESTNUT_NUTSHELL_LAN),
    }

    backend_type = os.environ.get("TOLLGATE_BACKEND", "go")

    if forced_mint != "auto":
        urls = MINT_ALIASES.get(forced_mint)
        if not urls:
            raise ValueError(f"Unknown mint '{forced_mint}'. Choose from: {', '.join(MINT_ALIASES)}")
        host_url, lan_url = urls
        log.info("Forced mint=%s → %s (LAN: %s)", forced_mint, host_url, lan_url)
        _configure_mint(lan_url)
        _verify_router_mint_reachability(lan_url)
        return lan_url

    PUBLIC_TESTNUTS = "https://testnut.cashu.exchange"

    # --- CDK V2 probe (all backends) ---
    # V2 keyset swap was verified working on Go backend (main @ f4dbf73,
    # issue #176). The previous skip was based on a transient cloud run 8
    # failure that was fixed by PR #167 (register all accepted mints).
    cdk_ok = False
    if True:  # was: if backend_type == "rust"
        try:
            probe_script = textwrap.dedent(f"""\
                import os, json, time, sys
                sys.path.insert(0, '{TEST_DIR}')
                from lib.router import Router
                from lib.backend import BackendConfig
                r = Router(host=os.environ['TOLLGATE_SSH_HOST'], phone_ip='', phone_mac='', domain='',
                           backend=BackendConfig(os.environ.get('TOLLGATE_BACKEND', 'go')))
                r.ssh('cat /etc/tollgate/config.json > /tmp/config.json.bak 2>/dev/null || true')
                r.replace_mints(['{CDK_MINT_URL}'], force=True)
                time.sleep(8)
                code = r.api_status('/')
                body = r.api_body('/') or ''
                try:
                    data = json.loads(body)
                except Exception:
                    data = {{{{}}}}
                has_pps = any(isinstance(t, list) and len(t) > 0 and t[0] == 'price_per_step'
                              for t in data.get('tags', []))
                print(f'v2_probe={{{{code}}}} full_merchant={{{{has_pps}}}}')
            """)
            probe_path = "/tmp/v2-probe.py"
            Path(probe_path).write_text(probe_script)

            r = _run(
                f"cd {TEST_DIR} && source /opt/tollgate-venv/bin/activate && set -a && source .env && set +a && "
                f"python3 {probe_path} 2>&1",
                timeout=120,
                check=False,
            )
            stdout = (r.stdout or "").strip()
            if r.returncode != 0:
                log.warning("V2 probe script failed (rc=%d): %s", r.returncode, stdout[-500:])
            elif "v2_probe=200" in stdout and "full_merchant=True" in stdout:
                cdk_ok = True
                log.info("Rust backend supports V2 keysets — CDK V2 candidate")
            elif "v2_probe=200" in stdout:
                log.info("Rust backend returned 200 with CDK V2 but not full merchant — V2 likely unsupported")
            else:
                log.warning("V2 probe: unexpected output (rc=%d): %s", r.returncode, stdout[-300:])
        except Exception as exc:
            log.warning("V2 probe failed: %s", exc)
    else:
        pass  # CDK V2 probe runs for all backends (issue #176)

    if cdk_ok:
        log.info("Validating CDK V2 mint with mint cycle...")
        if _quick_mint_cycle_check(V2_TESTNUT_CDK_LAN):
            _verify_router_mint_reachability(V2_TESTNUT_CDK_LAN)
            log.info("Selected CDK V2 mint (validated)")
            return V2_TESTNUT_CDK_LAN
        log.warning("CDK V2 mint probe passed but mint cycle FAILED — trying next mint")

    # --- Nutshell V1 (works with all backends) ---
    nutshell_v1_ok = False
    for attempt in range(10):
        try:
            req = urllib.request.Request(f"{NUTSHELL_V1_MINT_URL}/v1/keys")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read())
                    keysets = data.get("keysets", [])
                    if keysets:
                        kid = keysets[0].get("id", "")
                        if kid.startswith("00") and len(kid) == 16:
                            nutshell_v1_ok = True
                            log.info("Nutshell V1 mint has V1 keysets (kid=%s, attempt=%d)", kid, attempt + 1)
                            break
                        log.info("Nutshell V1 mint has V2 keysets (kid=%s), unusable for Go backend", kid[:16])
                        break
        except Exception:
            pass
        if attempt % 3 == 2:
            log.info("Nutshell V1 mint not ready yet (attempt %d/10)", attempt + 1)
        time.sleep(3)

    if nutshell_v1_ok:
        log.info("Configuring Nutshell V1 and validating with mint cycle...")
        _configure_mint(V1_TESTNUT_NUTSHELL_LAN)
        if _quick_mint_cycle_check(V1_TESTNUT_NUTSHELL_LAN):
            _verify_router_mint_reachability(V1_TESTNUT_NUTSHELL_LAN)
            log.info("Selected Nutshell V1 mint (validated)")
            return V1_TESTNUT_NUTSHELL_LAN
        log.warning("Nutshell V1 mint cycle FAILED — trying next mint")

    log.warning("All local mints failed — falling back to public testnut.cashu.exchange")
    _configure_mint(PUBLIC_TESTNUTS)
    _verify_router_mint_reachability(PUBLIC_TESTNUTS)
    return PUBLIC_TESTNUTS


def _verify_router_mint_reachability(mint_url: str) -> None:
    """Verify the OpenWrt router can resolve and reach the chosen mint URL.

    If the DNS-based URL fails, tries the IP-based URL to isolate DNS vs
    network issues. Logs diagnostics and attempts a repair (add route, ping
    to seed ARP) if connectivity is broken.
    """
    ssh_prefix = (
        f"sshpass -p {shlex.quote(VIRT_LAB_PASSWORD)} "
        f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-o ControlPath=none root@{OPENWRT_IP} "
    )

    def _router_curl(url: str) -> str:
        r = _run(
            f"{ssh_prefix}"
            f"'curl -s -o /dev/null -w \"%{{http_code}}\" {url}/v1/keys 2>/dev/null || echo 000'",
            timeout=15,
            check=False,
        )
        return r.stdout.strip()

    # Try the chosen (possibly DNS-based) URL first
    code = _router_curl(mint_url)
    if "200" in code:
        log.info("Router-side mint reachability verified: %s → HTTP %s", mint_url, code)
        return

    log.warning("Router-side mint reachability FAILED: %s → HTTP %s", mint_url, code)

    # Diagnose: try IP-based URL to isolate DNS vs network
    ip_url = f"http://{LOCAL_MINT_HOST}:{NUTSHELL_V1_MINT_PORT}"
    ip_code = _router_curl(ip_url)
    log.warning("Router-side IP reachability: %s → HTTP %s", ip_url, ip_code)

    # Log diagnostics
    diag_r = _run(
        f"{ssh_prefix}"
        f"'echo === hosts ===; grep testnut /etc/hosts; "
        f"echo === route ===; ip route; "
        f"echo === ping ===; ping -c 1 -W 2 {LOCAL_MINT_HOST} 2>&1; "
        f"echo === arp ===; cat /proc/net/arp | head -5'",
        timeout=20,
        check=False,
    )
    log.warning("Router diagnostics:\n%s", diag_r.stdout.strip()[-1000:])

    # Repair attempt: seed ARP table by pinging from router to host
    if "200" not in ip_code:
        log.info("Attempting repair: ping host from router to seed ARP...")
        _run(
            f"{ssh_prefix}"
            f"'ping -c 3 -W 2 {LOCAL_MINT_HOST} 2>/dev/null; "
            f"curl -s -o /dev/null -w \"%{{http_code}}\" {ip_url}/v1/keys 2>/dev/null || echo 000'",
            timeout=20,
            check=False,
        )
        repair_code = _router_curl(mint_url)
        if "200" in repair_code:
            log.info("Router-side mint reachability REPAIRED after ARP seeding: %s → HTTP %s", mint_url, repair_code)
            return

    log.error("Router-side mint reachability FAILED permanently — tests will likely fail")
