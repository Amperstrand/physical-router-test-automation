"""TollGate rig deploy slice — power-cycle fresh, deploy build, module up.

First tollgate test slice through the labgrid rig (2026-09-25), following
tests/test_rig_smoke.py as the template. Topology: POST-MOVE (see
configs/labgrid/rig-alpha-stock.yaml) — router-alpha (WS-AP3915i,
192.168.105.51) now hangs off STOCK GS1900-8HP #2 p5, reachable by direct
SSH from this host; power control via ZyxelStockPoEDriver (dispatcher.cgi).

Flow:
  0. (host) cross-compile the TMBG binaries for the DUT: GOOS=linux
     GOARCH=arm GOARM=7 — AP3915i is ipq40xx = ARMv7 Cortex-A7
     (arm_cortex-a7_neon-vfpv4), NOT arm64. via packaging/local-build-ipk.sh
     (ARCH=arm_cortex-a7; TG_ALLOW_GO_MISMATCH=1 — lab host go1.25.8 vs
     pinned 1.26.8, accepted for lab deploys, output not reproducible).
  1. ZyxelStockPoEDriver power-cycles stock p5 (off -> 8 s BCM59121
     renegotiation -> on).
  2. Wait for SSH to come back (AP3915i: ~60-120 s from PoE link-up).
  3. Board sanity: model + target.
  4. Deploy: MINIMAL MODULE BRING-UP. The DUT has no internet/DNS and no
     opkg feeds, and nodogsplash + jq (the ipk's DEPENDS) are not
     installed — `opkg install` of the full package is impossible on this
     bench VLAN today (gap: documented in the mission report). Instead we
     stage the two Go binaries in /tmp (tmpfs — a power cycle returns the
     DUT to vanilla; overlay is too small: 18.7 MB binaries vs 20 MB free)
     and start tollgate-wrt directly with TOLLGATE_DEBUG=1.
  5. Verify module up: process alive, config manager wrote the default
     /etc/tollgate/config.json (schema v0.0.8), HTTP merchant server
     listening on :2121 (src/main.go), and /whoami answers over the wire
     from the host (exercises the ARP-based client-MAC resolution seam).
  6. Teardown: kill the process, remove /etc/tollgate + staged files —
     DUT left vanilla for the other bench lanes.

Payment E2E (test_ecash_payment class) is OUT OF SCOPE here: the DUT has
no route to any Cashu mint. The PRTA fakewallet pattern (CDK mint with
ln_backend="fakewallet", scripts/provision-local-lab.sh) would need a
host-side mint reachable from the DUT — feasible later over the flat L2
(192.168.105.2), but unproven; noted as the human-in-loop gap.

Run (see configs/labgrid/rig-alpha-stock.yaml header):
  export STOCK_SWITCH_PASSWORD="$(SOPS_AGE_KEY_FILE=~/.config/age/keys.txt \
    sops -d ~/conwrt-bench/secrets/secrets.json | jq -r .fleet.bench_root_password)"
  TOLLGATE_SSH_HOST= ROUTER_IP= TOLLGATE_VIRTUAL_LAB= \
  ~/venvs/rig-labgrid/bin/python -m pytest \
    --lg-env=configs/labgrid/rig-alpha-stock.yaml --no-deploy \
    tests/test_tollgate_rig.py -v

Set TOLLGATE_SKIP_BUILD=1 to reuse an existing bin/armv7/ build.
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

import pytest

# Registers StockZyxelPoePort + StockZyxelPoEDriver (tollgate-lab canonical,
# commits 6b82d1a/141868b) with labgrid's target factory so the environment
# YAML can resolve them. Must run before the env is loaded.
import tollgate_lab.drivers.stock_zyxel_poe  # noqa: F401
from labgrid.driver import SSHDriver
from labgrid.protocol.powerprotocol import PowerProtocol

pytestmark = [
    pytest.mark.timeout(900),
    pytest.mark.physical_hardware,
]

#: TMBG checkout (built from its own scripts — no TMBG tree mutation).
TMBG_ROOT = os.environ.get("TMBG_ROOT", os.path.expanduser(
    "~/src/tollgate-module-basic-go"))
BINARIES = ("tollgate-wrt", "tollgate")  # main service + operator CLI
REMOTE_DIR = "/tmp/tollgate-rig"
DEBUG_LOG = "/tmp/tollgate-debug.log"
DUT_IP = "192.168.105.51"
MERCHANT_PORT = 2121  # src/main.go: "Starting HTTP server on all interfaces"

#: Seconds to wait for SSH after power-on (smoke observed cycle 8 s +
#: boot ~120 s; SSHDriver needs deactivate/reactivate after the cut).
SSH_WAIT_SECONDS = 300

#: Seconds for the module to become ready: it writes the default config
#: immediately, then RunInitialProbe walks the default mint list with
#: ~10 s DNS timeouts each (8 mints, one-shot — observed ~80 s) before
#: the HTTP server binds :2121.
STARTUP_DEADLINE_SECONDS = 180


def _build_binaries():
    """Cross-compile TMBG for the DUT; returns local binary paths."""
    paths = [os.path.join(TMBG_ROOT, "bin", "armv7", n) for n in BINARIES]
    if os.environ.get("TOLLGATE_SKIP_BUILD") and all(
            os.path.isfile(p) for p in paths):
        return paths
    env = dict(os.environ, ARCH="arm_cortex-a7", TG_ALLOW_GO_MISMATCH="1")
    subprocess.run(
        ["bash", "packaging/local-build-ipk.sh"],
        cwd=TMBG_ROOT, env=env, check=True, timeout=600,
    )
    for p in paths:
        assert os.path.isfile(p), f"build did not produce {p}"
    return paths


def _wait_for_ssh(target):
    """Poll SSHDriver until `ubus call system board` answers.

    Mirrors test_rig_smoke: SSH is acquired only AFTER the cycle, and a
    failed attempt deactivates the driver (a control master that died
    with the power cut refuses further commands until reactivated).
    """
    deadline = time.monotonic() + SSH_WAIT_SECONDS
    lines = None
    last_error = None
    ssh = None
    while time.monotonic() < deadline:
        try:
            ssh = target.get_driver(SSHDriver)
            lines = ssh.run_check("ubus call system board")
            break
        except Exception as exc:  # noqa: BLE001 - retry until deadline
            last_error = exc
            if ssh is not None:
                try:
                    target.deactivate(ssh)
                except Exception:  # noqa: BLE001
                    pass
                ssh = None
            time.sleep(5)
    if lines is None:
        pytest.fail(
            f"SSH did not come back within {SSH_WAIT_SECONDS}s after power "
            f"cycle (last error: {last_error!r})"
        )
    return ssh, json.loads("\n".join(lines))


def _http_get_status(url, timeout=10):
    """GET a URL; returns (status, body). Any HTTP answer counts."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # 4xx from the server = server up
        return exc.code, exc.read().decode("utf-8", "replace")


def test_tollgate_rig_deploy(target):
    binaries = _build_binaries()  # host-side, before cutting power

    power = target.get_driver(PowerProtocol)

    # Baseline: the DUT port must be delivering before we touch it.
    # Canonical get_status() returns 'state/priority/mW'.
    pre_status = power.get_status()
    state, _prio, mw = pre_status.split("/")
    assert state == "Enable" and int(mw.rstrip("mW")) > 0, pre_status

    # --- power cycle fresh (off -> 8 s renegotiation -> on) ---
    power.cycle()

    # --- wait for SSH + board sanity ---
    ssh, board = _wait_for_ssh(target)
    assert board["model"] == "Extreme Networks WS-AP3915i", board
    assert board["release"]["target"].startswith("ipq40xx"), board
    assert board["release"]["distribution"] == "OpenWrt", board

    # --- deploy: minimal module bring-up (binaries in /tmp; no feeds) ---
    ssh.run_check(f"mkdir -p {REMOTE_DIR}")
    # The ipk payload normally stages /etc/tollgate/ (portal site, ecash);
    # the config manager writes the default config.json into it but does
    # not create the directory itself — fatal on a bare rootfs without it.
    ssh.run_check("mkdir -p /etc/tollgate")
    for local, name in zip(binaries, BINARIES):
        ssh.put(local, f"{REMOTE_DIR}/{name}")  # scp -O via explicit_scp_mode
        ssh.run_check(f"chmod 0755 {REMOTE_DIR}/{name}")

    # Vanilla guard + explicit clean slate for the config manager. The
    # bracket pattern keeps pgrep from matching this very command line.
    ssh.run('kill $(pgrep -f "[t]ollgate-wrt") 2>/dev/null || true')
    ssh.run_check("rm -f /etc/tollgate/config.json")

    # Start the service detached, same shape as the init script's procd
    # instance (command tollgate-wrt, env TOLLGATE_DEBUG=1, log to
    # /tmp/tollgate-debug.log) — without procd, whose `depends
    # nodogsplash` coupling is unsatisfiable on this feed-less DUT.
    # busybox has no nohup; ssh command execution has no controlling
    # tty, so a plain background redirect survives the session.
    ssh.run_check(
        f"sh -c 'TOLLGATE_DEBUG=1 {REMOTE_DIR}/tollgate-wrt "
        f">{DEBUG_LOG} 2>&1 &'"
    )

    try:
        # --- verify module up ---
        # Poll for the HTTP server instead of a fixed sleep: startup is
        # dominated by the one-shot mint probes (offline DUT: ~10 s DNS
        # timeout per mint, observed ~80 s total).
        deadline = time.monotonic() + STARTUP_DEADLINE_SECONDS
        while time.monotonic() < deadline:
            out, _err, _rc = ssh.run(
                f"netstat -ltn | grep ':{MERCHANT_PORT}' || true")
            if "\n".join(out).strip():
                break
            time.sleep(5)
        else:
            log_tail = "\n".join(
                ssh.run(f"tail -15 {DEBUG_LOG} || true")[0])
            pytest.fail(
                f":{MERCHANT_PORT} not listening within "
                f"{STARTUP_DEADLINE_SECONDS}s; last log lines:\n{log_tail}"
            )
        print(f"merchant :{MERCHANT_PORT} up after "
              f"~{STARTUP_DEADLINE_SECONDS - (deadline - time.monotonic()):.0f}s")

        # 1. process alive (no procd respawn here: alive after startup
        #    means it did not crash-loop on the missing daemon deps).
        out, err, rc = ssh.run('pgrep -f "[t]ollgate-wrt" || true')
        text = "\n".join(out)  # labgrid run() returns line lists
        log_tail = "\n".join(
            ssh.run(f"tail -15 {DEBUG_LOG} || true")[0])
        assert rc == 0 and text.strip(), (
            f"tollgate-wrt process not running; last log lines:\n{log_tail}"
        )
        pid = text.split()[0]

        # 2. config manager wrote the default config (schema v0.0.8).
        out, err, rc = ssh.run(
            "grep -o '\"config_version\": \"[^\"]*\"' /etc/tollgate/config.json"
            " || cat /etc/tollgate/config.json")
        text = "\n".join(out)
        assert rc == 0 and "v0.0.8" in text, (
            f"default config not written as v0.0.8: {out} {err}"
        )

        # 3. merchant HTTP server listening on :2121 on all interfaces.
        out, err, rc = ssh.run(f"netstat -ltn | grep ':{MERCHANT_PORT}'"
                               " || true")
        text = "\n".join(out)
        assert rc == 0 and text.strip(), (
            f"nothing listening on :{MERCHANT_PORT}: {out} {err}"
        )

        # 4. startup log line from src/main.go.
        out, err, rc = ssh.run(
            f"grep -c 'Starting HTTP server on all interfaces' {DEBUG_LOG}"
            " || true")
        text = "\n".join(out)
        assert rc == 0 and text.strip() not in ("", "0"), (
            f"HTTP-server startup line missing from {DEBUG_LOG}"
        )

        # 5. /whoami answers over the wire from the host (the request
        #    sources from 192.168.105.2, exercising the DHCP-lease/ARP
        #    client-MAC resolution path, not just loopback).
        status, body = _http_get_status(
            f"http://{DUT_IP}:{MERCHANT_PORT}/whoami")
        assert status is not None and status < 500, (
            f"/whoami unreachable from host (status={status})"
        )
        assert body, "/whoami returned an empty body"
        print(f"/whoami -> {status}: {body[:200]}")
        print(f"module up: pid={pid}, config v0.0.8, :{MERCHANT_PORT} listening")

        # --- rig-side postcondition: the port delivers power again ---
        assert power.get() is True
    finally:
        # --- teardown: leave the DUT vanilla for the other bench lanes ---
        ssh.run('kill $(pgrep -f "[t]ollgate-wrt") 2>/dev/null || true')
        ssh.run(f"rm -rf /etc/tollgate {REMOTE_DIR} {DEBUG_LOG} /tmp/basic.log"
                " /tmp/whoami.out || true")
        # No sibling-config restore: the canonical driver's writes are
        # full-field read-modify-write by design (drift-free, commit 141868b).
