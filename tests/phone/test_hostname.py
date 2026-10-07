"""Phone-tier hostname resolution and captive portal tests.

Verifies user-facing behaviour: captive portal opens over HTTP and DNS
resolves the TollGate hostname. Feature-detected via hostname check.
"""

import pytest

pytestmark = [pytest.mark.story("G1"),pytest.mark.phone, pytest.mark.slow, pytest.mark.timeout(120),
              pytest.mark.critical, pytest.mark.requires_wifi]


def _skip_if_no_hostname_setup(router):
    hostname = router.ssh("uci get system.@system[0].hostname 2>/dev/null").strip()
    if hostname.lower() != "tollgate":
        pytest.skip("Hostname setup not present (hostname is not 'TollGate')")


def test_captive_portal_opens_on_phone(router, adb, connected_wifi, screenshot_portal):
    _skip_if_no_hostname_setup(router)

    screenshot_portal("hostname-portal.png")

    xml = adb.ui_xml()
    found = any(keyword in xml.lower() for keyword in
                ["tollgate", "portal_ready", "token_typing", "data-sm="])
    assert found, \
        f"Captive portal did not load on phone after WiFi connect. UI text: {xml[:300]}"


def test_phone_can_resolve_tollgate_hostname(router, adb, connected_wifi):
    _skip_if_no_hostname_setup(router)

    result = adb.shell("ping -c 1 -W 3 tollgate.lan 2>&1", timeout=15)
    failed = any(s in result.lower() for s in
                 ["unknown host", "cannot resolve", "not found",
                  "no address", "bad address", "name or service not known"])
    assert not failed, \
        f"Phone cannot resolve 'tollgate.lan' via DNS. ping output: {result[:300]}"
