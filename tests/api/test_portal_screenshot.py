import os

import pytest

pytestmark = [pytest.mark.story("P5"),pytest.mark.api, pytest.mark.smoke, pytest.mark.virtual_lab]


def _skip_unless_virtual_lab():
    has_jump = os.environ.get("TOLLGATE_SSH_JUMP_HOST", "") != ""
    has_vhost = os.environ.get("TOLLGATE_VIRTUAL_HOST", "") != ""
    has_vlab = os.environ.get("TOLLGATE_VIRTUAL_LAB", "") != ""
    if not (has_jump or has_vhost or has_vlab):
        pytest.skip("set TOLLGATE_VIRTUAL_LAB=1 and run scripts/virtual-lab.py start-poc")


@pytest.mark.smoke
def test_portal_screenshot(screenshot_portal, adb, request):
    _skip_unless_virtual_lab()

    client = request.config.getoption("--client", default="adb")
    if client != "container":
        pytest.skip("portal screenshot visual test requires --client=container")

    gateway = os.environ.get("TOLLGATE_VIRTUAL_GATEWAY", "10.99.99.1")
    assert adb.ping(gateway, count=1, timeout=3), "client must reach gateway before screenshot"

    screenshot_portal("portal-home.png")
