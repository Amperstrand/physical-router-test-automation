"""Rig smoke test — GS1900-8HP #1 PoE power-cycle -> SSH -> board sanity.

Milestone test for the labgrid rig bring-up (2026-09-25):
  1. ZyxelPoEDriver power-cycles the AP3915i on switch port lan5
     (off -> 8 s BCM59121 renegotiation delay -> on).
  2. Wait for SSH to come back (AP3915i needs ~60-90 s from PoE link-up).
  3. `ubus call system board` returns a sane OpenWrt payload.

Run (see configs/labgrid/rig-alpha.yaml header):
  TOLLGATE_SSH_HOST= ROUTER_IP= TOLLGATE_VIRTUAL_LAB= \
  ~/venvs/rig-labgrid/bin/python -m pytest \
    --lg-env=configs/labgrid/rig-alpha.yaml --no-deploy \
    tests/test_rig_smoke.py -v
"""

import json
import time

import pytest

# Registers ZyxelPoePort + ZyxelPoEDriver with labgrid's target factory so the
# environment YAML can resolve them. Must run before the env is loaded.
import tollgate_lab.drivers.zyxel_poe  # noqa: F401
from labgrid.driver import SSHDriver
from labgrid.protocol.powerprotocol import PowerProtocol

pytestmark = [pytest.mark.timeout(600), pytest.mark.smoke]

#: Seconds to wait for SSH after power-on (observed: cycle 8 s + boot ~120 s;
#: labgrid's SSHDriver needs a deactivate/reactivate round-trip after the
#: control master dies with the power cut).
SSH_WAIT_SECONDS = 300


def test_rig_smoke(target):
    power = target.get_driver(PowerProtocol)

    # Baseline: the port must be in a known PoE state before we touch it.
    pre_status = power.get_status()
    assert pre_status in ("Delivering power", "Searching"), pre_status

    # --- power cycle (off -> 8 s -> on) ---
    power.cycle()

    # --- wait for SSH to return ---
    # SSH is acquired only AFTER the cycle: the DUT is off/mid-boot right
    # now, and a pre-cycle control master would just burn its connect
    # timeout against a dead host. Powering the AP off kills SSHDriver's
    # control master and keepalive; the driver refuses further commands
    # ("Keepalive no longer running") until deactivated + reactivated.
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
        # Known failure mode: realtek-poe daemon crash (conwrt gotcha #3).
        # Surface both the SSH error and the current PoE state for triage.
        pytest.fail(
            f"SSH did not come back within {SSH_WAIT_SECONDS}s after power "
            f"cycle (last error: {last_error!r}; poe status after cycle: "
            f"{power.get_status()!r})"
        )

    # --- board sanity ---
    board = json.loads("\n".join(lines))
    assert board["release"]["distribution"] == "OpenWrt", board
    assert board["model"] == "Extreme Networks WS-AP3915i", board
    assert board["hostname"], board

    # --- rig-side postcondition: the port delivers power again ---
    assert power.get() is True
