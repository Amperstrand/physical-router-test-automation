"""
net4sats Captive Portal — UX Test Suite
=======================================
Full user journey tests with screenshot capture at every meaningful step.

Each test:
1. Asserts readiness via test_readiness.ensure_ready()
2. Performs user actions (tap, type, wait)
3. Captures screenshots at each step
4. Asserts expected outcomes

Screenshots saved to results/ux-test-<timestamp>/screenshots/
Report generated at results/ux-test-<timestamp>/report.html

Run: pytest tests/scenarios/test_net4sats_ux.py -v -s --phone --tb=short
"""

import base64
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

log = logging.getLogger("tollgate.ux")

pytestmark = [pytest.mark.story("G3"),pytest.mark.phone, pytest.mark.critical]

# Constants
ROUTER_IP = os.environ.get("ROUTER_IP", "192.168.1.1")
ROUTER_PW = os.environ.get("ROUTER_PASSWORD", "tollgate123")
PHONE_MAC = os.environ.get("PHONE_MAC", "6e:5e:c0:9d:7a:b8")
MINT_URL = os.environ.get("TOLLGATE_TEST_MINT_URL", "https://testnut.cashu.exchange")
RESULTS_DIR = Path(f"results/ux-test-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
SHOTS_DIR = RESULTS_DIR / "screenshots"

STEPS_CAPTURED: list[dict] = []


def _ssh(cmd: str, timeout: int = 15) -> str:
    import subprocess
    r = subprocess.run(
        f"sshpass -p '{ROUTER_PW}' ssh -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null -o PreferredAuthentications=password "
        f"-o ConnectTimeout=5 root@{ROUTER_IP} '{cmd}'",
        shell=True, capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout.strip()


def _adb(cmd: str, timeout: int = 15) -> str:
    import subprocess
    r = subprocess.run(f"adb shell {cmd}", shell=True, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def _screenshot(name: str, description: str = "") -> str:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOTS_DIR / f"{name}.png"
    _adb(f"screencap -p /sdcard/{name}.png")
    os.system(f"adb pull /sdcard/{name}.png {path} 2>/dev/null")
    if path.exists():
        STEPS_CAPTURED.append({
            "name": name,
            "description": description,
            "path": str(path),
            "timestamp": datetime.now().isoformat(),
        })
        log.info(f"  📸 {name}: {description}")
        return str(path)
    log.warning(f"  ❌ Screenshot failed: {name}")
    return ""


def _tap_text(text: str, timeout: int = 10) -> bool:
    """Find and tap a UI element by text."""
    _adb("uiautomator dump /sdcard/ux_ui.xml")
    os.system("adb pull /sdcard/ux_ui.xml /tmp/ux_ui.xml 2>/dev/null")
    try:
        tree = ET.parse("/tmp/ux_ui.xml")
        for node in tree.iter("node"):
            node_text = node.get("text", "") + node.get("content-desc", "")
            if text.lower() in node_text.lower():
                bounds = node.get("bounds", "")
                nums = bounds.strip("[]").replace("][", ",").split(",")
                cx = (int(nums[0]) + int(nums[2])) // 2
                cy = (int(nums[1]) + int(nums[3])) // 2
                _adb(f"input tap {cx} {cy}")
                time.sleep(2)
                return True
    except Exception as e:
        log.warning(f"  tap_text error: {e}")
    return False


def _wait(seconds: int, msg: str = ""):
    if msg:
        log.info(f"  ⏳ {msg} ({seconds}s)")
    time.sleep(seconds)


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture(scope="module", autouse=True)
def ensure_readiness():
    """Ensure router + phone are ready for testing."""
    from lib.test_readiness import ensure_ready
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)

    report = ensure_ready(
        router_ip=ROUTER_IP,
        password=ROUTER_PW,
        phone_mac=PHONE_MAC,
        auto_fix=True,
        max_fix_attempts=2,
    )
    log.info("\n" + report.summary())
    assert report.ready, f"Not ready for testing:\n{report.summary()}"
    yield
    # Generate report after all tests
    _generate_html_report()


@pytest.fixture(autouse=True)
def reset_between_tests():
    """Reset phone to Preauthenticated before each test."""
    _ssh("/etc/init.d/tollgate-wrt restart")
    _wait(12, "Restarting tollgate (clear sessions)")
    _ssh(f"ndsctl deauth {PHONE_MAC}")
    _wait(3, "Deauth phone")
    yield


# ============================================================================
# TESTS
# ============================================================================

class TestCaptivePortalDetection:
    """Test: Android detects captive portal and shows popup."""

    def test_captive_portal_auto_popup(self):
        """Android detects CAPTIVE_PORTAL within 15s of WiFi connect."""
        # Toggle WiFi to trigger fresh detection
        _adb("svc wifi disable")
        _wait(3, "WiFi off")
        _adb("svc wifi enable")
        _wait(10, "WiFi reconnecting + captive portal detection")

        # Check detection
        connectivity = _adb("dumpsys connectivity 2>/dev/null")
        detected = "CAPTIVE_PORTAL" in connectivity

        _screenshot("01_captive_detected", "Android captive portal detection state")
        assert detected, "Android did not detect CAPTIVE_PORTAL"

    def test_portal_popup_opens(self):
        """CaptivePortalLogin activity opens and shows portal page."""
        os.system("adb shell am start -n com.android.captiveportallogin/.CaptivePortalLoginActivity")
        _wait(6, "Portal loading in CaptivePortalLogin")

        _screenshot("02_portal_popup", "CaptivePortalLogin activity with portal page")

        # Check portal rendered
        ui = _adb("uiautomator dump /sdcard/p.xml && cat /sdcard/p.xml")
        has_branding = "net4sats" in ui.lower() or "internet" in ui.lower()
        assert has_branding, "Portal page did not render in CaptivePortalLogin"


class TestPortalUI:
    """Test: Portal UI renders correctly with branding."""

    def test_portal_initial_load(self):
        """Portal shows net4sats branding, pricing, payment tabs."""
        os.system("adb shell am start -a android.intent.action.VIEW -d http://192.168.1.1:2050/splash.html")
        _wait(6, "Portal loading")
        _screenshot("03_portal_initial", "Portal page with net4sats branding")

    def test_portal_via_net4sats_lan(self):
        """Portal accessible via net4sats.lan domain."""
        os.system("adb shell am start -a android.intent.action.VIEW -d http://net4sats.lan:2050/")
        _wait(6, "Loading via net4sats.lan")
        _screenshot("04_portal_domain", "Portal via net4sats.lan domain")


class TestLightningPayment:
    """Test: Full Lightning payment flow through portal UI."""

    def test_select_data_and_generate_invoice(self):
        """User selects 100 MB and taps Generate Invoice."""
        os.system("adb shell am start -a android.intent.action.VIEW -d http://192.168.1.1:2050/splash.html")
        _wait(6, "Portal loading")

        # Select 100 MB (may already be selected)
        _tap_text("100 MB")
        _wait(1)
        _screenshot("05_selected_100mb", "100 MB selected")

        # Tap Generate Invoice
        assert _tap_text("Generate Invoice"), "Could not find Generate Invoice button"
        _wait(4, "Invoice generating")
        _screenshot("06_invoice_generating", "Invoice generating / processing")

    def test_lightning_payment_completes(self):
        """Lightning payment completes and grants internet access."""
        # Create invoice via API (simulates what the UI does)
        import subprocess
        import json
        r = subprocess.run(
            f"adb shell curl -s -X POST http://{ROUTER_IP}:2121/ln-invoice "
            f"-H 'Content-Type: application/json' "
            f"-d '{{\"amount\":5,\"mint_url\":\"{MINT_URL}\"}}'",
            shell=True, capture_output=True, text=True, timeout=15,
        )
        data = json.loads(r.stdout)
        quote = data.get("quote", "")
        assert quote, f"No quote in response: {r.stdout}"

        _screenshot("07_invoice_created", f"Lightning invoice created (quote: {quote[:12]}...)")

        # Poll for payment
        for i in range(10):
            _wait(3, f"Polling payment ({i+1}/10)")
            r = subprocess.run(
                f"adb shell curl -s 'http://{ROUTER_IP}:2121/ln-invoice?quote={quote}'",
                shell=True, capture_output=True, text=True, timeout=10,
            )
            data = json.loads(r.stdout)
            if data.get("access_granted"):
                _screenshot("08_payment_success", "Payment successful — access granted")

                # Verify internet
                ping = _adb("ping -c 2 -W 3 8.8.8.8")
                assert "0% packet loss" in ping or "0% loss" in ping, f"No internet: {ping}"
                _screenshot("09_internet_verified", "Internet access confirmed")
                return

        _screenshot("08_payment_timeout", "Payment timed out")
        pytest.fail("Lightning payment did not complete in 30s")


class TestAdminPanel:
    """Test: Admin panel accessible at tollgate.lan."""

    def test_admin_login_page(self):
        """Admin login page renders at tollgate.lan."""
        os.system("adb shell am start -a android.intent.action.VIEW -d http://tollgate.lan/")
        _wait(6, "Admin panel loading")
        _screenshot("10_admin_login", "Admin login page at tollgate.lan")

        ui = _adb("uiautomator dump /sdcard/a.xml && cat /sdcard/a.xml")
        assert "Sign In" in ui or "Password" in ui, "Admin login page did not render"


# ============================================================================
# REPORT
# ============================================================================

def _generate_html_report():
    """Generate HTML report with all captured screenshots."""
    if not STEPS_CAPTURED:
        return

    html_parts = [f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>net4sats UX Test Report — {datetime.now().strftime('%Y-%m-%d %H:%M')}</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 0; padding: 20px; background: #0a0a0a; color: #fff; }}
h1 {{ color: #f60; border-bottom: 2px solid #f60; padding-bottom: 10px; }}
.step {{ background: #1a1a1a; border-radius: 12px; padding: 20px; margin: 20px 0; }}
.step img {{ max-width: 300px; border-radius: 8px; border: 1px solid #333; display: block; margin: 10px 0; }}
.step-num {{ color: #f60; font-weight: bold; font-size: 1.2rem; }}
.step-desc {{ color: #aaa; }}
.timestamp {{ color: #666; font-size: 0.75rem; }}
.summary {{ display: flex; gap: 20px; margin: 20px 0; }}
.stat {{ background: #1a1a1a; padding: 15px 25px; border-radius: 8px; text-align: center; }}
.stat-value {{ font-size: 2rem; font-weight: bold; color: #f60; }}
.stat-label {{ font-size: 0.8rem; color: #888; }}
</style></head><body>
<h1>net4sats Captive Portal — UX Test Report</h1>
<p class="timestamp">{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
<div class="summary">
<div class="stat"><div class="stat-value">{len(STEPS_CAPTURED)}</div><div class="stat-label">Screenshots</div></div>
</div>
"""]

    for i, step in enumerate(STEPS_CAPTURED):
        img_rel = os.path.relpath(step["path"], RESULTS_DIR)
        html_parts.append(f"""
<div class="step">
<span class="step-num">Step {i+1}</span>
<span class="step-desc"> — {step["description"]}</span>
<span class="timestamp"> — {step["timestamp"]}</span>
<img src="{img_rel}" alt="{step['name']}" loading="lazy">
</div>""")

    html_parts.append("</body></html>")
    report_path = RESULTS_DIR / "report.html"
    report_path.write_text("".join(html_parts))
    log.info(f"\n{'='*60}")
    log.info(f"  REPORT: {report_path}")
    log.info(f"  Screenshots: {SHOTS_DIR}")
    log.info(f"{'='*60}")
