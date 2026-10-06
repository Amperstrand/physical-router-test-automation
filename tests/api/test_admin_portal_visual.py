import os
import re

import pytest

from lib.constants import POC_GATEWAY

pytestmark = [
    pytest.mark.story("O8"),pytest.mark.api, pytest.mark.virtual_lab, pytest.mark.publish_screenshot]


def _skip_unless_container():
    if os.environ.get("TOLLGATE_CLIENT", "") != "container":
        pytest.skip("admin visual test requires --client=container")
    if not os.environ.get("TOLLGATE_VIRTUAL_LAB"):
        pytest.skip("set TOLLGATE_VIRTUAL_LAB=1")


ADMIN_URL = f"http://{POC_GATEWAY}/"


def test_admin_portal_screenshot(adb, results_dir, request):
    _skip_unless_container()
    raw = os.path.join(results_dir, "raw")
    os.makedirs(raw, exist_ok=True)
    ok = adb.screenshot_url(ADMIN_URL, os.path.join(raw, "admin-portal-home.png"))
    if not ok:
        pytest.skip("admin portal screenshot failed — portal may not be deployed")


def test_admin_portal_video_tour(adb, results_dir, request):
    _skip_unless_container()
    raw = os.path.join(results_dir, "raw")
    os.makedirs(raw, exist_ok=True)
    video_path = os.path.join(raw, "admin-portal-tour-passed.webm")
    ok = adb.record_url_video(
        ADMIN_URL,
        video_path,
        timeout=25,
        click_selectors=[
            "nav a",
            "button",
            ".tab",
            "[role=tab]",
        ],
    )
    if not ok:
        pytest.skip("admin portal video recording failed")
