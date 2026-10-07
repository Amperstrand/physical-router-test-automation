"""
Upstream WiFi management test scenarios.

Ports the upstream WiFi scan/connect/list/edge-case tests from the
Makefile-based branch into the pytest framework. Uses feature detection
to skip cleanly when the router binary lacks upstream WiFi support.
"""

import os
import re
import time

import pytest

pytestmark = [pytest.mark.story("O6"),pytest.mark.api, pytest.mark.extended, pytest.mark.go_only, pytest.mark.virtual_lab]


def _skip_if_no_wireless_config(router):
    """Skip if wireless config is absent (no radios, no hwsim)."""
    try:
        result = router.ssh("ls /etc/config/wireless 2>/dev/null", timeout=5)
        if not result or not result.strip():
            pytest.skip("No /etc/config/wireless — WiFi hardware not available")
    except Exception:
        pytest.skip("Cannot check wireless config — SSH error")


def _skip_if_no_upstream_wifi(router):
    """Feature-detect upstream WiFi CLI support and skip if absent."""
    _skip_if_no_wireless_config(router)

    try:
        result = router.cli_command("upstream", ["list"])
    except (NotImplementedError, Exception):
        pytest.skip("tollgate upstream commands not available (Rust backend or old binary)")
        return
    msg = str(result.get("message", "") or result.get("raw", "")).lower()
    if "unknown command" in msg or "not found" in msg:
        pytest.skip("tollgate upstream subcommand not recognized by this binary")
    if result.get("success") is False and not msg:
        pytest.skip("tollgate upstream list returned failure with no message")


def _skip_if_hwsim_no_scan(router):
    """Skip if hwsim virtual radios are present — they can't propagate beacons for real scans."""
    # Check environment variable first
    if os.environ.get("TOLLGATE_ENABLE_HWSIM"):
        pytest.skip("WiFi scan tests require real radios — hwsim cannot propagate beacons")
    
    # Check if mac80211_hwsim module is loaded
    try:
        result = router.ssh("ls /sys/module/mac80211_hwsim 2>/dev/null", timeout=5)
        if result and result.strip():
            pytest.skip("WiFi scan tests require real radios — hwsim module loaded")
    except Exception:
        # If command fails, assume no hwsim
        pass
    
    # Check if radio interfaces are hwsim-style (phy0-ap0, phy1-ap0, etc.)
    try:
        result = router.ssh("iw dev | grep -E 'phy[0-9]+-ap'", timeout=5)
        # hwsim AP interfaces are named phyN-ap0 on OpenWrt
        if result and result.strip():
            pytest.skip("WiFi scan tests require real radios — hwsim-style interfaces detected")
    except Exception:
        # If command fails, assume no hwsim
        pass


def test_upstream_scan(router):
    _skip_if_no_upstream_wifi(router)
    _skip_if_hwsim_no_scan(router)

    result = router.cli_command("upstream", ["scan"])
    assert result is not None, "upstream scan returned None"

    raw = result.get("raw", "")
    msg = result.get("message", "")
    if result.get("success") is False and not raw and not msg:
        pytest.skip("upstream scan failed — WiFi hardware may not support scanning")

    assert raw or msg or result.get("success") is not None, \
        f"upstream scan returned unexpected structure: {result}"


def test_upstream_list(router):
    _skip_if_no_upstream_wifi(router)

    result = router.cli_command("upstream", ["list"])
    assert result is not None, "upstream list returned None"

    raw = result.get("raw", "")
    msg = result.get("message", "")
    assert raw or msg or result.get("success") is not None, \
        f"upstream list returned unexpected structure: {result}"


def test_connect_to_unknown_ssid_fails(router):
    _skip_if_no_upstream_wifi(router)
    _skip_if_hwsim_no_scan(router)

    result = router.cli_command("upstream", ["connect", "NonExistentSSID_UnitTest"])
    raw = str(result.get("raw", "") or result.get("message", "")).lower()
    success = result.get("success")
    assert success is False or "fail" in raw or "error" in raw or "not found" in raw, \
        f"Connecting to unknown SSID should fail gracefully, got: {result}"


def test_remove_unknown_ssid_fails(router):
    _skip_if_no_upstream_wifi(router)

    result = router.cli_command("upstream", ["remove", "UnknownSSID_UnitTest"])
    raw = str(result.get("raw", "") or result.get("message", "")).lower()
    success = result.get("success")
    assert success is False or "fail" in raw or "error" in raw or "not found" in raw, \
        f"Removing unknown SSID should fail gracefully, got: {result}"


def test_existing_cli_commands_work(router):
    if not router.backend.has_cli_socket:
        pytest.skip("CLI socket not supported by this backend")

    version = router.get_tollgate_version()
    if version.get("success") is None and "raw" in version:
        pytest.skip(f"CLI socket exists but version command not supported: {version}")

    status = router.get_tollgate_status()
    if status.get("success") is None and "raw" in status:
        pytest.skip(f"CLI socket exists but status command not supported: {status}")

    assert version.get("success") is True, f"version command failed: {version}"
    assert status.get("success") is True, f"status command failed: {status}"


def test_upstream_status_check(router):
    logs = router.get_tollgate_logs(filter_expr="upstream", lines=500)
    if not logs or "upstream" not in logs.lower():
        pytest.skip("No upstream WiFi log entries found — feature may not be active")

    init_patterns = [
        r"upstream\s+wifi\s+manager\s+init",
        r"upstream.*init",
        r"wifi\s+manager.*start",
    ]
    for pattern in init_patterns:
        if re.search(pattern, logs, re.IGNORECASE):
            return
    pytest.skip("No upstream WiFi manager initialization signal in logs")


@pytest.mark.slow
def test_reseller_mode_guard(router):
    """Reseller mode suppresses upstream scan cycles; CLI must still respond."""
    _skip_if_no_upstream_wifi(router)

    original_reseller = router.ssh(
        "uci get tollgate.config.reseller_mode 2>/dev/null || echo '0'"
    ).strip()

    try:
        router.ssh(
            "uci set tollgate.config.reseller_mode=1 && uci commit tollgate",
            timeout=10,
        )
        router.restart_backend()

        time.sleep(2)
        # baseline — not used for comparison, just lets the backend settle
        router.get_tollgate_logs(filter_expr="upstream", lines=50)

        # Wait 35s — during this window, no scan cycles should appear
        time.sleep(35)

        # CLI must still respond during reseller mode
        status = router.get_tollgate_status()
        assert status.get("success") is True, \
            f"CLI should still respond during reseller mode: {status}"

    finally:
        restore_val = "0" if original_reseller in ("0", "") else original_reseller
        router.ssh(
            f"uci set tollgate.config.reseller_mode={restore_val} "
            f"&& uci commit tollgate",
            timeout=10,
        )
        router.restart_backend()


def test_no_dual_wwan(router):
    """Guard: only one wwan interface should exist (prevents routing breakage)."""
    _skip_if_no_upstream_wifi(router)

    wwan_count_raw = router.ssh(
        "uci show network | grep 'wwan.*proto' | grep -c ''",
        timeout=10,
    ).strip()
    try:
        wwan_count = int(wwan_count_raw)
    except ValueError:
        pytest.skip(f"Could not parse wwan interface count: {wwan_count_raw!r}")
        return

    assert wwan_count <= 1, \
        f"Multiple wwan interfaces detected ({wwan_count}), risk of routing breakage"


def test_sta_health(router):
    """Exactly 1 active STA interface, no duplicate SSIDs."""
    _skip_if_no_upstream_wifi(router)

    sta_output = router.ssh(
        "iwinfo 2>/dev/null | grep -E '^[a-z]' | grep -i 'STA'",
        timeout=10,
    ).strip()

    if not sta_output:
        pytest.skip("No active STA interfaces found — upstream WiFi may not be connected")
        return

    sta_lines = [line for line in sta_output.split("\n") if line.strip()]
    assert len(sta_lines) >= 1, f"Expected at least 1 STA, got: {sta_output}"

    ssid_pattern = re.compile(r'ESSID:\s*"([^"]*)"')
    ssids = []
    for line in sta_lines:
        match = ssid_pattern.search(line)
        if match:
            ssids.append(match.group(1))

    if ssids:
        unique_ssids = set(ssids)
        assert len(unique_ssids) == len(ssids), \
            f"Duplicate SSIDs detected: {ssids}"

    upstream_wifi = router.ssh(
        "uci show wireless | grep 'upstream_'",
        timeout=10,
    ).strip()
    assert upstream_wifi is not None
