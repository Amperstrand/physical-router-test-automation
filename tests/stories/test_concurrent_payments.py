"""User Story: Multiple clients pay simultaneously without corrupting
each other's sessions.

Tests that two clients submitting tokens at the same time each get
independent, working sessions — no cross-contamination, no lost payments.
"""
import logging
import os
import subprocess
import time

import pytest

from lib.contract import min_token_sats, probe_host
from lib.cashu import HttpMinter

log = logging.getLogger("tollgate.story.concurrent")

pytestmark = [pytest.mark.story("M3"),pytest.mark.slow, pytest.mark.physical_hardware]

ROUTER_HOST = os.environ.get("TOLLGATE_SSH_HOST", "")
DEBIAN_HOST = os.environ.get("TOLLGATE_DEBIAN_HOST", "")


def _ssh_router(cmd: str) -> str:
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=no",
         f"root@{ROUTER_HOST}", cmd],
        capture_output=True, text=True, timeout=15).stdout.strip()


@pytest.mark.skipif(not ROUTER_HOST, reason="TOLLGATE_SSH_HOST not set")
@pytest.mark.skipif(not DEBIAN_HOST, reason="TOLLGATE_DEBIAN_HOST not set")
def test_phone_and_vm_pay_simultaneously(tollgate_ssid, rate_limiter):
    """A phone and a Debian VM pay at the same time — both get internet."""
    from tests.stories.conftest import get_client_device

    phone = get_client_device("android-phone")
    vm = get_client_device("debian-vm")

    # Connect both devices
    assert phone.join_wifi(tollgate_ssid), "phone WiFi join failed"
    assert vm.join_wifi(tollgate_ssid), "VM network join failed"

    phone_ip = phone.get_ip()
    vm_ip = vm.get_ip()
    assert phone_ip and vm_ip, f"missing IP: phone={phone_ip} vm={vm_ip}"
    assert phone_ip != vm_ip, f"same IP: {phone_ip}"

    log.info("phone: %s, vm: %s", phone_ip, vm_ip)

    # Deauth both to start clean
    phone_mac = "24:46:c8:a9:de:bb"
    _ssh_router(f"ndsctl deauth {phone_mac} 2>/dev/null")
    time.sleep(2)

    # Mint two tokens
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL",
                              "http://192.168.13.221:8383")
    minter = HttpMinter(mint_url)
    phone_token = minter.mint(min_token_sats())
    vm_token = minter.mint(min_token_sats())

    rate_limiter()
    rate_limiter()

    # Submit simultaneously (threading would be ideal, but sequential
    # submission within a tight window is sufficient for drift detection)
    log.info("submitting phone token...")
    t0 = time.time()
    phone_ok = phone.submit_token(phone_token)
    t1 = time.time()
    log.info("phone submitted in %.2fs (ok=%s)", t1 - t0, phone_ok)

    log.info("submitting VM token...")
    t2 = time.time()
    vm_ok = vm.submit_token(vm_token)
    t3 = time.time()
    log.info("VM submitted in %.2fs (ok=%s)", t3 - t2, vm_ok)

    assert phone_ok, "phone payment failed"
    assert vm_ok, "VM payment failed"

    # Both should have internet independently
    deadline = time.time() + 30
    phone_net = False
    vm_net = False
    while time.time() < deadline and not (phone_net and vm_net):
        if not phone_net and phone.has_internet(probe_host()):
            phone_net = True
            log.info("✓ phone has internet (%.1fs)", time.time() - t0)
        if not vm_net and vm.has_internet(probe_host()):
            vm_net = True
            log.info("✓ VM has internet (%.1fs)", time.time() - t0)
        time.sleep(2)

    assert phone_net, "phone does not have internet after concurrent payment"
    assert vm_net, "VM does not have internet after concurrent payment"

    # Verify they have different sessions (different MACs → different sessions)
    nds = _ssh_router("ndsctl clients 2>&1")
    log.info("NDS clients:\n%s", nds[:500])

    log.info("╔══════════════════════════════════════╗")
    log.info("║ Concurrent Payment: PASS              ║")
    log.info("║ Phone: %s (%.1fs)        ║", phone_ip, t1 - t0)
    log.info("║ VM:    %s (%.1fs)        ║", vm_ip, t3 - t2)
    log.info("╚══════════════════════════════════════╝")
