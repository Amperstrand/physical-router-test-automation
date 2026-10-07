import os
import subprocess

import pytest

from lib.constants import POC_GATEWAY, NDS_PORTAL_PORT

pytestmark = [pytest.mark.story("P7"),pytest.mark.api, pytest.mark.smoke, pytest.mark.virtual_lab]


def _skip_unless_virtual_lab():
    if not (os.environ.get("TOLLGATE_SSH_JUMP_HOST")
            or os.environ.get("TOLLGATE_VIRTUAL_HOST")
            or os.environ.get("TOLLGATE_VIRTUAL_LAB")):
        pytest.skip("set TOLLGATE_VIRTUAL_LAB=1 and start the virtual lab")


def _netns_exec(*args, timeout=10):
    jump_host = os.environ.get("TOLLGATE_SSH_JUMP_HOST", "")
    password = os.environ.get("TOLLGATE_SSH_PASSWORD",
                              os.environ.get("TOLLGATE_LUCI_PASSWORD", "tollgate"))

    ns_cmd = ["sudo", "ip", "netns", "exec", "tg-poc-client"] + list(args)
    ssh_cmd = ["sshpass", "-p", password, "ssh",
               "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null",
               "-o", "LogLevel=ERROR",
               jump_host] + ns_cmd

    return subprocess.run(
        ssh_cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.mark.smoke
def test_container_reaches_openwrt_gateway(adb, request):
    _skip_unless_virtual_lab()

    client = request.config.getoption("--client", default="adb")
    gateway = os.environ.get("TOLLGATE_VIRTUAL_GATEWAY", POC_GATEWAY)
    portal_url = f"http://{gateway}:{NDS_PORTAL_PORT}/"

    if client == "container":
        code = adb.curl(portal_url, o="/dev/null", w="%{http_code}", s=True)
        code = code.strip()
    else:
        if not os.environ.get("TOLLGATE_SSH_JUMP_HOST"):
            pytest.skip("requires TOLLGATE_SSH_JUMP_HOST for network namespace access")
        result = _netns_exec("curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                             portal_url, timeout=15)
        code = result.stdout.strip()

    assert code.startswith("2") or code.startswith("3") or code in ("404", "500"), \
        f"Client could not reach NDS portal at {portal_url} (HTTP {code})"
