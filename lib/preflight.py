"""Environment autodetection for PRTA — probe what the lab can actually run.

Checks, with everything injectable for offline unit tests (mock SSH /
mock subprocess):

  routers   every entry in config/routers.json: SSH reachable, brand
            (/etc/tollgate/brand), device code (uci tollgate.device.code),
            SSIDs (runtime + classification), OpenWrt firmware, installed
            tollgate-wrt version, tollgate-wrt + nodogsplash running
  adb       ``adb devices -l`` and per-device state
  docker    daemon up + lab images (mint-zoo fleet: cashubtc/nutshell,
            cashubtc/mintd) present — the docker-backed cloud/mint tooling
  qemu      local virtual-lab liveness: ~/tollgate-virtual-lab/run/*.pid
            alive + tg-poc-br bridge up
  lanes     which pytest lanes (api/phone/web/scenarios/virtual-lab) are
            runnable and what is missing when they are not

Output: a human checklist (``format_checklist``) and a JSON snapshot
(``run_preflight`` writes results/preflight-latest.json; results/ is
gitignored).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, MutableMapping

from lib.ssid import prefixes_from_inventory, probe_router_identity

SNAPSHOT_SCHEMA_VERSION = 1
DEFAULT_SNAPSHOT_PATH = os.path.join("results", "preflight-latest.json")
SNAPSHOT_MAX_AGE_SECONDS = 300

VIRTUAL_LAB_RUN_DIR = os.path.expanduser("~/tollgate-virtual-lab/run")
VIRTUAL_LAB_BRIDGE = "tg-poc-br"

LAB_IMAGE_RE = re.compile(r"^cashubtc/(nutshell|mintd):")

RunSsh = Callable[[str], str]
RunCmd = Callable[[list[str]], "tuple[int, str]"]


class SshError(RuntimeError):
    pass


def default_run_cmd(argv: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 127, ""
    return r.returncode, r.stdout


def make_ssh_runner(
    entry: dict,
    timeout: int = 10,
    env: dict[str, str] | None = None,
) -> RunSsh:
    """Build a run_ssh(cmd) -> stdout for an inventory entry.

    Mirrors Router's auth chain: sshpass with TOLLGATE_SSH_PASSWORD /
    TOLLGATE_LUCI_PASSWORD, else TOLLGATE_SSH_KEY identity, plus optional
    jumpHost/sshPort.
    """
    env = env if env is not None else dict(os.environ)
    host = entry.get("sshHost", "")
    if not host:
        raise SshError("inventory entry has no sshHost")

    base = [
        "ssh",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
    ]
    identity = env.get("TOLLGATE_SSH_KEY", "")
    password = env.get("TOLLGATE_SSH_PASSWORD") or env.get("TOLLGATE_LUCI_PASSWORD", "")
    if password and not identity:
        base = ["sshpass", "-p", password] + base
    if identity:
        base += ["-i", identity]
    if entry.get("jumpHost"):
        base += ["-J", entry["jumpHost"]]
    if entry.get("sshPort"):
        base += ["-p", str(entry["sshPort"])]
    user = entry.get("sshUser", "root")
    base.append(f"{user}@{host}")

    def run_ssh(cmd: str) -> str:
        try:
            r = subprocess.run(
                base + [cmd],
                capture_output=True, text=True, timeout=timeout + 20,
            )
        except subprocess.TimeoutExpired as exc:
            raise SshError(f"timeout after {timeout + 20}s") from exc
        if r.returncode != 0:
            raise SshError(f"exit {r.returncode}: {(r.stderr or '').strip()[:200]}")
        return r.stdout

    return run_ssh


@dataclass
class RouterProbe:
    router_id: str = ""
    host: str = ""
    ssh_ok: bool = False
    ssh_error: str = ""
    brand: str = ""
    device_code: str = ""
    ssids: list[str] = field(default_factory=list)
    captive_ssid: str = ""
    private_ssid: str = ""
    firmware: str = ""
    tollgate_version: str = ""
    tollgate_running: bool = False
    nodogsplash_running: bool = False


def probe_router(router_id: str, entry: dict, run_ssh: RunSsh | None = None) -> RouterProbe:
    """Probe one inventory router. Unreachable → ssh_ok False with the error."""
    probe = RouterProbe(router_id=router_id, host=entry.get("sshHost", ""))
    if run_ssh is None:
        try:
            run_ssh = make_ssh_runner(entry)
        except SshError as exc:
            probe.ssh_error = str(exc)
            return probe

    try:
        run_ssh("true")
    except Exception as exc:
        probe.ssh_error = str(exc)[:300]
        return probe
    probe.ssh_ok = True

    def safe(cmd: str) -> str:
        try:
            return run_ssh(cmd)
        except Exception:
            return ""

    probe.brand = safe("cat /etc/tollgate/brand 2>/dev/null").strip()

    captive_prefixes, private_prefixes = prefixes_from_inventory(entry)
    ident = probe_router_identity(run_ssh, captive_prefixes, private_prefixes)
    probe.device_code = ident.device_code
    probe.ssids = ident.ssids
    probe.captive_ssid = ident.captive_ssid
    probe.private_ssid = ident.private_ssid

    probe.firmware = safe(
        ". /etc/openwrt_release 2>/dev/null && echo $DISTRIB_DESCRIPTION"
    ).strip()
    probe.tollgate_version = safe(
        "opkg list-installed tollgate-wrt 2>/dev/null | awk '{print $3}'"
    ).strip()
    probe.tollgate_running = bool(safe("pidof tollgate-wrt").strip())
    probe.nodogsplash_running = bool(safe("pidof nodogsplash").strip())
    return probe


def check_adb(run_cmd: RunCmd = default_run_cmd) -> dict:
    rc, out = run_cmd(["adb", "devices", "-l"])
    if rc != 0:
        return {"available": False, "devices": []}
    devices = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        devices.append({
            "serial": fields[0],
            "state": fields[1],
            "description": " ".join(fields[2:]),
        })
    return {"available": True, "devices": devices}


def check_docker(run_cmd: RunCmd = default_run_cmd) -> dict:
    rc, out = run_cmd(["docker", "info", "--format", "{{.ServerVersion}}"])
    daemon = rc == 0
    result = {"cli": rc != 127, "daemon": daemon, "server_version": "", "lab_images": []}
    if not daemon:
        return result
    result["server_version"] = out.strip()
    _, img_out = run_cmd(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"])
    result["lab_images"] = sorted(
        line.strip() for line in img_out.splitlines() if LAB_IMAGE_RE.match(line.strip())
    )
    return result


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def check_virtual_lab(
    run_dir: str = VIRTUAL_LAB_RUN_DIR,
    run_cmd: RunCmd = default_run_cmd,
) -> dict:
    pid_files: dict[str, int | None] = {}
    if os.path.isdir(run_dir):
        for name in sorted(os.listdir(run_dir)):
            if not name.endswith(".pid"):
                continue
            try:
                with open(os.path.join(run_dir, name)) as f:
                    pid_files[name] = int(f.read().strip())
            except (OSError, ValueError):
                pid_files[name] = None
    alive = {name: pid for name, pid in pid_files.items() if pid and _pid_alive(pid)}
    rc, _ = run_cmd(["ip", "link", "show", VIRTUAL_LAB_BRIDGE])
    bridge_up = rc == 0
    return {
        "run_dir": run_dir,
        "pid_files": pid_files,
        "alive_pids": alive,
        "bridge": VIRTUAL_LAB_BRIDGE if bridge_up else "",
        "alive": bool(alive) or bridge_up,
    }


def compute_lanes(snapshot: dict) -> dict:
    routers = snapshot.get("routers", {})
    ssh_ok = [rid for rid, r in routers.items() if r.get("ssh_ok")]
    backend_ok = [
        rid for rid, r in routers.items()
        if r.get("ssh_ok") and r.get("tollgate_running")
    ]
    adb_ready = any(
        d.get("state") == "device"
        for d in snapshot.get("adb", {}).get("devices", [])
    )
    docker = snapshot.get("docker", {})
    vl = snapshot.get("virtual_lab", {})

    def lane(runnable: bool, needs: str, detail: str = "") -> dict:
        return {"runnable": runnable, "needs": needs, "detail": detail}

    return {
        "api": lane(
            bool(backend_ok),
            "router with SSH + tollgate-wrt running",
            f"routers ready: {', '.join(backend_ok) or 'none'}",
        ),
        "phone": lane(
            bool(backend_ok) and adb_ready,
            "api prerequisites + ADB device in 'device' state",
            f"routers: {', '.join(backend_ok) or 'none'}; adb ready: {adb_ready}",
        ),
        "web": lane(
            bool(ssh_ok),
            "physical router SSH (LuCI/Playwright)",
            f"routers reachable: {', '.join(ssh_ok) or 'none'}",
        ),
        "scenarios": lane(
            bool(backend_ok),
            "physical router with TollGate running",
            f"routers ready: {', '.join(backend_ok) or 'none'}",
        ),
        "virtual-lab": lane(
            bool(vl.get("alive")),
            "local QEMU VMs alive (~/tollgate-virtual-lab/run) or bridge up",
            f"alive pids: {len(vl.get('alive_pids', {}))}; bridge: {vl.get('bridge') or 'down'}",
        ),
        "cloud-lab": lane(
            bool(docker.get("daemon")),
            "docker daemon (mint-zoo / docker-backed lab tooling)",
            f"lab images: {len(docker.get('lab_images', []))}",
        ),
    }


def _yes_no(flag: bool) -> str:
    return "YES " if flag else "NO  "


def format_checklist(snapshot: dict) -> str:
    lines = [f"=== PRTA Preflight — {snapshot.get('generated_at', '?')} ==="]

    inv = snapshot.get("inventory_path", "")
    lines.append(f"Routers ({inv or 'no inventory'})")
    routers = snapshot.get("routers", {})
    if not routers:
        lines.append("  (none configured)")
    for rid, r in routers.items():
        if not r.get("ssh_ok"):
            lines.append(f"  [FAIL] {rid} @ {r.get('host', '?')} — {r.get('ssh_error', 'unreachable')}")
            continue
        bits = [
            f"[OK]   {rid} @ {r.get('host', '?')}",
            f"brand={r.get('brand') or '-'}",
            f"code={r.get('device_code') or '-'}",
            f"captive={r.get('captive_ssid') or '-'}",
            f"private={r.get('private_ssid') or '-'}",
            f"fw={r.get('firmware') or '-'}",
            f"tollgate={r.get('tollgate_version') or '-'}"
            + ("/running" if r.get("tollgate_running") else "/DOWN"),
            "nds=" + ("running" if r.get("nodogsplash_running") else "down"),
        ]
        lines.append("  " + "  ".join(bits))
        ssids = r.get("ssids") or []
        if ssids:
            lines.append(f"         SSIDs: {', '.join(ssids)}")

    adb = snapshot.get("adb", {})
    lines.append("ADB")
    if not adb.get("available"):
        lines.append("  [FAIL] adb CLI not available")
    elif not adb.get("devices"):
        lines.append("  [WARN] no devices attached")
    for d in adb.get("devices", []):
        marker = "[OK]  " if d.get("state") == "device" else "[WARN]"
        lines.append(f"  {marker} {d['serial']} {d['state']} {d.get('description', '')}".rstrip())

    docker = snapshot.get("docker", {})
    lines.append("Docker / cloud-lab")
    if not docker.get("cli"):
        lines.append("  [WARN] docker CLI not found")
    elif not docker.get("daemon"):
        lines.append("  [WARN] docker daemon not running")
    else:
        imgs = docker.get("lab_images") or []
        lines.append(f"  [OK]  daemon {docker.get('server_version', '?')}; "
                     f"lab images: {', '.join(imgs) if imgs else 'none pulled'}")

    vl = snapshot.get("virtual_lab", {})
    lines.append("Virtual lab (local QEMU)")
    if vl.get("alive"):
        alive = vl.get("alive_pids", {})
        lines.append(f"  [OK]  pids alive: {', '.join(f'{k}({v})' for k, v in alive.items()) or 'none'}; "
                     f"bridge {vl.get('bridge') or 'down'}")
    else:
        lines.append(f"  [WARN] not running (no live pids in {vl.get('run_dir', '?')})")

    lines.append("Runnable lanes")
    for lane_name, info in snapshot.get("lanes", {}).items():
        lines.append(f"  {_yes_no(bool(info.get('runnable')))} {lane_name:<14} "
                     f"— {info.get('detail') or info.get('needs', '')}")
    lines.append("Snapshot: " + str(snapshot.get("snapshot_path") or DEFAULT_SNAPSHOT_PATH))
    return "\n".join(lines)


def run_preflight(
    inventory_path: str | None = None,
    out_path: str | None = None,
    ssh_runner_factory: Callable[[dict], RunSsh] | None = None,
    run_cmd: RunCmd = default_run_cmd,
    virtual_lab_run_dir: str = VIRTUAL_LAB_RUN_DIR,
    repo_root: str | None = None,
) -> dict:
    """Probe everything, write the JSON snapshot, return it."""
    repo_root = repo_root or os.getcwd()
    if inventory_path is None:
        inventory_path = os.environ.get(
            "TOLLGATE_ROUTER_INVENTORY",
            os.path.join(repo_root, "config", "routers.json"),
        )

    routers: dict[str, dict] = {}
    if os.path.isfile(inventory_path):
        with open(inventory_path) as f:
            inventory = json.load(f)
        factory = ssh_runner_factory or (lambda entry: make_ssh_runner(entry))
        for router_id, entry in inventory.get("routers", {}).items():
            try:
                runner = factory(entry)
            except SshError as exc:
                probe = RouterProbe(router_id=router_id, host=entry.get("sshHost", ""),
                                    ssh_error=str(exc))
            else:
                probe = probe_router(router_id, entry, run_ssh=runner)
            routers[router_id] = asdict(probe)
    else:
        inventory = {}

    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "generated_at_epoch": time.time(),
        "inventory_path": inventory_path if (routers or os.path.isfile(inventory_path)) else "",
        "default_router": inventory.get("default", "") if isinstance(inventory, dict) else "",
        "routers": routers,
        "adb": check_adb(run_cmd),
        "docker": check_docker(run_cmd),
        "virtual_lab": check_virtual_lab(run_dir=virtual_lab_run_dir, run_cmd=run_cmd),
    }
    snapshot["lanes"] = compute_lanes(snapshot)

    out_path = out_path or os.path.join(repo_root, DEFAULT_SNAPSHOT_PATH)
    snapshot["snapshot_path"] = out_path
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(snapshot, f, indent=2)
    return snapshot


def load_snapshot(path: str | None = None, max_age_seconds: int = SNAPSHOT_MAX_AGE_SECONDS) -> dict | None:
    """Load a fresh snapshot; None when missing, stale, or corrupt."""
    path = path or DEFAULT_SNAPSHOT_PATH
    try:
        with open(path) as f:
            snapshot = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    generated = snapshot.get("generated_at_epoch", 0)
    if not generated or time.time() - generated > max_age_seconds:
        return None
    return snapshot


def apply_env_defaults(
    snapshot: dict,
    env: MutableMapping[str, str] | None = None,
    logger=None,
) -> tuple[str | None, str | None]:
    """Default router host / phone serial to the first AVAILABLE device.

    Only fills gaps: an explicitly configured host that is simply absent
    from the inventory (e.g. the virtual-lab QEMU host) or a phone serial
    that is attached in any state is never overridden. Returns
    ``(chosen_router_id, chosen_phone_serial)`` — (None, None) when nothing
    was chosen.
    """
    env = env if env is not None else dict(os.environ)
    chosen_router: str | None = None
    chosen_phone: str | None = None

    probes = snapshot.get("routers", {})
    reachable = [(rid, p) for rid, p in probes.items() if p.get("ssh_ok")]
    inventory_hosts = {p.get("host") for p in probes.values() if p.get("host")}
    reachable_hosts = {p.get("host") for _, p in reachable}
    configured_host = env.get("TOLLGATE_SSH_HOST") or env.get("ROUTER_IP")
    if reachable:
        # Only judge hosts the inventory actually knows: a configured host
        # absent from the inventory (e.g. the virtual-lab QEMU host) keeps
        # its explicit configuration.
        configured_unreachable = bool(
            configured_host
            and configured_host in inventory_hosts
            and configured_host not in reachable_hosts
        )
        if not configured_host or configured_unreachable:
            router_id, probe = next(
                ((rid, p) for rid, p in reachable if rid == snapshot.get("default_router")),
                reachable[0],
            )
            chosen_router = router_id
            if logger:
                if configured_unreachable:
                    logger.warning(
                        "Configured router host %s unreachable — falling back to '%s' @ %s",
                        configured_host, router_id, probe["host"],
                    )
                else:
                    logger.info("No router host configured — using '%s' @ %s",
                                router_id, probe["host"])
            env["TOLLGATE_SSH_HOST"] = probe["host"]
            env.setdefault("TOLLGATE_ROUTER_ID", router_id)

    devices = snapshot.get("adb", {}).get("devices", [])
    ready: list[str] = [d["serial"] for d in devices if d.get("state") == "device"]
    attached = {d["serial"] for d in devices}
    configured_serial = env.get("PHONE_SERIAL", "")
    if ready and (not configured_serial or configured_serial not in attached):
        chosen_phone = ready[0]
        if logger:
            if configured_serial:
                logger.warning("PHONE_SERIAL %s not attached — using %s",
                               configured_serial, chosen_phone)
            else:
                logger.info("No PHONE_SERIAL configured — using first available device %s",
                            chosen_phone)
        env["PHONE_SERIAL"] = chosen_phone

    return chosen_router, chosen_phone
