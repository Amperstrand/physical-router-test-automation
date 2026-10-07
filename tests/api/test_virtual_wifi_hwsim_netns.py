import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


pytestmark = [pytest.mark.story("P7"),pytest.mark.api, pytest.mark.virtual_wifi, pytest.mark.hwsim_netns]

REPO_ROOT = Path(__file__).resolve().parents[2]
POC_SCRIPT = REPO_ROOT / "scripts" / "hwsim-netns-poc.py"


def _enabled() -> bool:
    return os.environ.get("TOLLGATE_WIFI_PLANE") == "hwsim-netns" or os.environ.get(
        "TOLLGATE_ENABLE_HWSIM_NETNS"
    ) == "1"


if not _enabled():
    pytest.skip(
        "set TOLLGATE_WIFI_PLANE=hwsim-netns to run virtual Wi-Fi namespace POC",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def hwsim_netns_result():
    if not POC_SCRIPT.exists():
        pytest.skip(f"POC script missing: {POC_SCRIPT}")
    check = subprocess.run(
        [sys.executable, str(POC_SCRIPT), "check"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    try:
        prereqs = json.loads(check.stdout or "{}")
    except json.JSONDecodeError:
        prereqs = {"raw": check.stdout, "stderr": check.stderr}
    if check.returncode != 0:
        pytest.skip(f"hwsim-netns prerequisites unavailable: {prereqs}")

    run = subprocess.run(
        [sys.executable, str(POC_SCRIPT), "run", "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    data: dict[str, Any]
    try:
        data = json.loads(run.stdout or "{}")
    except json.JSONDecodeError:
        data = {"ok": False, "raw": run.stdout, "stderr": run.stderr}
    data["returncode"] = run.returncode
    return data


def test_client_scan_sees_alpha(hwsim_netns_result):
    assert hwsim_netns_result.get("scan", {}).get("TollGate-ALPHA") is True, hwsim_netns_result


def test_client_scan_sees_bravo(hwsim_netns_result):
    assert hwsim_netns_result.get("scan", {}).get("TollGate-BRAVO") is True, hwsim_netns_result


def test_client_associates_with_alpha(hwsim_netns_result):
    assert hwsim_netns_result.get("alpha", {}).get("associated") is True, hwsim_netns_result


def test_client_associates_with_bravo(hwsim_netns_result):
    assert hwsim_netns_result.get("bravo", {}).get("associated") is True, hwsim_netns_result


def test_client_gets_dhcp_on_alpha(hwsim_netns_result):
    assert hwsim_netns_result.get("alpha", {}).get("dhcp") is True, hwsim_netns_result


def test_client_gets_dhcp_on_bravo(hwsim_netns_result):
    assert hwsim_netns_result.get("bravo", {}).get("dhcp") is True, hwsim_netns_result


def test_client_reaches_alpha_captive_endpoint(hwsim_netns_result):
    assert hwsim_netns_result.get("alpha", {}).get("http") is True, hwsim_netns_result


def test_client_reaches_bravo_captive_endpoint(hwsim_netns_result):
    assert hwsim_netns_result.get("bravo", {}).get("http") is True, hwsim_netns_result
