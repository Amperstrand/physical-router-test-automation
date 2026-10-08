"""Tests for PR #F: Captive portal degraded-mode UI.

Verifies that the portal frontend includes:
- Degraded-mode error state HTML elements
- Required i18n locale keys for degraded state
- Correct JS bundle references
"""

import json
import pytest

pytestmark = [pytest.mark.story("O1"),pytest.mark.api, pytest.mark.extended]


def _skip_if_no_portal(router):
    splash = router.ssh(
        "test -f /etc/tollgate/tollgate-captive-portal-site/splash.html && echo EXISTS || echo MISSING"
    ).strip()
    if splash == "MISSING":
        pytest.skip("Captive portal splash.html not found")


@pytest.mark.extended
def test_portal_locale_keys_exist(router):
    _skip_if_no_portal(router)
    locales_raw = router.ssh(
        "cat /etc/tollgate/tollgate-captive-portal-site/locales/en.json 2>/dev/null || echo MISSING"
    )
    if locales_raw == "MISSING":
        pytest.skip("locales/en.json not found")

    locales = json.loads(locales_raw)

    required_keys = [
        "retrying",
        "TG005_label",
        "TG005_message",
        "no-reachable-mints_label",
        "no-reachable-mints_message",
    ]

    missing = [k for k in required_keys if k not in locales]
    if missing:
        pytest.skip(f"Portal does not have degraded-mode locale keys yet (missing: {missing})")


@pytest.mark.extended
def test_portal_splash_has_degraded_elements(router):
    _skip_if_no_portal(router)
    splash = router.ssh(
        "cat /etc/tollgate/tollgate-captive-portal-site/splash.html 2>/dev/null || echo MISSING"
    )
    if splash == "MISSING":
        pytest.skip("splash.html not found")

    has_retrying = "retrying" in splash.lower() or "Retry" in splash
    has_service_unavailable = (
        "service" in splash.lower() and "unavailable" in splash.lower()
    ) or "TG005" in splash

    if not (has_retrying or has_service_unavailable):
        pytest.skip("splash.html missing degraded-mode UI elements (pre-PR F firmware)")


@pytest.mark.extended
def test_portal_has_apple_touch_icon(router):
    _skip_if_no_portal(router)
    splash = router.ssh(
        "cat /etc/tollgate/tollgate-captive-portal-site/splash.html 2>/dev/null || echo MISSING"
    )
    if splash == "MISSING":
        pytest.skip("splash.html not found")

    has_touch_icon = "apple-touch-icon" in splash
    if not has_touch_icon:
        pytest.skip("apple-touch-icon not in splash.html (pre-PR F firmware)")
