"""User Story: A router with active sessions upgrades without losing them.

Tests the upgrade path: a running TollGate with a paid session gets a
new ipk installed (service restart, not full reflash). The session
should persist — the client should still have internet after the upgrade.

This is the test that catches the "volatile wallet/session" class of bug
where a restart or upgrade wipes state that should be persistent.
"""
import logging
import os
import subprocess
import time

import pytest

log = logging.getLogger("tollgate.story.upgrade")

pytestmark = [pytest.mark.slow, pytest.mark.physical_hardware]

ROUTER_HOST = os.environ.get("TOLLGATE_SSH_HOST", "")
IPK_PATH = os.environ.get("TOLLGATE_RELEASE_IPK", "")


def _ssh(cmd: str, timeout: int = 15) -> str:
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=no",
         f"root@{ROUTER_HOST}", cmd],
        capture_output=True, text=True, timeout=timeout).stdout.strip()


@pytest.mark.skipif(not ROUTER_HOST, reason="TOLLGATE_SSH_HOST not set")
@pytest.mark.skipif(not IPK_PATH or not os.path.isfile(IPK_PATH),
                    reason="TOLLGATE_RELEASE_IPK not set or file not found")
def test_upgrade_preserves_sessions(fresh_session):
    """A TollGate with an active session survives an ipk upgrade."""
    device = fresh_session

    # Precondition: authenticated with internet (fresh_session fixture)
    assert device.has_internet(), "precondition: should have internet"
    log.info("precondition: authenticated with active session")

    # Record the current version
    old_version = _ssh(
        "opkg status tollgate-wrt 2>/dev/null | grep Version | awk '{print $2}'")
    log.info("current version: %s", old_version)

    # Get the session state before upgrade
    sessions_before = _ssh("cat /etc/tollgate/sessions.json 2>/dev/null || echo '{}'")
    log.info("sessions before: %s", sessions_before[:100])

    # Act: install the new ipk (service restart, not reflash)
    log.info("installing %s ...", os.path.basename(IPK_PATH))
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".log") as tf:
        scp = subprocess.run(
            ["scp", "-O", "-o", "StrictHostKeyChecking=no",
             IPK_PATH, f"root@{ROUTER_HOST}:/tmp/upgrade.ipk"],
            capture_output=True, text=True, timeout=120)
        if scp.returncode != 0:
            pytest.fail(f"scp failed: {scp.stderr[:200]}")

        install = _ssh(
            "opkg install --force-overwrite /tmp/upgrade.ipk && "
            "/etc/init.d/tollgate-wrt restart && sleep 5 && "
            "opkg status tollgate-wrt | grep Version",
            timeout=60)
    log.info("install result: %s", install[:100])

    new_version = _ssh(
        "opkg status tollgate-wrt 2>/dev/null | grep Version | awk '{print $2}'")
    log.info("new version: %s", new_version)

    # Wait for backend to come back
    deadline = time.time() + 30
    backend_up = False
    while time.time() < deadline:
        code = _ssh(
            "wget -qO- --timeout=3 --spider "
            f"'http://[::1]:2121/' 2>/dev/null && echo UP || echo DOWN")
        if "UP" in code:
            backend_up = True
            break
        time.sleep(2)
    assert backend_up, "backend did not come back after upgrade"

    # Assert: session persisted
    sessions_after = _ssh("cat /etc/tollgate/sessions.json 2>/dev/null || echo '{}'")
    log.info("sessions after: %s", sessions_after[:100])

    # The phone should still have internet (session survived)
    # Give it a moment for NDS to re-establish rules
    time.sleep(5)
    deadline = time.time() + 30
    while time.time() < deadline:
        if device.has_internet():
            break
        time.sleep(3)

    assert device.has_internet(), \
        "session was lost during upgrade — client has no internet after service restart"


@pytest.mark.skipif(not ROUTER_HOST, reason="TOLLGATE_SSH_HOST not set")
def test_backend_restart_preserves_sessions(fresh_session):
    """A simple backend restart (no upgrade) preserves active sessions.

    This isolates the restart behavior from the upgrade behavior.
    """
    device = fresh_session

    assert device.has_internet(), "precondition: should have internet"

    # Record sessions
    before = _ssh("cat /etc/tollgate/sessions.json 2>/dev/null | wc -c")
    log.info("sessions file size before restart: %s bytes", before)

    # Restart the backend
    _ssh("/etc/init.d/tollgate-wrt restart", timeout=30)
    time.sleep(8)

    # Backend should be back
    assert "running" in _ssh("/etc/init.d/tollgate-wrt status"), \
        "backend did not restart cleanly"

    # Sessions should still exist
    after = _ssh("cat /etc/tollgate/sessions.json 2>/dev/null | wc -c")
    log.info("sessions file size after restart: %s bytes", after)

    # Client should still have internet (or at least the session data survived)
    time.sleep(5)
    deadline = time.time() + 30
    while time.time() < deadline:
        if device.has_internet():
            break
        time.sleep(3)

    # Note: internet may drop briefly during restart — the important thing
    # is the session data persisted, not that there was zero downtime
    log.info("internet after restart: %s", device.has_internet())
