import base64
import inspect
import json
import os
import re
import shlex
import socket
import subprocess
import time
import logging

import pytest

try:
    from pytest_html import extras as html_extras  # pyright: ignore[reportMissingImports]
except ImportError:
    html_extras = None

from lib.router import Router
from lib.router_lock import RouterLock
from lib.cashu import CashuMint, MintUnavailableError, TokenPool, create_minter
from lib.clients.adb import ADBDevice
from lib.clients.wifi import WiFi
from lib.clients.desktop import MacWiFiClient, MacAdapter, LinuxWiFiClient, LinuxAdapter
from lib.clients.container import ContainerClient
from lib.clients.cuttlefish import CuttlefishClient
from lib.constants import DEFAULT_STEP_SIZE_MS, NDS_PORTAL_PORT
from lib.backend import BackendConfig, BACKEND_CHOICES_CLI

# --- Mock mode support ---
# When TOLLGATE_MOCK=1 is set, tests run against a MockRouter that returns
# canned responses and serves a local HTTP backend. No router hardware needed.
IS_MOCK_MODE = os.environ.get("TOLLGATE_MOCK", "").lower() in ("1", "true", "yes")
if IS_MOCK_MODE:
    from lib.mock_router import MockRouter, stop_mock_server, get_mock_server

# Register the auto-minting Cashu fixture module so its `minted_token` fixture
# (lib/cashu_fixture.py, Part A of WD6) is discoverable by any test by name.
# Fixtures defined in a non-conftest module are otherwise invisible to pytest.
pytest_plugins = ["lib.cashu_fixture", "lib.film_recorder"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("tollgate.conftest")

SCRIPT_DIR = os.path.join(os.path.dirname(__file__), "..")
MAX_EMBED_SIZE = 20 * 1024 * 1024


def load_env():
    for candidate in [
        os.path.join(SCRIPT_DIR, ".env"),
    ]:
        if os.path.isfile(candidate):
            with open(candidate) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, _, val = line.partition("=")
                        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
            return


load_env()


# Map inventory fields to environment variables they override (only if not already set).
_INVENTORY_ENV_MAP = {
    "sshHost": "TOLLGATE_SSH_HOST",
    "sshUser": "TOLLGATE_SSH_USER",
    "luciUrl": "TOLLGATE_LUCI_URL",
    "arch": "TOLLGATE_ROUTER_ARCH",
    "wifiInterface": "TOLLGATE_WIFI_INTERFACE",
    "tollgateSsidPrefix": "TOLLGATE_SSID_PREFIX",
    "jumpHost": "TOLLGATE_SSH_JUMP_HOST",
    "sshPort": "TOLLGATE_SSH_PORT",
}


def _load_router_inventory():
    if os.environ.get("TOLLGATE_SKIP_ROUTER_INVENTORY"):
        return
    if os.environ.get("TOLLGATE_VIRTUAL_LAB"):
        return
    router_id = os.environ.get("TOLLGATE_ROUTER_ID")
    if not router_id:
        return
    inventory_path = os.environ.get(
        "TOLLGATE_ROUTER_INVENTORY",
        os.path.join(SCRIPT_DIR, "config", "routers.json"),
    )
    if not os.path.isfile(inventory_path):
        log.warning("Router inventory not found: %s", inventory_path)
        return
    with open(inventory_path) as f:
        inventory = json.load(f)
    routers = inventory.get("routers", {})
    if router_id not in routers:
        log.warning("Router ID '%s' not in inventory (available: %s)",
                     router_id, list(routers.keys()))
        return
    entry = routers[router_id]
    for field, env_var in _INVENTORY_ENV_MAP.items():
        value = entry.get(field)
        if value:
            os.environ[env_var] = value
    log.info("Loaded router inventory: %s (%s)", router_id, entry.get("model", "unknown"))


_load_router_inventory()


def _results_dir():
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(SCRIPT_DIR, "results")
    return os.path.join(base, f"test-{ts}")


def _client_mode(request):
    return request.config.getoption("--client")


def _is_publish_mode(config):
    return config.getoption("--publish", default=False)


def _is_container_client(config):
    return config.getoption("--client", default="adb") == "container"


def _container_password() -> str:
    return os.environ.get(
        "TOLLGATE_SSH_PASSWORD",
        os.environ.get("TOLLGATE_LUCI_PASSWORD", "tollgate"),
    )


def _container_ssh_target() -> tuple[str, str | None] | None:
    client_ip = os.environ.get("TOLLGATE_CLIENT_IP", "10.99.99.100")
    container_host = os.environ.get("TOLLGATE_CONTAINER_HOST", client_ip)
    if not container_host:
        return None
    jump_host = os.environ.get("TOLLGATE_SSH_JUMP_HOST", container_host or "")
    if jump_host in {client_ip, "localhost", "127.0.0.1", "::1"}:
        jump_host = ""
    return container_host, jump_host or None


def _run_container_ssh(command: str, timeout: int = 15) -> subprocess.CompletedProcess[str] | None:
    target = _container_ssh_target()
    if target is None:
        return None
    container_host, jump_host = target
    cmd = [
        "sshpass", "-p", _container_password(), "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
    ]
    if jump_host:
        cmd += ["-J", jump_host]
    cmd += [f"root@{container_host}", command]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _prepare_container_nds_client(router, *, deauth: bool = True) -> None:
    """Register the cloud container client with NDS before isolated tests.

    The Debian VM can retain NDS auth/preauth state across tests. Touching the
    portal from the VM after an optional deauth makes NDS recreate client state
    for the configured MAC/IP without relying on cross-test leftovers.
    """
    if not os.environ.get("TOLLGATE_VIRTUAL_LAB"):
        return
    client_mac = os.environ.get("TOLLGATE_CLIENT_MAC", "")
    if not client_mac:
        return
    client_ip = os.environ.get("TOLLGATE_CLIENT_IP", "10.99.99.100")
    quoted_mac = shlex.quote(client_mac)
    lease_pattern = shlex.quote(f" {client_mac} {client_ip} ")
    lease_line = shlex.quote(f"4102444800 {client_mac} {client_ip} gcp-client 01:{client_mac}")
    if deauth:
        router.ssh(f"ndsctl deauth {quoted_mac} 2>/dev/null || true", timeout=10)
    router.ssh(
        f"grep -qi {lease_pattern} /tmp/dhcp.leases 2>/dev/null || "
        f"echo {lease_line} >> /tmp/dhcp.leases",
        timeout=10,
    )
    portal_url = f"http://{router.host}:{NDS_PORTAL_PORT}/"
    quoted_url = shlex.quote(portal_url)
    _run_container_ssh(
        f"curl -s --connect-timeout 3 --max-time 5 -o /dev/null {quoted_url} || true",
        timeout=15,
    )


@pytest.hookimpl(optionalhook=True)
def pytest_metadata(metadata):
    """Add virtual lab metadata to pytest-html report."""
    if os.environ.get("TOLLGATE_SSH_JUMP_HOST"):
        metadata["Router"] = "QEMU x86_64 (virtual lab)"
        metadata["Client"] = "Debian 12 QEMU VM"
        metadata["Jump Host"] = os.environ["TOLLGATE_SSH_JUMP_HOST"]


def pytest_addoption(parser):
    parser.addoption("--binary", default=None,
                     help="Install .ipk file on router before tests")
    parser.addoption("--restore", action="store_true",
                     help="Restore previous binary after tests")
    parser.addoption("--no-deploy", action="store_true",
                     help="Skip portal deploy before phone tests")
    parser.addoption("--results", default=None,
                     help="Custom results directory path")
    parser.addoption("--client", default=os.environ.get("TOLLGATE_CLIENT", "adb"),
                     choices=["adb", "cuttlefish", "mac", "linux", "container"],
                     help="WiFi client mode: adb (Android phone), cuttlefish (Android VM on a remote host, e.g. ai-legion), mac (macOS), linux (NetworkManager/nmcli), or container (Docker via SSH). Default also honors TOLLGATE_CLIENT.")
    parser.addoption("--publish", action="store_true",
                     help="Publish mode: only include screenshots from @pytest.mark.publish_screenshot tests in report")
    parser.addoption("--quick-phone", action="store_true",
                     help="Quick phone mode: skip WiFi reconnect between tests (much faster)")
    parser.addoption("--tollgate-branch", default=None,
                     help="Deploy this branch from CI before tests (e.g. 94-mint-health-rebase-clean)")
    parser.addoption("--tollgate-run-id", default=None,
                     help="Specific CI run ID (implies --tollgate-branch)")
    parser.addoption("--tollgate-force", action="store_true",
                     help="Force redeploy even if router is already healthy")
    parser.addoption("--tollgate-factory-reset", action="store_true",
                     help="Remove TollGate before deploying (clean slate)")
    parser.addoption("--tollgate-arch", default=None,
                     help="Router architecture (default: TOLLGATE_ROUTER_ARCH or aarch64_cortex-a53)")
    parser.addoption("--tollgate-reboot", action="store_true",
                     help="Reboot router after deploy and wait for it to come back")
    parser.addoption("--expected-pr", default=None, type=int,
                     help="PR number being tested. Tests marked @pytest.mark.pr(N) where N != expected_pr are expected to fail/skip.")
    parser.addoption("--backend", default=None, choices=list(BACKEND_CHOICES_CLI),
                     help="TollGate backend type: 'go' (Go v1), 'rust' (Rust v1 / tollgate-rs), or 'rust-basic' (tollgate-module-basic-rust). Default: TOLLGATE_BACKEND env or 'go'")
    parser.addoption("--lock-phase", default=None,
                     help="Auto-acquire router lock with this phase description. "
                          "Prevents concurrent sessions on the same router.")
    parser.addoption("--lab-type", default=None,
                     choices=["virtual-lab", "gcloud", "physical", "browserstack"],
                     help="Lab environment type. Default: TOLLGATE_LAB_TYPE env or 'physical'")


def pytest_configure(config):
    lab_type_opt = config.getoption("--lab-type", default=None)
    if lab_type_opt:
        os.environ["TOLLGATE_LAB_TYPE"] = lab_type_opt
    elif IS_MOCK_MODE:
        os.environ["TOLLGATE_LAB_TYPE"] = "mock"
    elif not os.environ.get("TOLLGATE_LAB_TYPE"):
        if os.environ.get("TOLLGATE_VIRTUAL_LAB"):
            os.environ["TOLLGATE_LAB_TYPE"] = "virtual-lab"
        else:
            os.environ["TOLLGATE_LAB_TYPE"] = "physical"

    if IS_MOCK_MODE:
        from lib.mock_router import get_mock_server as _gms
        _srv = _gms()
        os.environ["TOLLGATE_BACKEND_URL"] = _srv.url


@pytest.fixture(scope="session")
def results_dir(request):
    custom = request.config.getoption("--results")
    rd = custom or _results_dir()
    os.makedirs(os.path.join(rd, "raw"), exist_ok=True)
    os.makedirs(os.path.join(rd, "report"), exist_ok=True)
    return rd


@pytest.fixture(scope="session")
def backend(request):
    opt = request.config.getoption("--backend")
    return BackendConfig(backend_type=opt)


@pytest.fixture(scope="session")
def router(request, backend):
    if IS_MOCK_MODE:
        log.info("TOLLGATE_MOCK=1 — using MockRouter (no physical router needed)")
        phone_ip = os.environ.get("TOLLGATE_CLIENT_IP", "10.0.0.100")
        phone_mac = os.environ.get("TOLLGATE_CLIENT_MAC", "00:11:22:33:44:55")
        mock = MockRouter(
            host="127.0.0.1",
            phone_ip=phone_ip,
            phone_mac=phone_mac,
            backend=backend,
        )
        return mock

    if os.environ.get("TOLLGATE_VENUE") == "labgrid":
        from lib.lab_inventory import InventoryError, load_inventory
        from lib.labgrid_venue import LabgridBenchError, apply_venue_env, bench_for_router

        try:
            labgrid_inv = load_inventory()
            binding = bench_for_router(
                labgrid_inv, os.environ.get("TOLLGATE_LABGRID_PLACE", "ap-lan2")
            )
        except (InventoryError, LabgridBenchError) as e:
            pytest.exit(f"labgrid venue unavailable: {e}", returncode=4)
        binding.bench.acquire()
        request.addfinalizer(binding.bench.release)
        apply_venue_env(binding.entry)
        log.info("labgrid venue: acquired place %s", binding.entry.place)

    host = os.environ.get("TOLLGATE_SSH_HOST") or os.environ.get("ROUTER_IP")
    identity_file = os.environ.get("TOLLGATE_SSH_KEY", "")
    jump_host = os.environ.get("TOLLGATE_SSH_JUMP_HOST", "")

    # Virtual lab uses password auth (sshpass) through jump host.
    # SSH key auth fails without agent forwarding (-A), so clear identity_file.
    if os.environ.get("TOLLGATE_VIRTUAL_LAB") and jump_host:
        identity_file = ""
    client = _client_mode(request)
    phone_ip = os.environ.get("TOLLGATE_CLIENT_IP", "")
    phone_mac = os.environ.get("TOLLGATE_CLIENT_MAC", "")

    if client in ("mac", "linux"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            phone_ip = phone_ip or s.getsockname()[0]
        except Exception:
            phone_ip = phone_ip or ""
        finally:
            s.close()
        if client == "mac":
            phone_mac = phone_mac or MacWiFiClient().mac_address
        else:
            phone_mac = phone_mac or LinuxWiFiClient().mac_address
        log.info(f"--client={client}: auto-detected WiFi MAC {phone_mac}")
    elif client == "container":
        phone_ip = phone_ip or "10.99.99.100"
        phone_mac = phone_mac or "02:00:00:00:00:01"
        log.info(f"--client=container: using container IP {phone_ip}, MAC {phone_mac}")
    else:
        phone_ip = phone_ip or ""
        phone_mac = phone_mac or ""
    domain = os.environ.get("TOLLGATE_DOMAIN", "")

    if not host:
        log.warning("TOLLGATE_SSH_HOST or ROUTER_IP not set — router fixture returns None")
        return None

    ssh_port = os.environ.get("TOLLGATE_SSH_PORT", "")

    router = Router(
        host=host,
        phone_ip=phone_ip,
        phone_mac=phone_mac,
        domain=domain,
        identity_file=identity_file or None,
        jump_host=jump_host or None,
        port=int(ssh_port) if ssh_port else None,
        backend=backend,
    )

    # Rust backend's DhcpLeasesResolver needs the client IP in /tmp/dhcp.leases.
    # In cloud lab the container uses a static IP that never appears via DHCP.
    if client == "container" and phone_ip and phone_mac:
        router.ensure_dhcp_lease()

    return router


@pytest.fixture(scope="session")
def secondary_router(backend):
    host = os.environ.get("TOLLGATE_SECONDARY_ROUTER_HOST", "")
    if not host:
        yield None
        return

    password = os.environ.get(
        "TOLLGATE_SECONDARY_ROUTER_PASSWORD",
        os.environ.get("TOLLGATE_SSH_PASSWORD", os.environ.get("TOLLGATE_LUCI_PASSWORD", "")),
    )
    port = os.environ.get("TOLLGATE_SECONDARY_ROUTER_PORT", "")
    original_password = os.environ.get("TOLLGATE_SSH_PASSWORD")
    if password:
        os.environ["TOLLGATE_SSH_PASSWORD"] = password
    try:
        secondary = Router(
            host=host,
            phone_ip=os.environ.get("TOLLGATE_SECONDARY_CLIENT_IP", ""),
            phone_mac=os.environ.get("TOLLGATE_SECONDARY_CLIENT_MAC", ""),
            domain=os.environ.get("TOLLGATE_SECONDARY_DOMAIN", ""),
            identity_file=os.environ.get("TOLLGATE_SECONDARY_ROUTER_SSH_KEY", "") or None,
            jump_host=os.environ.get("TOLLGATE_SECONDARY_ROUTER_JUMP_HOST", "") or None,
            port=int(port) if port else None,
            backend=backend,
        )
    finally:
        if original_password is None:
            os.environ.pop("TOLLGATE_SSH_PASSWORD", None)
        else:
            os.environ["TOLLGATE_SSH_PASSWORD"] = original_password

    yield secondary
    secondary.close()


@pytest.fixture(scope="session")
def chain_routers(backend):
    hosts_str = os.environ.get("TOLLGATE_CHAIN_ROUTER_HOSTS", "")
    count_str = os.environ.get("TOLLGATE_CHAIN_ROUTER_COUNT", "0")
    count = int(count_str) if count_str else 0

    if count < 3 or not hosts_str:
        yield []
        return

    hosts = hosts_str.split()
    routers: list[Router] = []
    for host in hosts:
        r = Router(
            host=host,
            phone_ip="",
            phone_mac="",
            domain="",
            backend=backend,
        )
        routers.append(r)

    yield routers

    for r in routers:
        r.close()


@pytest.fixture(scope="session", autouse=True)
def deploy_session(request, router, backend):
    if IS_MOCK_MODE:
        log.info("TOLLGATE_MOCK=1 — skipping deployment and health checks")
        yield
        stop_mock_server()
        return

    # Parity tests manage their own Go+Rust backends on port 2121.
    # Skip the session-level health check so the parity fixtures can bind.
    _args = getattr(request.config, "args", []) or []
    _parity_only = bool(_args) and all(
        "parity" in str(a).replace(os.sep, "/") for a in _args
    )
    if _parity_only:
        log.info("Parity tests only — skipping session deployment and health checks")
        yield
        return

    from lib import deploy as deploy_lib

    if router is None:
        log.info("Skipping deployment (no router available)")
        yield
        return

    binary = request.config.getoption("--binary")
    restore = request.config.getoption("--restore")
    tg_branch = request.config.getoption("--tollgate-branch")
    tg_run_id = request.config.getoption("--tollgate-run-id")
    tg_force = request.config.getoption("--tollgate-force")
    tg_reset = request.config.getoption("--tollgate-factory-reset")
    tg_arch = request.config.getoption("--tollgate-arch")
    tg_reboot = request.config.getoption("--tollgate-reboot")
    no_deploy = request.config.getoption("--no-deploy")

    # Unit tests don't need router connectivity. Skip deployment when running
    # only tests under tests/unit/ and no explicit deploy source is specified.
    _no_deploy_source = not binary and not tg_branch and not tg_run_id
    _args = getattr(request.config, "args", []) or []
    _unit_only = bool(_args) and all("unit" in str(a).replace(os.sep, "/") for a in _args)
    if no_deploy or (_no_deploy_source and _unit_only):
        log.info("Skipping deployment (unit tests only or --no-deploy)")
        yield
        return

    if binary:
        subprocess.run(
            ["bash", os.path.join(SCRIPT_DIR, "scripts", "deploy.sh"), binary, "--restart"],
            check=False,
        )
    elif tg_branch or tg_run_id:
        if tg_reset:
            log.info("Factory resetting router before deploy")
            deploy_lib.factory_reset(router, reboot=tg_reboot)

        branch = tg_branch or "main"
        result = deploy_lib.deploy_branch(
            router, branch,
            arch=tg_arch,
            run_id=tg_run_id,
            force=tg_force,
            reboot=tg_reboot,
            backend=backend,
        )
        if not result["success"]:
            pytest.exit(
                f"Deploy failed: version={result.get('installed_version')}, "
                f"health={result.get('health_code')}",
                returncode=1,
            )
        log.info("Deployed: version=%s", result.get("installed_version"))

    if not no_deploy:
        code = router.api_status("/")
        if code != 200:
            pytest.exit(f"Backend not reachable at {router.host}:2121 (HTTP {code})", returncode=1)

        router.enable_debug_portal()
        router.ensure_test_mint()
        router.replace_mints()
        for _ in range(60):
            if router.api_status("/") == 200:
                break
            time.sleep(1)
        else:
            pytest.exit(f"Backend not reachable after test mint setup at {router.host}:2121", returncode=1)

        if _is_container_client(request.config) and os.environ.get("TOLLGATE_CLIENT_MAC"):
            _prepare_container_nds_client(router)

    if _is_container_client(request.config) and os.environ.get("TOLLGATE_VIRTUAL_LAB"):
        container_host = os.environ.get("TOLLGATE_CONTAINER_HOST", "")
        client_ip = os.environ.get("TOLLGATE_CLIENT_IP", "10.99.99.100")
        client_mac = os.environ.get("TOLLGATE_CLIENT_MAC", "")
        jump_host = os.environ.get("TOLLGATE_SSH_JUMP_HOST", container_host or "")
        if jump_host == client_ip:
            jump_host = ""
        password = _container_password()
        request.session._tollgate_adb = ContainerClient(
            host=container_host or None,
            jump_host=jump_host or None,
            client_ip=client_ip,
            client_mac=client_mac or None,
            password=password,
        )
        log.info("Pre-created ContainerClient for auto-screenshots")

    yield

    try:
        if restore:
            print("\n[deploy] Restoring previous binary")
    finally:
        if not no_deploy:
            router.disable_debug_portal()
        router.close()


@pytest.fixture(scope="session")
def adb(request, router):
    client = _client_mode(request)
    if client == "mac":
        mac = MacWiFiClient()
        return MacAdapter(mac, router_domain=router.domain)
    if client == "linux":
        linux = LinuxWiFiClient()
        return LinuxAdapter(linux, router_domain=router.domain)
    if client == "container":
        container_host = os.environ.get("TOLLGATE_CONTAINER_HOST", "")
        client_ip = os.environ.get("TOLLGATE_CLIENT_IP", "10.99.99.100")
        client_mac = os.environ.get("TOLLGATE_CLIENT_MAC", "")
        jump_host = os.environ.get("TOLLGATE_SSH_JUMP_HOST", container_host or "")
        if jump_host == client_ip:
            jump_host = ""
        password = _container_password()
        client = ContainerClient(
            host=container_host or None,
            jump_host=jump_host or None,
            client_ip=client_ip,
            client_mac=client_mac or None,
            password=password,
        )
        request.session._tollgate_adb = client
        return client
    if client == "cuttlefish":
        cf_host = os.environ.get("TOLLGATE_CF_HOST", "ai-legion")
        cf_serial = os.environ.get("TOLLGATE_CF_SERIAL", "0.0.0.0:6520")
        cf = CuttlefishClient(host=cf_host, serial=cf_serial)
        request.session._tollgate_adb = cf
        return cf
    serial = os.environ.get("PHONE_SERIAL", "")
    pin = os.environ.get("PHONE_PIN", "")
    return ADBDevice(serial=serial, pin=pin)


@pytest.fixture(scope="session")
def cashu():
    if IS_MOCK_MODE:
        pytest.skip("cashu fixture not available in mock mode (no real mint)")
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL", "https://testnut.cashu.exchange")
    minter = create_minter(mint_url)
    for attempt in range(5):
        try:
            minter.ensure_mint_available(timeout=10)
            minter.warmup(timeout=30)
            pool_size = int(os.environ.get("TOLLGATE_TOKEN_POOL_SIZE", "10"))
            if pool_size > 0:
                return TokenPool(minter, pool_size=pool_size)
            return minter
        except MintUnavailableError as exc:
            if attempt < 4:
                time.sleep(5)
            else:
                pytest.skip(f"cashu mint unavailable after 5 retries: {exc}")


@pytest.fixture(scope="session")
def all_routers(backend):
    identity_file = os.environ.get("TOLLGATE_SSH_KEY", "")
    inventory_path = os.environ.get(
        "TOLLGATE_ROUTER_INVENTORY",
        os.path.join(SCRIPT_DIR, "config", "routers.json"),
    )
    if not os.path.isfile(inventory_path):
        return {}
    with open(inventory_path) as f:
        inventory = json.load(f)
    routers = {}
    for router_id, entry in inventory.get("routers", {}).items():
        host = entry.get("sshHost")
        if not host:
            continue
        routers[router_id] = Router(
            host=host,
            phone_ip="",
            phone_mac="",
            domain="",
            identity_file=identity_file or None,
            jump_host=entry.get("jumpHost") or None,
            port=int(entry["sshPort"]) if entry.get("sshPort") else None,
            backend=backend,
        )
    return routers


@pytest.fixture(scope="session")
def wifi(adb, router):
    ssid = os.environ.get("TOLLGATE_SSID", "TollGate")
    return WiFi(adb=adb, router=router, ssid=ssid)


@pytest.fixture(autouse=True)
def attach_results(request, results_dir):
    request.node._results_dir = results_dir


@pytest.fixture(autouse=True)
def _mock_fast_sleep():
    """In mock mode, patch time.sleep to be near-instant for long sleeps.

    Tests with hardcoded time.sleep(15) or time.sleep(35) would exceed
    pytest --timeout=10 and hang the suite. In mock mode there is no real
    hardware timing to wait for, so we cap long sleeps at 0.01s.
    """
    if not IS_MOCK_MODE:
        yield
        return
    import time as _time
    import unittest.mock as _mock
    orig_sleep = _time.sleep
    def _fast_sleep(secs):
        orig_sleep(0.01 if secs > 1 else secs)
    with _mock.patch("time.sleep", side_effect=_fast_sleep):
        yield


@pytest.fixture(autouse=True)
def container_nds_preflight(request):
    if not _is_container_client(request.config):
        return
    if not os.environ.get("TOLLGATE_VIRTUAL_LAB"):
        return
    if "api" not in request.node.keywords:
        return
    router = request.getfixturevalue("router")
    _prepare_container_nds_client(router)

    client_mac = os.environ.get("TOLLGATE_CLIENT_MAC", "")
    if client_mac:
        router.ssh(f"ndsctl deauth {client_mac} 2>/dev/null || true", timeout=10)


def _get_pay_via_marker(item):
    """Extract pay_via value from @pytest.mark.pay_via(X) marker."""
    for marker in item.iter_markers("pay_via"):
        if marker.args:
            return marker.args[0]
    return "portal"  # default: open portal


@pytest.fixture
def connected_wifi(router, wifi, adb, request):
    quick = request.config.getoption("--quick-phone", default=False)
    pay_via = _get_pay_via_marker(request.node)
    skip_portal = pay_via == "skip"
    router.resolve_phone_client(adb)
    router.reset_state(adb=adb)
    if quick:
        if not wifi.is_connected():
            assert wifi.reconnect(skip_portal=skip_portal), "WiFi reconnect failed — portal did not render"
    else:
        assert wifi.reconnect(skip_portal=skip_portal), "WiFi reconnect failed — portal did not render"
    router.resolve_phone_client(adb)
    yield


@pytest.fixture
def test_pricing(router):
    if router.backend.has_config_json:
        router.ssh("cp /etc/tollgate/config.json /etc/tollgate/config.json.test-backup")

    def apply(step_size: int = DEFAULT_STEP_SIZE_MS, metric: str = "milliseconds"):
        router.apply_pricing(step_size=step_size, metric=metric)

    yield apply

    router.restore_pricing()


@pytest.fixture
def screenshot_portal(adb, results_dir, request):
    report_dir = os.path.join(results_dir, "report")
    publish_mode = _is_publish_mode(request.config)
    can_publish = "publish_screenshot" in request.keywords
    container_mode = _is_container_client(request.config)

    def take(name: str):
        raw_path = os.path.join(results_dir, "raw", name)
        adb.screenshot_portal(raw_path, report_dir=report_dir)

        if publish_mode and not can_publish and not container_mode:
            return

        try:
            with open(raw_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            if b64 and html_extras is not None:
                extras_list = getattr(request.node, "_screenshot_extras", [])
                extras_list.append(html_extras.image(b64, name))
                request.node._screenshot_extras = extras_list
        except Exception as exc:
            log.debug(f"screenshot embed skipped: {exc}")

    return take


@pytest.fixture
def screenshot_raw(adb, results_dir):
    def take(name: str):
        path = os.path.join(results_dir, "raw", name)
        adb.screenshot(path)
        return path

    return take


def pytest_runtest_setup(item):
    backend_type = item.config.getoption("--backend", default=None) or os.environ.get("TOLLGATE_BACKEND", "go")

    if "go_only" in item.keywords and backend_type == "rust":
        pytest.skip("Go-only test (LuCI, CLI socket, or sessions.json)")

    if "rust_only" in item.keywords and backend_type == "go":
        pytest.skip("Rust-only test")

    if (
        "reseller_scenario" in item.keywords
        and os.environ.get("TOLLGATE_ENABLE_RESELLER_SCENARIOS") != "1"
    ):
        pytest.skip("set TOLLGATE_ENABLE_RESELLER_SCENARIOS=1 to run reseller scenarios")

    expected_pr = item.config.getoption("--expected-pr")
    if expected_pr:
        pr_num = _get_pr_marker(item)
        if pr_num is not None and pr_num != expected_pr:
            pytest.skip(
                f"PR-specific test for #{pr_num} (testing #{expected_pr})"
            )

    # Lab type filtering
    lab_type = os.environ.get("TOLLGATE_LAB_TYPE", "")
    if not lab_type and os.environ.get("TOLLGATE_VIRTUAL_LAB"):
        lab_type = "virtual-lab"
    if not lab_type:
        lab_type = "physical"

    if "physical_only" in item.keywords and lab_type != "physical":
        pytest.skip(f"Physical-only test (current lab: {lab_type})")

    if "virtual_lab_only" in item.keywords and lab_type != "virtual-lab":
        pytest.skip(f"Virtual-lab-only test (current lab: {lab_type})")

    if "gcloud_only" in item.keywords and lab_type != "gcloud":
        pytest.skip(f"GCloud-only test (current lab: {lab_type})")

    client_mode = item.config.getoption("--client")
    if client_mode in ("mac", "linux", "container"):
        if "android_only" in item.keywords:
            pytest.skip("Android-only test (requires physical device)")
        if "requires_wifi" in item.keywords:
            pytest.skip("requires WiFi adapter (--client=container has no WiFi)")
        return

    if "phone" in item.keywords:
        serial = os.environ.get("PHONE_SERIAL", "")
        if not serial:
            pytest.skip("PHONE_SERIAL not set, phone tests require ADB device")


def pytest_collection_modifyitems(config, items):
    # Tier hierarchy: smoke ⊂ critical ⊂ extended
    for item in items:
        if "smoke" in item.keywords:
            item.add_marker(pytest.mark.critical)
            item.add_marker(pytest.mark.extended)
        elif "critical" in item.keywords:
            item.add_marker(pytest.mark.extended)

    scenarios_skip = pytest.mark.skip(
        reason="hardware scenarios not run in virtual lab / cloud worker"
    )
    for item in items:
        fspath = str(item.fspath).replace(os.sep, "/")
        if "/tests/scenarios/" in fspath:
            item.add_marker(pytest.mark.hardware)
        if os.environ.get("TOLLGATE_VIRTUAL_LAB") and "hardware" in item.keywords:
            if "virtual_lab" not in item.keywords:
                item.add_marker(scenarios_skip)

    for item in items:
        if "phone" in item.keywords:
            item.add_marker(pytest.mark.flaky(reruns=1, reruns_delay=5))
            item.add_marker(pytest.mark.timeout(300))

    # Cashu-dependent tests get a retry — the Nutshell V1 FakeWallet
    # is inherently flaky under load (subprocess deadlocks).
    for item in items:
        if "complete" in item.keywords:
            continue
        if "publish_screenshot" in item.keywords or "virtual_lab" in item.keywords:
            continue
        fixturenames = getattr(item, "fixturenames", [])
        if "cashu" in fixturenames:
            item.add_marker(pytest.mark.flaky(reruns=1, reruns_delay=3))

    api = [t for t in items if "api" in t.keywords]
    phone = [t for t in items if "phone" in t.keywords]
    other = [t for t in items if "api" not in t.keywords and "phone" not in t.keywords]
    items[:] = api + other + phone

    if IS_MOCK_MODE:
        _mock_skip = pytest.mark.skip(
            reason="skipped in mock mode (requires external binaries or real hardware state)"
        )
        _mock_skip_fnames = {
            "test_go_rust_basic_parity.py",
            "test_mint_wallet_compat.py",
            "test_nds_gating.py",
        }
        for item in items:
            fname = os.path.basename(str(item.fspath))
            if fname in _mock_skip_fnames:
                item.add_marker(_mock_skip)

    metadata = {}
    for item in items:
        doc = ""
        if hasattr(item, "function") and item.function.__doc__:
            doc = inspect.getdoc(item.function) or ""
        markers = sorted(set(m.name for m in item.iter_markers() if m.name))
        metadata[item.nodeid] = {
            "name": item.name,
            "description": doc,
            "markers": markers,
            "file": str(item.fspath).replace(os.sep, "/"),
        }
    config._tollgate_test_metadata = metadata


def _save_test_metadata(session):
    metadata = getattr(session.config, "_tollgate_test_metadata", None)
    if not metadata:
        return
    results_dir = session.config.getoption("--results") or _results_dir()
    os.makedirs(results_dir, exist_ok=True)
    meta_path = os.path.join(results_dir, "test-metadata.json")
    existing = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    existing.update(metadata)
    with open(meta_path, "w") as f:
        json.dump(existing, f, indent=2)

    if _debug_log_data:
        debug_path = os.path.join(results_dir, "debug-logs.json")
        existing_debug = {}
        if os.path.isfile(debug_path):
            try:
                with open(debug_path) as f:
                    existing_debug = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        existing_debug.update(_debug_log_data)
        with open(debug_path, "w") as f:
            json.dump(existing_debug, f, indent=2)


_debug_log_data = {}


def _write_debug_log(item, report, results_dir):
    if not results_dir or not report:
        return
    if report.passed:
        return

    entry = {
        "outcome": report.outcome,
        "duration_ms": round(report.duration * 1000),
        "stdout": getattr(report, "capstdout", "") or "",
        "stderr": getattr(report, "capstderr", "") or "",
        "logs": getattr(report, "caplog", "") or "",
        "traceback": str(report.longrepr) if report.failed and report.longrepr else None,
        "failure_message": None,
    }

    if report.failed:
        for attr in ("longreprtext", "longrepr"):
            val = getattr(report, attr, None)
            if val:
                entry["failure_message"] = str(val)[:500]
                break

    _debug_log_data[item.name] = entry


def _get_pr_marker(item):
    """Extract PR number from @pytest.mark.pr(N) marker."""
    for marker in item.iter_markers("pr"):
        if marker.args:
            return marker.args[0]
    return None


def _pr_label_for_item(item):
    pr_num = _get_pr_marker(item)
    return f" [PR#{pr_num}]" if pr_num else ""


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    outcome = yield
    exc_info = outcome.excinfo
    if exc_info and issubclass(exc_info[0], MintUnavailableError):
        outcome.force_exception(pytest.skip.Exception(f"cashu mint unavailable: {exc_info[1]}"))


def pytest_runtest_logreport(report):
    if report.when == "call" and hasattr(report, "nodeid"):
        report._pr_label = ""


def _debug_summary(adb, router) -> str:
    lines = ["\n\n=== DEBUG ON FAILURE ==="]
    if router:
        try:
            state = router.get_nds_state()
            lines.append(f"ndsctl state: {state}")
        except Exception:
            pass
        try:
            portal_log = router.get_portal_log()
            if portal_log:
                lines.append(f"portal log (last 500 chars): {portal_log[-500:]}")
        except Exception:
            pass
        try:
            session = router.get_session()
            lines.append(f"session: {str(session)[:300]}")
        except Exception:
            pass
        try:
            nds_clients = router.ssh("timeout 5 ndsctl clients 2>/dev/null || true", timeout=10)
            mac = router.phone_mac
            if mac and mac in nds_clients:
                for line in nds_clients.split("\n"):
                    if "state=" in line:
                        lines.append(f"ndsctl client detail: {line.strip()}")
                        break
        except Exception:
            pass
        try:
            ipv6 = router.ssh("ip -6 addr show br-lan scope global 2>/dev/null | wc -l", timeout=5)
            lines.append(f"global IPv6 on br-lan: {ipv6.strip()}")
        except Exception:
            pass
    if adb and hasattr(adb, "ui_xml"):
        try:
            xml = adb.ui_xml(timeout=5)
            texts = re.findall(r'text="([^"]{3,})"', xml)
            if texts:
                lines.append(f"phone UI text: {texts[:15]}")
            sm = re.search(r'data-sm="([^"]*)"', xml)
            if sm:
                lines.append(f"portal state machine: {sm.group(1)}")
        except Exception:
            pass
    return "\n".join(lines)


def _auto_portal_screenshot(item, report, results_dir, adb):
    if not _is_container_client(item.config):
        return
    if not os.environ.get("TOLLGATE_VIRTUAL_LAB"):
        return
    if not results_dir or not adb:
        return
    if getattr(item, "_screenshot_extras", None):
        return

    raw = os.path.join(results_dir, "raw")
    os.makedirs(raw, exist_ok=True)

    if report.failed:
        status = "failed"
    elif report.skipped:
        status = "skipped"
    else:
        status = "passed"
    safe_name = re.sub(r'[^\w\-.]', '_', item.name)
    img_path = os.path.join(raw, f"{safe_name}-{status}.png")

    try:
        adb.screenshot(img_path)
    except Exception as exc:
        log.debug("auto portal screenshot capture failed: %s", exc)
        return

    if not os.path.isfile(img_path):
        return

    if html_extras is not None:
        try:
            with open(img_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            if b64:
                extras = list(getattr(report, "extras", []))
                extras.append(html_extras.image(b64, f"{safe_name}-{status}"))
                report.extras = extras
        except Exception as exc:
            log.debug("auto portal screenshot embed failed: %s", exc)


def _video_is_static(video_path: str, pixel_threshold: int = 5, min_diff_pixels: int = 10) -> bool:
    """Check if a video shows no meaningful visual change across ALL frames.

    Returns True (assume static) if ffmpeg is not installed, so test reporting
    is never disrupted by a missing optional dependency on cloud lab VMs.
    """
    import shutil as _shutil
    if not _shutil.which("ffmpeg"):
        return True
    extract = subprocess.run(
        ["ffmpeg", "-y", "-i", video_path,
         "-vf", "scale=160:90",
         "-f", "image2pipe", "-vcodec", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, timeout=30,
    )

    raw = extract.stdout
    if not raw:
        return False

    frame_size = 160 * 90 * 3
    n_frames = len(raw) // frame_size

    if n_frames <= 1:
        return True

    for i in range(n_frames - 1):
        f1 = raw[i * frame_size : (i + 1) * frame_size]
        f2 = raw[(i + 1) * frame_size : (i + 2) * frame_size]
        diffs = sum(1 for a, b in zip(f1, f2, strict=False) if abs(a - b) > pixel_threshold)
        if diffs > min_diff_pixels:
            return False

    return True

    return False


def _auto_portal_video(item, report, results_dir, adb):
    if not _is_container_client(item.config):
        return
    if not os.environ.get("TOLLGATE_VIRTUAL_LAB"):
        return
    if not results_dir or not adb or not hasattr(adb, "record_portal_video"):
        return
    if report.failed and os.environ.get("TOLLGATE_AUTO_VIDEO_ON_FAILURE") != "1":
        return
    if not report.failed and not (report.passed and os.environ.get("TOLLGATE_RECORD_ALL") == "1"):
        return
    if report.passed and "virtual_lab" not in item.keywords and "publish_screenshot" not in item.keywords:
        return

    raw = os.path.join(results_dir, "raw")
    os.makedirs(raw, exist_ok=True)

    status = "failed" if report.failed else "passed"
    safe_name = re.sub(r'[^\w\-.]', '_', item.name)
    video_path = os.path.join(raw, f"{safe_name}-{status}.webm")

    try:
        ok = adb.record_portal_video(video_path)
    except Exception as exc:
        log.debug("auto portal video capture failed: %s", exc)
        return

    if not ok or not os.path.isfile(video_path):
        return

    if _video_is_static(video_path):
        os.remove(video_path)
        log.debug("skipped static video (no visual change): %s", video_path)
        return

    if html_extras is None:
        return

    try:
        if os.path.getsize(video_path) > MAX_EMBED_SIZE:
            return
        with open(video_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        if b64:
            extras = list(getattr(report, "extras", []))
            extras.append(
                html_extras.video(
                    b64,
                    name=f"{safe_name}-{status}",
                    mime_type="video/webm",
                    extension="webm",
                )
            )
            report.extras = extras
    except Exception as exc:
        log.debug("auto portal video embed failed: %s", exc)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()

    screenshot_extras = getattr(item, "_screenshot_extras", None)
    if screenshot_extras and report.when == "call" and html_extras is not None:
        extras = list(getattr(report, "extras", []))
        extras.extend(screenshot_extras)
        report.extras = extras

    if report.when == "call":
        results_dir = getattr(item, "_results_dir", None)
        adb = item.funcargs.get("adb")
        router = item.funcargs.get("router")
        safe_name = re.sub(r'[^\w\-.]', '_', item.name)

        if not adb:
            adb = getattr(item.session, "_tollgate_adb", None)

        _auto_portal_screenshot(item, report, results_dir, adb)
        _auto_portal_video(item, report, results_dir, adb)
        _write_debug_log(item, report, results_dir)

        if report.failed:
            raw = os.path.join(results_dir, "raw") if results_dir else None
            if raw:
                os.makedirs(raw, exist_ok=True)

            if adb and raw:
                try:
                    img_path = os.path.join(raw, f"{safe_name}-failed-full.png")
                    adb.screenshot(img_path)

                    if os.path.isfile(img_path) and html_extras is not None:
                        with open(img_path, "rb") as f:
                            b64 = base64.b64encode(f.read()).decode()
                        if b64:
                            extras = list(getattr(report, "extras", []))
                            extras.append(html_extras.image(b64, f"{safe_name}-failed-full"))
                            report.extras = extras
                except Exception:
                    pass
                if hasattr(adb, "ui_xml") and raw:
                    try:
                        xml_path = os.path.join(raw, f"{safe_name}-ui.xml")
                        xml = adb.ui_xml()
                        with open(xml_path, "w") as f:
                            f.write(xml)
                        if html_extras is not None:
                            texts = re.findall(r'text="([^"]{3,})"', xml)
                            sm = re.search(r'data-sm="([^"]*)"', xml)
                            summary = f"Phone UI texts: {texts[:15]}"
                            if sm:
                                summary += f"\nPortal state: {sm.group(1)}"
                            extras = list(getattr(report, "extras", []))
                            extras.append(html_extras.text(summary, name="phone-ui"))
                            report.extras = extras
                    except Exception:
                        pass
            if router and "unit" not in str(item.fspath).replace(os.sep, "/"):
                try:
                    router.collect_logs(results_dir, adb=adb, bundle=safe_name)
                except Exception:
                    pass
                try:
                    report.longrepr = str(report.longrepr) + _debug_summary(adb, router)
                except Exception:
                    pass


_session_lock: RouterLock | None = None
_hardware_lock_acquired: bool = False


@pytest.fixture(scope="session")
def serial_console(router):
    """Serial console for the current router when TOLLGATE_SERIAL_PORT is set."""
    port = os.environ.get("TOLLGATE_SERIAL_PORT", "").strip()
    if not port:
        pytest.skip("TOLLGATE_SERIAL_PORT not set")
    from lib.serial_console import SerialConsole
    return SerialConsole(port)


def pytest_sessionstart(session):
    global _session_lock, _hardware_lock_acquired
    lock_phase = session.config.getoption("--lock-phase", default=None)
    use_hardware = (
        lock_phase
        or os.environ.get("TOLLGATE_USE_HARDWARE_LOCK", "").lower() in ("1", "true", "yes")
    )
    if use_hardware:
        from lib.hardware_lock import acquire_hardware_lock, require_hardware_lock
        try:
            if lock_phase:
                acquire_hardware_lock(lock_phase)
                _hardware_lock_acquired = True
            else:
                require_hardware_lock()
        except RuntimeError as exc:
            pytest.exit(f"Cannot use hardware lock: {exc}", returncode=1)
        return

    if not lock_phase:
        return
    from lib.router_lock import RouterLock
    router_id = os.environ.get("TOLLGATE_ROUTER_ID", "default")
    branch = os.environ.get("TOLLGATE_BRANCH", "unknown")
    lock = RouterLock()
    try:
        lock.acquire(router_id=router_id, phase=lock_phase, branch=branch)
    except RuntimeError as exc:
        pytest.exit(f"Cannot acquire router lock: {exc}", returncode=1)
    _session_lock = lock


def pytest_sessionfinish(session, exitstatus):
    _save_test_metadata(session)

    if IS_MOCK_MODE:
        return

    global _session_lock, _hardware_lock_acquired
    if _hardware_lock_acquired:
        from lib.hardware_lock import release_hardware_lock
        release_hardware_lock()
        _hardware_lock_acquired = False
    if _session_lock is not None:
        _session_lock.release()
        _session_lock = None

    # Always collect router logs at session end (not just on failure).
    # Uses bundle="session-end" to avoid overwriting per-failure bundles.
    if "unit" in str(getattr(session.config, "testpaths", "")):
        return
    for item in session.items[:1]:
        if "router" in item.fixturenames:
            try:
                router = item.funcargs.get("router")
                if router and hasattr(router, "collect_logs"):
                    results_dir = getattr(item, "_results_dir", None)
                    if results_dir:
                        router.collect_logs(results_dir, adb=None, bundle="session-end")
                        log.info("Collected session-end logs to %s", results_dir)
            except Exception:
                pass
            break
