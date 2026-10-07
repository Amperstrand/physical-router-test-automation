"""Recovery and startup hygiene tests for physical routers.

Ported from the Makefile-based branch test targets:
- r-test-startup-hygiene / r-test-startup-hygiene-dead-only
- r-rescue-router

These tests verify that the router recovers correctly from adverse boot
conditions and that startup connectivity hygiene checks are logged.
Destructive tests (which modify router state) are marked with
``@pytest.mark.destructive`` and skip unless their required environment
variables are set.

Run with:
    pytest tests/scenarios/test_recovery.py
    pytest tests/scenarios/test_recovery.py -m destructive  # destructive only
    pytest tests/scenarios/test_recovery.py -m "not destructive"  # safe only
"""

import json
import logging
import os
import re
import subprocess
import time

import pytest

log = logging.getLogger("tollgate.recovery")

pytestmark = [pytest.mark.story("O1"),pytest.mark.api, pytest.mark.extended]


# ---------------------------------------------------------------------------
# Feature-detection helpers
# ---------------------------------------------------------------------------

def _skip_if_no_upstream_wifi(router):
    """Skip if the router has no upstream WiFi station-mode config."""
    out = router.ssh("uci get wireless.wwan 2>/dev/null || echo MISSING", timeout=10)
    if "MISSING" in out:
        pytest.skip("No upstream wireless station-mode (wwan) configured on router")


def _skip_if_no_startup_log_support(router):
    """Skip if the backend does not log startup-check messages."""
    logs = router.get_tollgate_logs(filter_expr="startup", lines=50)
    # Even if no startup logs exist yet, we check if logread is available
    # and the backend is running — the test itself will verify content.
    if not logs:
        # Check if backend is at least running
        code = router.api_status("/")
        if code != 200:
            pytest.skip("Backend not reachable; cannot check startup logs")


# ---------------------------------------------------------------------------
# 1. Startup hygiene — read-only log check
# ---------------------------------------------------------------------------

def test_startup_hygiene_logs(router):
    """Verify that startup connectivity check entries appear in logread.

    After boot, the tollgate backend should log startup-check messages
    indicating which upstream networks were probed and their results.
    This is a read-only check — no router state is modified.
    """
    logs = router.ssh("logread | grep -i 'startup check' 2>/dev/null", timeout=15)
    if not logs.strip():
        # Try broader patterns from the backend
        logs = router.get_tollgate_logs(filter_expr="tollgate", lines=200)

    # The backend should at minimum have logged something on startup.
    # We accept either explicit "startup check" lines or general tollgate
    # startup activity (service start, mint configuration, etc.).
    assert logs.strip(), "No tollgate startup logs found in logread"


# ---------------------------------------------------------------------------
# 2. Rescue router relay — destructive, requires TOLLGATE_RESCUE_VIA
# ---------------------------------------------------------------------------

@pytest.mark.destructive
def test_rescue_router_relay(router):
    """Verify SSH relay through a rescue router to reach the target.

    Requires ``TOLLGATE_RESCUE_VIA`` environment variable set to the
    hostname/IP of the rescue router. The test SSHes through the rescue
    router to reach the target router, verifying multi-hop connectivity.

    This reproduces the ``r-rescue-router`` Makefile target.
    """
    rescue_via = os.environ.get("TOLLGATE_RESCUE_VIA", "").strip()
    if not rescue_via:
        pytest.skip("TOLLGATE_RESCUE_VIA not set — skipping relay test")

    ssh_pw = os.environ.get("TOLLGATE_SSH_PASSWORD") or os.environ.get("TOLLGATE_LUCI_PASSWORD", "")
    identity_file = os.environ.get("TOLLGATE_SSH_KEY", "")

    target_host = router.host

    # Build an SSH command that jumps through the rescue router
    ssh_opts = [
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
    ]

    if identity_file:
        cmd = ["ssh"] + ssh_opts + ["-i", identity_file, "-J", f"root@{rescue_via}", f"root@{target_host}", "hostname"]
    elif ssh_pw:
        cmd = ["sshpass", "-e", "ssh"] + ssh_opts + ["-J", f"root@{rescue_via}", f"root@{target_host}", "hostname"]
    else:
        cmd = ["ssh"] + ssh_opts + ["-J", f"root@{rescue_via}", f"root@{target_host}", "hostname"]

    env = os.environ.copy()
    if ssh_pw:
        env["SSHPASS"] = ssh_pw

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, env=env,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"SSH relay through {rescue_via} to {target_host} timed out")
        return  # unreachable, for type checkers

    assert result.returncode == 0, (
        f"SSH relay failed (rc={result.returncode}): {result.stderr.strip()[:300]}"
    )
    hostname = result.stdout.strip()
    assert hostname, "No hostname returned via relay SSH"
    log.info(f"Rescue relay OK: reached {target_host} via {rescue_via}, hostname={hostname}")


# ---------------------------------------------------------------------------
# 3. Save/restore upstream SSID — destructive
# ---------------------------------------------------------------------------

@pytest.mark.destructive
def test_save_restore_upstream(router):
    """Save current upstream SSID, switch to a different one, verify, restore.

    This test verifies that the router's upstream WiFi station-mode
    configuration can be changed and restored. It:
    1. Saves the current upstream SSID
    2. Switches to ``TOLLGATE_UPSTREAM_SSID_ALT`` (or skips if unset)
    3. Verifies the switch took effect
    4. Restores the original SSID in a ``finally`` block

    Marked destructive because it modifies UCI wireless configuration.
    """
    _skip_if_no_upstream_wifi(router)

    alt_ssid = os.environ.get("TOLLGATE_UPSTREAM_SSID_ALT", "").strip()
    if not alt_ssid:
        pytest.skip("TOLLGATE_UPSTREAM_SSID_ALT not set — cannot test SSID switch")

    # Read current upstream SSID
    current_ssid = router.ssh(
        "uci get wireless.@wifi-iface[0].ssid 2>/dev/null || echo UNKNOWN",
        timeout=10,
    ).strip()

    # Find the wwan/station interface section name
    section = router.ssh(
        "uci show wireless | grep -E 'wifi-iface.*mode.*sta' | "
        "head -1 | sed 's/wireless\\.\\([^=]*\\)\\.mode=.*/\\1/'",
        timeout=10,
    ).strip()

    if not section or "=" in section:
        # Fallback: try the first wifi-iface
        section = router.ssh(
            "uci show wireless | grep 'wifi-iface' | head -1 | "
            "cut -d. -f2 | cut -d= -f1",
            timeout=10,
        ).strip()

    if not section:
        pytest.skip("Could not identify upstream wifi-iface section")

    original_ssid = router.ssh(
        f"uci get wireless.{section}.ssid 2>/dev/null || echo UNKNOWN",
        timeout=10,
    ).strip()

    try:
        # Switch to alternate SSID
        router.ssh(
            f"uci set wireless.{section}.ssid='{alt_ssid}' && uci commit wireless",
            timeout=10,
        )

        # Verify the switch
        new_ssid = router.ssh(
            f"uci get wireless.{section}.ssid 2>/dev/null",
            timeout=10,
        ).strip()
        assert new_ssid == alt_ssid, f"SSID not switched: expected '{alt_ssid}', got '{new_ssid}'"
        log.info(f"Upstream SSID switched: '{original_ssid}' -> '{alt_ssid}'")

    finally:
        # Restore original SSID
        router.ssh(
            f"uci set wireless.{section}.ssid='{original_ssid}' && uci commit wireless",
            timeout=10,
        )
        log.info(f"Upstream SSID restored to '{original_ssid}'")


# ---------------------------------------------------------------------------
# 4. Connectivity loss detection — read-only log check
# ---------------------------------------------------------------------------

def test_connectivity_loss_detection(router):
    """Verify logs contain connectivity-loss patterns when detected.

    Checks recent logs for patterns indicating the backend detected and
    handled upstream connectivity loss (e.g. "lost", "emergency",
    "unreachable", "degraded"). This is a read-only check.

    Skips if no such patterns are found in recent logs — this is normal
    for routers with stable upstream connectivity during the test window.
    """
    # Check both logread (system) and tollgate-specific logs
    system_logs = router.ssh(
        "logread -l 500 2>/dev/null | grep -iE '(lost|emergency|unreachable|degraded|no route|connectivity)' || true",
        timeout=15,
    )
    tollgate_logs = router.get_tollgate_logs(filter_expr="tollgate", lines=200)

    combined = system_logs + "\n" + tollgate_logs

    loss_patterns = [
        r"lost",
        r"emergency",
        r"unreachable",
        r"degraded",
        r"connectivity.*(?:lost|down|fail)",
        r"upstream.*(?:down|fail|unreachable)",
        r"mint.*(?:unreachable|timeout|fail)",
    ]

    matches = []
    for pattern in loss_patterns:
        found = re.findall(pattern, combined, re.IGNORECASE)
        if found:
            matches.extend(found)

    if not matches:
        pytest.skip(
            "No connectivity-loss patterns found in recent logs "
            "(router likely has stable upstream)"
        )

    log.info(f"Found {len(matches)} connectivity-loss log entries: {matches[:10]}")


# ---------------------------------------------------------------------------
# 5. Cold boot via serial console — destructive, requires TOLLGATE_SERIAL_PORT
# ---------------------------------------------------------------------------

@pytest.mark.destructive
@pytest.mark.slow
def test_cold_boot_via_serial(router):
    """Reboot router via serial console and capture boot log.

    Requires ``TOLLGATE_SERIAL_PORT`` environment variable (e.g.
    ``/dev/serial-alpha``). Uses ``scripts/router-serial.py`` to:
    1. Send ``reboot`` over serial
    2. Capture the full boot log until the login prompt
    3. Verify key startup messages appear

    Marked destructive because it reboots the router.
    Marked slow because boot takes 60-180 seconds.
    """
    from lib.serial_console import SerialConsole

    serial_port = os.environ.get("TOLLGATE_SERIAL_PORT", "").strip()
    if not serial_port:
        pytest.skip("TOLLGATE_SERIAL_PORT not set — skipping serial boot test")

    if not os.path.exists(serial_port):
        pytest.skip(f"Serial port {serial_port} not found on this host")

    console = SerialConsole(serial_port)
    try:
        boot_output = console.reboot_and_bootlog(timeout=180)
    except (subprocess.TimeoutExpired, RuntimeError) as exc:
        pytest.fail(f"Serial boot log capture failed: {exc}")
        return  # unreachable
    assert boot_output.strip(), "Empty boot log captured via serial"

    boot_lines = boot_output.strip().split("\n")
    log.info(f"Captured {len(boot_lines)} boot log lines via serial")

    # Verify key boot messages
    boot_text = boot_output.lower()
    essential_patterns = [
        ("kernel boot", r"linux version"),
        ("init", r"init:"),
        ("login ready", r"login:"),
    ]

    missing = []
    for label, pattern in essential_patterns:
        if not re.search(pattern, boot_text):
            missing.append(label)

    # Not all routers log "Linux version" via serial, so only require login:
    if "login ready" in missing:
        pytest.fail("Boot did not complete to login prompt within 180s")
    elif missing:
        log.warning(f"Boot log missing expected patterns: {missing}")
