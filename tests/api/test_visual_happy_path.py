import base64
import os
import threading
import time
from typing import Any

import pytest

from lib.constants import TOKEN_DEFAULT
from lib.helpers import assert_deauthenticated, metering_test_setup, wait_expiry_and_verify_cutoff

pytestmark = [pytest.mark.story("P5"),pytest.mark.api, pytest.mark.virtual_lab, pytest.mark.publish_screenshot]

DATA_DOWNLOAD_URL = os.environ.get(
    "TOLLGATE_VISUAL_DATA_TEST_URL",
    "http://cachefly.cachefly.net/1mb.test",
)

try:
    from pytest_html import extras as html_extras
except ImportError:
    html_extras = None


def _skip_unless_virtual_lab():
    if not (os.environ.get("TOLLGATE_SSH_JUMP_HOST") or os.environ.get("TOLLGATE_VIRTUAL_HOST") or os.environ.get("TOLLGATE_VIRTUAL_LAB")):
        pytest.skip("set TOLLGATE_VIRTUAL_LAB=1 and run scripts/virtual-lab.py start-poc")


def _wait_for_auth_state(router, mac: str | None, timeout: int = 45) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if router.get_nds_state(mac) == "Authenticated":
            return True
        time.sleep(1)
    return router.get_nds_state(mac) == "Authenticated"


def _embed_screenshot(path: str, name: str, request):
    if not os.path.exists(path):
        return
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    if b64 and html_extras is not None:
        extras_list = getattr(request.node, "_screenshot_extras", [])
        extras_list.append(html_extras.image(b64, name))
        request.node._screenshot_extras = extras_list


def _embed_video(path: str, request):
    if not os.path.exists(path):
        return
    video_size = os.path.getsize(path)
    if video_size > 20 * 1024 * 1024:
        return
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    if b64 and html_extras is not None:
        extras_list = getattr(request.node, "_screenshot_extras", [])
        extras_list.append(
            html_extras.video(b64, name="Portal Happy Path", mime_type="video/webm", extension="webm")
        )
        request.node._screenshot_extras = extras_list


def _capture_visual_checkpoint(adb, results_dir: str, request, name: str):
    output_dir = os.path.join(results_dir, "raw", "visual")
    report_dir = os.path.join(results_dir, "report")
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, name)
    if adb.screenshot_portal(path, report_dir=report_dir):
        _embed_screenshot(path, os.path.basename(path).replace(".png", ""), request)
    return path


@pytest.mark.smoke
@pytest.mark.timeout(300)
def test_visual_happy_path(adb, cashu, router, results_dir, request):
    _skip_unless_virtual_lab()

    client = request.config.getoption("--client", default="adb")
    if client != "container":
        pytest.skip("visual test requires --client=container")

    # IPv6 on br-lan bypasses Nodogsplash's IPv4-only captive portal.
    # Without this, the container reaches the internet via IPv6 and the
    # Playwright recording never sees auth markers (ok=False).
    # See: https://github.com/OpenTollGate/physical-router-test-automation/issues/30
    router.disable_ipv6_on_lan()

    gateway = os.environ.get("TOLLGATE_VIRTUAL_GATEWAY", "10.99.99.1")
    client_mac = os.environ.get("TOLLGATE_CLIENT_MAC", "")
    output_dir = os.path.join(results_dir, "raw", "visual")
    os.makedirs(output_dir, exist_ok=True)

    print(f"[visual] gateway={gateway} client_mac={client_mac}")

    # Pre-check: verify Playwright is functional in the container
    try:
        pw_check = adb._exec("python3 -c 'from playwright.sync_api import sync_playwright; print(\"PW_OK\")' 2>&1", timeout=15)
        if "PW_OK" not in pw_check:
            pytest.fail(f"Playwright not functional in container: {pw_check[:200]}")
    except Exception as exc:
        pytest.fail(f"Cannot check Playwright in container: {exc}")

    portal_url = f"http://{gateway}/"
    code = ""
    for _ in range(20):
        code = adb.curl(portal_url, o="/dev/null", w="%{http_code}", s=True).strip()
        if code.startswith("2") or code.startswith("3") or code in ("404", "500"):
            break
        time.sleep(1)
    assert code.startswith("2") or code.startswith("3") or code in ("404", "500"), f"client must reach portal, got HTTP {code}"
    print(f"[visual] client can reach portal ({code})")

    # Step 0.5: deauth client so NDS intercepts and shows portal
    if client_mac:
        try:
            router.ssh(f"ndsctl deauth {client_mac} 2>/dev/null || true", timeout=10)
            time.sleep(1)
        except Exception:
            pass

    # Step 1: start recording thread (video + screenshots)
    adb.start_portal_recording()
    recording_result: list[dict[str, Any] | None] = [None]

    def run_recording():
        recording_result[0] = adb.finish_portal_recording(output_dir, timeout=120)

    recording_thread = threading.Thread(target=run_recording, daemon=True)
    recording_thread.start()

    # Step 2: wait for portal to load and unpaid screenshot
    # Use 90s timeout for cloud lab (Playwright startup is slower in QEMU)
    assert adb.wait_for_portal_ready(timeout=90), "Playwright did not load portal in time"
    print("[visual] portal loaded, unpaid screenshot taken by recording thread")

    # Step 3: mint a token and let the recorded browser paste + submit it
    print("[visual] minting Cashu token for browser payment...")
    token = cashu.mint(TOKEN_DEFAULT)
    assert token, "cashu mint failed"
    adb.signal_token(token)

    # Step 4: wait for browser payment authentication to take effect
    authenticated = _wait_for_auth_state(router, client_mac or None, timeout=45)
    print(f"[visual] authenticated={authenticated}")

    # Step 6: wait for recording to finish
    recording_thread.join(timeout=120)
    assert not recording_thread.is_alive(), "portal recording did not finish in time"
    artifacts = recording_result[0]
    print(f"[visual] recording done, artifacts={artifacts}")
    assert artifacts and artifacts.get("ok"), f"portal recording failed: {artifacts}"

    # Step 7: embed screenshots from recording
    if artifacts:
        for path in artifacts.get("screenshots", []):
            step = os.path.basename(path).replace(".png", "")
            _embed_screenshot(path, step, request)
            print(f"[visual] embedded screenshot: {step}")

        video_path = artifacts.get("video")
        if video_path:
            _embed_video(video_path, request)
            print("[visual] embedded video")

    # Step 8: standalone screenshot of the portal (in case recording failed)
    portal_shot = os.path.join(output_dir, "portal-standalone.png")
    try:
        adb.screenshot_portal(portal_shot)
        _embed_screenshot(portal_shot, "portal-standalone", request)
    except Exception as exc:
        print(f"[visual] standalone screenshot skipped: {exc}")

    assert authenticated, "client should be authenticated after ndsctl auth"


@pytest.mark.complete
@pytest.mark.critical
@pytest.mark.config
@pytest.mark.timeout(180)
def test_visual_time_metering_expiry(adb, cashu, router, wifi, test_pricing, results_dir, request):
    _skip_unless_virtual_lab()

    client = request.config.getoption("--client", default="adb")
    if client != "container":
        pytest.skip("visual metering test requires --client=container")

    amount = TOKEN_DEFAULT
    step_ms = 10_000
    expected_sec = ((amount - 1) * step_ms) // 1000

    try:
        session, _ = metering_test_setup(router, adb, wifi, cashu, test_pricing,
                                         amount, step_ms, "milliseconds")
        assert session.get("metric") == "milliseconds"
        _capture_visual_checkpoint(adb, results_dir, request, "05-time-authed.png")

        elapsed = wait_expiry_and_verify_cutoff(router, adb)
        _capture_visual_checkpoint(adb, results_dir, request, "06-time-expired.png")

        drift = abs(elapsed - expected_sec)
        assert drift <= 12, \
            f"Session duration {elapsed}s too far from expected {expected_sec}s (drift={drift}s)"
    finally:
        router.reset_state(adb=adb)


@pytest.mark.complete
@pytest.mark.critical
@pytest.mark.config
@pytest.mark.timeout(180)
def test_visual_data_metering_cutoff(adb, cashu, router, wifi, test_pricing, results_dir, request):
    _skip_unless_virtual_lab()

    if os.environ.get("TOLLGATE_VIRTUAL_LAB"):
        pytest.skip("data metering cutoff requires physical network (nftables byte counting)")

    client = request.config.getoption("--client", default="adb")
    if client != "container":
        pytest.skip("visual metering test requires --client=container")

    amount = TOKEN_DEFAULT
    step_bytes = 256 * 1024
    expected_bytes = amount * step_bytes

    try:
        session, _ = metering_test_setup(router, adb, wifi, cashu, test_pricing,
                                         amount, step_bytes, "bytes")
        assert session.get("metric") == "bytes", \
            f"Session metric is '{session.get('metric')}', expected 'bytes'"
        _capture_visual_checkpoint(adb, results_dir, request, "07-data-authed.png")

        cutoff_at = None
        start = time.time()
        while time.time() - start < 45:
            adb.curl(DATA_DOWNLOAD_URL, timeout=5, L=True, o="/dev/null", s=True)
            state = router.get_nds_state()
            if state != "Authenticated":
                cutoff_at = int(time.time() - start)
                break
            time.sleep(1)

        if cutoff_at is None:
            pytest.fail(f"Data cutoff not detected within 45s after consuming ~{expected_bytes}B")

        time.sleep(2)
        assert_deauthenticated(router)
        _capture_visual_checkpoint(adb, results_dir, request, "08-data-exhausted.png")
    finally:
        router.reset_state(adb=adb)
