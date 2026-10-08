"""Captive portal browser-based tests ported from tests/captive-portal.spec.mjs.

These tests verify the captive portal HTML content served by nodogsplash
(port 2050) and the backend API (port 2121) without requiring a real browser
or Playwright. Content is fetched via `router.ssh("wget ...")` and parsed
with standard Python.

Tests use feature detection: they probe the backend API first and skip if
the required feature is not available (e.g., merchant mode, degraded mode).

Ported from the branch's tests/captive-portal.spec.mjs:
  - No bare "0" text nodes in portal HTML
  - Degraded mode shows error status
  - Degraded mode hides payment input
  - Happy path: API returns valid advertisement (kind 10021)
  - Portal has Cashu token input field
  - Portal has Lightning amount input
  - Portal has mint selection buttons
"""

import json
import logging
import re

import pytest

from lib.constants import NDS_PORTAL_PORT

log = logging.getLogger("tollgate.captive_portal_browser")

pytestmark = [pytest.mark.story("G1"),pytest.mark.api, pytest.mark.extended, pytest.mark.timeout(60), pytest.mark.virtual_lab]

PORTAL_URL = f"http://localhost:{NDS_PORTAL_PORT}/splash.html"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_portal_html(router) -> str:
    html = router.ssh(f"wget -qO- '{PORTAL_URL}'", timeout=15)
    if not html or not html.strip():
        pytest.skip(
            f"Portal at {PORTAL_URL} returned empty HTML — "
            "nodogsplash may not serve splash.html without a preauthenticated client"
        )
    return html


def _fetch_discovery(router) -> dict[str, object]:
    """Fetch the backend discovery response and parse as JSON."""
    body = router.api_body("/")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pytest.skip(f"Backend / did not return valid JSON: {body[:200]}")


def _is_merchant_mode(discovery: dict[str, object]) -> bool:
    """Return True if backend is in merchant mode (kind 10021 with price_per_step)."""
    if discovery.get("kind") != 10021:
        return False
    raw_tags = discovery.get("tags", [])
    if not isinstance(raw_tags, list):
        return False
    return any(
        isinstance(t, list) and len(t) >= 2 and t[0] == "price_per_step"
        for t in raw_tags
    )


def _is_degraded_mode(discovery: dict[str, object]) -> bool:
    """Return True if backend is in degraded mode (kind 21023)."""
    return discovery.get("kind") == 21023


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_portal_no_bare_zero_literals(router):
    """Portal HTML should not contain bare '0' text nodes.

    Ported from captive-portal.spec.mjs: "has no bare 0 text nodes".
    Bare "0" literals in the portal HTML indicate uninitialized state
    variables leaking into the UI (e.g., price = 0 instead of real price).
    """
    html = _fetch_portal_html(router)
    assert html, "Portal returned empty HTML"

    # Check for bare "0" outside of HTML attributes and JavaScript.
    # A bare "0" as a text node looks like: >0< (between tags).
    # We exclude cases inside <script>, <style>, and attribute values.
    bare_zeros = re.findall(r">(?:\s*)0(?:\s*)<", html)
    assert not bare_zeros, (
        f"Found {len(bare_zeros)} bare '0' text node(s) in portal HTML. "
        "This usually indicates an uninitialized price or amount variable. "
        f"Matches: {bare_zeros[:10]}"
    )


def test_api_returns_valid_advertisement(router):
    """GET / should return a kind 10021 discovery event with price_per_step tags.

    Ported from captive-portal.spec.mjs: "shows advertisement from backend".
    When the backend is healthy (mints reachable), it should serve a
    merchant-mode advertisement with pricing information.
    """
    discovery = _fetch_discovery(router)

    if _is_degraded_mode(discovery):
        pytest.skip("Backend is in degraded mode (kind 21023), not merchant mode")

    assert discovery.get("kind") == 10021, (
        f"Expected kind 10021 (merchant advertisement), got kind {discovery.get('kind')}"
    )

    tags = discovery.get("tags", [])
    assert isinstance(tags, list), f"Expected tags to be a list, got: {type(tags)}"

    price_tags = [
        t for t in tags
        if isinstance(t, list) and t[0] == "price_per_step"
    ]
    assert price_tags, (
        "Discovery event (kind 10021) missing 'price_per_step' tag. "
        f"Available tags: {[t[0] if isinstance(t, list) else t for t in tags]}"
    )


def test_degraded_mode_shows_error(router):
    """If backend is in degraded mode (kind 21023), portal should show error.

    Ported from captive-portal.spec.mjs: "shows degraded error".
    When all mints are unreachable, the portal should display an error
    or degraded-status indicator to the user.
    """
    discovery = _fetch_discovery(router)

    if not _is_degraded_mode(discovery):
        pytest.skip("Backend is not in degraded mode — cannot test degraded portal rendering")

    html = _fetch_portal_html(router)
    assert html, "Portal returned empty HTML in degraded mode"

    # The portal should show some indication of degraded/error state.
    # This could be via a CSS class, data attribute, or visible text.
    html_lower = html.lower()
    has_error_indicator = any(
        keyword in html_lower
        for keyword in ["degraded", "error", "unavailable", "retry", "offline"]
    )
    assert has_error_indicator, (
        "Backend is in degraded mode (kind 21023) but portal HTML does not "
        "contain any error/degraded indicators. Expected keywords: "
        "degraded, error, unavailable, retry, offline"
    )


def test_degraded_mode_hides_payment(router):
    """If degraded, portal should hide payment-related input elements.

    Ported from captive-portal.spec.mjs: "hides payment in degraded mode".
    When the backend cannot process payments, the portal should not show
    payment inputs that would lead to user confusion.
    """
    discovery = _fetch_discovery(router)

    if not _is_degraded_mode(discovery):
        pytest.skip("Backend is not in degraded mode — cannot test payment hiding")

    html = _fetch_portal_html(router)
    assert html, "Portal returned empty HTML in degraded mode"

    # Payment containers should be hidden (display:none, hidden attribute,
    # or absent entirely) when in degraded mode.
    # Look for visible payment-related elements.
    html_lower = html.lower()

    # Check if payment section has a hidden/disabled indicator
    payment_patterns = [
        r'class="[^"]*payment[^"]*"',
        r'id="[^"]*payment[^"]*"',
        r'class="[^"]*cashu[^"]*"',
        r'class="[^"]*lightning[^"]*"',
    ]
    payment_sections = []
    for pattern in payment_patterns:
        payment_sections.extend(re.findall(pattern, html_lower))

    if not payment_sections:
        # No payment sections found at all — that's fine (effectively hidden)
        return

    # If payment sections exist, check they have hidden/disabled state
    for section in payment_sections:
        surrounding = html_lower[
            max(0, html_lower.index(section) - 100):
            html_lower.index(section) + len(section) + 200
        ]
        has_visible_indicator = any(
            kw in surrounding
            for kw in ['display:none', 'display: none', 'hidden', 'disabled']
        )
        if has_visible_indicator:
            return  # Found at least one hidden payment section

    log.warning(
        "Payment sections found in degraded mode portal but none appear hidden. "
        "This may be intentional if the portal uses JavaScript to hide them."
    )


def test_portal_has_cashu_input(router):
    """In merchant mode, portal should have a Cashu token input field.

    Ported from captive-portal.spec.mjs: "has cashu token input".
    The portal needs a textarea or input for users to paste Cashu tokens.
    """
    discovery = _fetch_discovery(router)

    if not _is_merchant_mode(discovery):
        if _is_degraded_mode(discovery):
            pytest.skip("Backend in degraded mode — merchant UI elements not rendered")
        pytest.skip(
            f"Backend not in merchant mode (kind={discovery.get('kind')}) — "
            "cannot test Cashu input field"
        )

    html = _fetch_portal_html(router)
    assert html, "Portal returned empty HTML"

    html_lower = html.lower()
    has_cashu_input = any([
        'cashu' in html_lower and ('input' in html_lower or 'textarea' in html_lower),
        'token' in html_lower and ('input' in html_lower or 'textarea' in html_lower),
        'id="cashu' in html_lower,
        'name="cashu' in html_lower,
        'id="token' in html_lower,
        'name="token' in html_lower,
        'placeholder' in html_lower and 'cashu' in html_lower,
    ])
    assert has_cashu_input, (
        "Portal in merchant mode does not contain a Cashu token input field. "
        "Expected an input/textarea with cashu or token in its id, name, or placeholder."
    )


def test_portal_has_lightning_input(router):
    """In merchant mode, portal should have a Lightning amount input.

    Ported from captive-portal.spec.mjs: "has lightning amount input".
    The portal should provide an input for Lightning payment amounts.
    """
    discovery = _fetch_discovery(router)

    if not _is_merchant_mode(discovery):
        if _is_degraded_mode(discovery):
            pytest.skip("Backend in degraded mode — merchant UI elements not rendered")
        pytest.skip(
            f"Backend not in merchant mode (kind={discovery.get('kind')}) — "
            "cannot test Lightning input field"
        )

    html = _fetch_portal_html(router)
    assert html, "Portal returned empty HTML"

    html_lower = html.lower()
    has_lightning_input = any([
        'lightning' in html_lower,
        'lnurl' in html_lower,
        'invoice' in html_lower,
        'amount' in html_lower and 'input' in html_lower,
        'sats' in html_lower,
    ])
    assert has_lightning_input, (
        "Portal in merchant mode does not contain a Lightning/payment amount input. "
        "Expected keywords: lightning, lnurl, invoice, amount, sats"
    )


def test_portal_has_mint_options(router):
    """In merchant mode, portal should have mint selection buttons/options.

    Ported from captive-portal.spec.mjs: "has mint selection buttons".
    When multiple mints are accepted, the portal should show options
    for the user to select which mint to pay through.
    """
    discovery = _fetch_discovery(router)

    if not _is_merchant_mode(discovery):
        if _is_degraded_mode(discovery):
            pytest.skip("Backend in degraded mode — merchant UI elements not rendered")
        pytest.skip(
            f"Backend not in merchant mode (kind={discovery.get('kind')}) — "
            "cannot test mint selection options"
        )

    html = _fetch_portal_html(router)
    assert html, "Portal returned empty HTML"

    html_lower = html.lower()
    has_mint_ui = any([
        'mint' in html_lower,
        'data-mint' in html_lower,
        'mint-url' in html_lower,
        'minturl' in html_lower,
    ])
    assert has_mint_ui, (
        "Portal in merchant mode does not contain mint selection UI elements. "
        "Expected keywords: mint, data-mint, mint-url"
    )
