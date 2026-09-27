"""Android phone as a labgrid resource — presence, identity, wifi state.

Seed for the portal-UX lane (phone joins router-alpha's SSID, detects the
captive portal, pays). Skips cleanly when no phone is plugged/authorized.

Run:
  TOLLGATE_SSH_HOST= ROUTER_IP= TOLLGATE_VIRTUAL_LAB= \
  ~/venvs/rig-labgrid/bin/python -m pytest \
    --lg-env=configs/labgrid/android-phone.yaml --no-deploy \
    tests/test_android_labgrid.py -v
"""

import os
import subprocess
import sys

import pytest

# Registers AndroidADDDevice + AndroidADBDriver with labgrid's factory.
import tollgate_lab.drivers.android_adb  # noqa: F401
from tollgate_lab.drivers.android_adb import AndroidADBDriver

pytestmark = [pytest.mark.timeout(120), pytest.mark.smoke]

COORDINATOR = os.environ.get("BENCH_COORDINATOR", "192.168.13.208:20408")

#: The bench's single test phone; override when the fleet grows.
EXPECTED_MODEL = os.environ.get("BENCH_PHONE_MODEL", "moto g(7) power")


@pytest.fixture(scope="session")
def android_place():
    """Acquire the coordinator place for this session (bench convention:
    explicit acquire/release, like the bolty/fips-lab HIL conftests)."""
    client = os.path.join(os.path.dirname(sys.executable), "labgrid-client")
    r = subprocess.run([client, "-x", COORDINATOR, "-p", "android-phone",
                        "acquire"], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        pytest.skip(f"cannot acquire android-phone place: {r.stderr.strip()}")
    yield
    subprocess.run([client, "-x", COORDINATOR, "-p", "android-phone",
                    "release"], capture_output=True, timeout=30)


def _adb_devices() -> list[str]:
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True,
                         timeout=15).stdout
    return [line.split("\t")[0] for line in out.splitlines()
            if line.endswith("\tdevice")]


def _lastline(lines: list[str]) -> str:
    """Last non-empty, stripped line of a run_check result."""
    for line in reversed(lines):
        if line.strip():
            return line.strip()
    return ""


def _driver(android_place, target) -> AndroidADBDriver:
    """Activate the driver, skipping cleanly when no phone is present."""
    import labgrid.exceptions as lg_exc

    try:
        drv = target.get_driver(AndroidADBDriver)
    except (lg_exc.NoDriverFoundError, lg_exc.InvalidConfigError) as exc:
        pytest.skip(f"android-phone resources unavailable: {exc}")
    devices = _adb_devices()
    if not devices:
        pytest.skip("no adb device visible — plug the phone in, enable USB "
                    "debugging, and accept the RSA authorization prompt "
                    "(see docs/android-phone-bringup.md)")
    return drv


def test_phone_present_and_responsive(android_place, target):
    driver = _driver(android_place, target)
    assert driver.get_status() == "device", "adb get-state not 'device'"
    assert _lastline(driver.run_check("echo labgrid-android-ok")) == \
        "labgrid-android-ok"


def test_phone_identity(android_place, target):
    driver = _driver(android_place, target)
    model = _lastline(driver.run_check("getprop ro.product.model"))
    android = _lastline(driver.run_check("getprop ro.build.version.release"))
    assert model == EXPECTED_MODEL, f"unexpected phone: {model!r}"
    assert android, "empty ro.build.version.release"


def test_phone_wifi_state_dump(android_place, target):
    """Wifi must be toggleable for portal tests; dump the current state."""
    driver = _driver(android_place, target)
    wifi = driver.run_check(
        "settings query global wifi_on 2>/dev/null || "
        "dumpsys wifi | grep -m1 'Wi-Fi is'")
    assert wifi, "no wifi state output — is this an Android with wifi?"


# Portal-UX seed (requires router-alpha's SSID up + phone MAC allowed):
#   driver.run_check("svc wifi enable")
#   driver.run_check("cmd wifi connect <ssid> wpa2 <psk>")
#   open_url('http://neverssl.com') -> screenshot() -> detect portal -> pay
