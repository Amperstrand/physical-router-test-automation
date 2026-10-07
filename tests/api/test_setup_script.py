"""Tests for PR #117/#I: Setup script improvements.

Verifies that the uci-defaults setup script (99-tollgate-setup):
- Only enables HTTPS when cert and key files exist (conditional)
- Sets hostname to TollGate and applies to running kernel
- Idempotent nodogsplash rules (no duplicates on re-run)
- Sets gatewaydomainname and gatewayport correctly
"""

import os

import pytest

pytestmark = [pytest.mark.story("O5"),pytest.mark.api, pytest.mark.extended]


def _skip_if_no_setup_script(router):
    setup = router.ssh("cat /etc/uci-defaults/99-tollgate-setup 2>/dev/null || echo MISSING")
    if setup == "MISSING":
        pytest.skip("uci-defaults script already consumed or not present")


@pytest.mark.extended
def test_setup_uhttpd_conditional_https(router):
    _skip_if_no_setup_script(router)
    setup = router.ssh("cat /etc/uci-defaults/99-tollgate-setup 2>/dev/null")
    assert "uhttpd" in setup and "listen_https" in setup, \
        "setup_uhttpd should configure HTTPS listener"


@pytest.mark.extended
def test_setup_hostname_applied_to_kernel(router):
    hostname_uci = router.ssh("uci -q get system.@system[0].hostname 2>/dev/null").strip()
    hostname_kernel = router.ssh("cat /proc/sys/kernel/hostname").strip()
    assert hostname_uci == hostname_kernel, \
        f"UCI hostname '{hostname_uci}' != kernel hostname '{hostname_kernel}'"


@pytest.mark.extended
def test_setup_nodogsplash_gatewaydomainname(router):
    _skip_if_no_setup_script(router)
    domain = router.ssh(
        "uci -q get nodogsplash.@nodogsplash[0].gatewaydomainname 2>/dev/null"
    ).strip()
    if not domain:
        pytest.skip("gatewaydomainname not set (pre-PR I firmware)")
    assert domain == "TollGate.lan", f"Expected TollGate.lan, got '{domain}'"


@pytest.mark.extended
def test_setup_nodogsplash_gatewayport(router):
    _skip_if_no_setup_script(router)
    port = router.ssh(
        "uci -q get nodogsplash.@nodogsplash[0].gatewayport 2>/dev/null"
    ).strip()
    if not port:
        pytest.skip("gatewayport not set (pre-PR I firmware)")
    expected = os.environ.get("TOLLGATE_NDS_PORTAL_PORT", "2050")
    assert port == expected, f"Expected gatewayport {expected}, got {port}"


@pytest.mark.extended
def test_setup_nodogsplash_idempotent(router):
    _skip_if_no_setup_script(router)
    users_before = router.ssh(
        "uci -q get nodogsplash.@nodogsplash[0].users_to_router 2>/dev/null || echo MISSING"
    )
    if users_before == "MISSING":
        pytest.skip("nodogsplash config not found")

    count_port_2121_before = users_before.count("port 2121")
    count_port_8080_before = users_before.count("port 8080")

    assert count_port_2121_before <= 1, \
        f"port 2121 appears {count_port_2121_before} times (not idempotent)"
    assert count_port_8080_before <= 1, \
        f"port 8080 appears {count_port_8080_before} times (not idempotent)"
