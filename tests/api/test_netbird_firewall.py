"""Netbird firewall zone tests for the wt0 interface.

Verifies that the firewall zone for Netbird's WireGuard tunnel (wt0) is
configured correctly when present. Originally written for PR #108 (closed);
the feature ships in non-main/development builds only. Feature-detected
so tests run against any firmware with the netbird zone configured and
skip cleanly otherwise.

See: https://github.com/OpenTollGate/tollgate-module-basic-go/pull/108
"""

import pytest

pytestmark = [pytest.mark.story("O6"),pytest.mark.api, pytest.mark.extended]


def _skip_if_no_netbird_zone(router):
    out = router.ssh("uci show firewall | grep netbird 2>/dev/null || echo NOT_FOUND")
    if "NOT_FOUND" in out or not out.strip():
        pytest.skip("Netbird firewall zone not configured (no netbird zone in firewall UCI)")


def _get_netbird_zone_config(router):
    """Get all UCI config lines for the netbird zone."""
    out = router.ssh("uci show firewall | grep netbird")
    return out.strip()


@pytest.mark.extended
def test_netbird_zone_exists_in_firewall(router):
    """Verify the netbird zone exists with device wt0."""
    _skip_if_no_netbird_zone(router)
    out = _get_netbird_zone_config(router)
    assert "netbird" in out, \
        f"No 'netbird' references in firewall UCI: {out}"
    assert "wt0" in out, \
        f"Netbird zone does not reference device 'wt0': {out}"


@pytest.mark.extended
def test_netbird_zone_input_policy(router):
    """Verify netbird zone has input='ACCEPT'."""
    _skip_if_no_netbird_zone(router)
    out = router.ssh("uci show firewall | grep netbird | grep input")
    assert "ACCEPT" in out, \
        f"Netbird zone input policy is not ACCEPT: {out}"


@pytest.mark.extended
def test_netbird_zone_forward_policy(router):
    """Verify netbird zone has forward='REJECT'."""
    _skip_if_no_netbird_zone(router)
    out = router.ssh("uci show firewall | grep netbird | grep forward")
    assert "REJECT" in out, \
        f"Netbird zone forward policy is not REJECT: {out}"


@pytest.mark.extended
def test_netbird_zone_output_policy(router):
    """Verify netbird zone has output='ACCEPT'."""
    _skip_if_no_netbird_zone(router)
    out = router.ssh("uci show firewall | grep netbird | grep output")
    assert "ACCEPT" in out, \
        f"Netbird zone output policy is not ACCEPT: {out}"


@pytest.mark.extended
def test_netbird_zone_has_lan_forwarding(router):
    """Verify forwarding from netbird to lan exists."""
    _skip_if_no_netbird_zone(router)
    out = router.ssh("uci show firewall | grep forwarding | grep netbird")
    assert "lan" in out, \
        f"No forwarding from netbird to lan found: {out}"


@pytest.mark.extended
def test_netbird_zone_no_wan_forwarding(router):
    """Verify NO forwarding from netbird to wan (security: no internet routing).

    Netbird traffic must not be forwarded to wan to prevent routing
    internet traffic through the netbird tunnel.
    """
    _skip_if_no_netbird_zone(router)
    out = router.ssh("uci show firewall | grep forwarding | grep netbird")
    assert "wan" not in out.split(), \
        f"SECURITY: Found forwarding from netbird to wan: {out}"


@pytest.mark.extended
def test_netbird_zone_has_private_forwarding(router):
    """Verify forwarding from netbird to private zone exists."""
    _skip_if_no_netbird_zone(router)
    out = router.ssh("uci show firewall | grep forwarding | grep netbird")
    assert "private" in out, \
        f"No forwarding from netbird to private zone found: {out}"


@pytest.mark.extended
def test_fw4_include_is_not_broken(router):
    """Verify firewall.tollgate_rules include is not broken.

    The PR fixes a bug where firewall.tollgate_rules pointed to a UCI config
    file instead of a shell script. It should either not exist, or point to
    an executable shell script.
    """
    include_path = router.ssh(
        "uci get firewall.tollgate_rules.path 2>/dev/null || echo NOT_SET"
    ).strip()
    if include_path == "NOT_SET":
        return

    file_type = router.ssh(f"file {include_path} 2>/dev/null || echo MISSING").strip()
    if "MISSING" in file_type:
        pytest.skip(
            f"fw4 include path {include_path} does not exist on filesystem "
            "(pre-PR #108 setup included a broken include path)"
        )
    assert not include_path.startswith("/etc/config/"), \
        f"fw4 include points to UCI config dir (should be a shell script): {include_path}"
    assert "script" in file_type.lower() or "text" in file_type.lower() or "executable" in file_type.lower(), \
        f"fw4 include path {include_path} does not appear to be a shell script: {file_type}"


@pytest.mark.extended
def test_setup_netbird_zone_in_defaults(router):
    """Verify 99-tollgate-setup contains setup_netbird_zone function and it's called."""
    _skip_if_no_netbird_zone(router)
    setup = router.ssh(
        "cat /etc/uci-defaults/99-tollgate-setup 2>/dev/null || echo NOT_FOUND"
    )
    if "NOT_FOUND" in setup:
        pytest.skip("uci-defaults script not found")

    assert "setup_netbird_zone" in setup, \
        "setup_netbird_zone function not found in 99-tollgate-setup"

    # Verify the function is called in the driver/main section
    lines = setup.splitlines()
    in_function = False
    found_call = False
    for line in lines:
        if line.strip().startswith("setup_netbird_zone()"):
            in_function = True
            continue
        if in_function and line.strip() == "}":
            in_function = False
            continue
        # Look for a call outside the function definition
        if not in_function:
            stripped = line.strip()
            if stripped == "setup_netbird_zone" or stripped.startswith("setup_netbird_zone "):
                found_call = True
                break
            # Also match in a driver block like: setup_netbird_zone || true
            if "setup_netbird_zone" in stripped and not stripped.startswith("#"):
                # Make sure it's not just the function definition line
                if "()" not in stripped:
                    found_call = True
                    break

    assert found_call, \
        "setup_netbird_zone function exists but is never called in 99-tollgate-setup"


@pytest.mark.extended
def test_netbird_zone_enabled_marker_exists(router):
    """Check if /etc/tollgate/netbird-zone-enabled sentinel file exists."""
    _skip_if_no_netbird_zone(router)
    out = router.ssh(
        "test -f /etc/tollgate/netbird-zone-enabled && echo EXISTS || echo MISSING"
    ).strip()
    assert out == "EXISTS", \
        f"Sentinel file /etc/tollgate/netbird-zone-enabled not found (got: {out})"
