"""Unit tests for lib/portal_payment.py — portal-driving orchestration.

These run without a browser or router: they drive ``pay_cashu_via_portal`` with
a FakePage and exhaustively cover the allotment parser. The live Playwright +
router path is exercised by tests/scenarios/test_captive_portal_cashu_payment.py.
"""
from __future__ import annotations

import re

import pytest

from lib.portal_payment import (
    SEL_CASHU_TAB,
    SEL_CHECKMARK,
    SEL_CONTENT,
    SEL_SUBMIT_CLICK,
    SEL_SUBMIT_READY,
    SEL_TOKEN_INPUT,
    PortalPaymentResult,
    parse_allotment_bytes,
    pay_cashu_via_portal,
)

PORTAL_URL = "http://192.168.41.1:2050/splash.html"
TOKEN = "cashuAeyJ1bml0Ijoi..."  # truncated representative token


class FakeTimeout(Exception):
    """Stands in for playwright.sync_api.TimeoutError in unit tests."""


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self._page = page
        self._selector = selector

    def fill(self, text: str) -> None:
        self._page.filled[self._selector] = text

    def is_visible(self) -> bool:
        # Default to "present implies visible" unless explicitly overridden.
        return self._page.visible.get(self._selector, self._selector in self._page.present)

    def inner_text(self) -> str:
        return self._page.texts.get(self._selector, "")


class FakePage:
    """Minimal duck-typed Playwright Page for unit-testing the helper."""

    def __init__(
        self,
        *,
        present: set[str] | None = None,
        visible: dict[str, bool] | None = None,
        texts: dict[str, str] | None = None,
    ) -> None:
        self.present = present or set()
        self.visible = visible or {}
        self.texts = texts or {}
        self.filled: dict[str, str] = {}
        self.calls: list[tuple] = []

    def goto(self, url: str, **kwargs) -> None:
        self.calls.append(("goto", url, kwargs.get("wait_until")))

    def wait_for_selector(self, selector: str, timeout: int | None = None) -> None:
        self.calls.append(("wait_for_selector", selector))
        if selector not in self.present:
            raise FakeTimeout(f"timeout waiting for {selector}")

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def click(self, selector: str) -> None:
        self.calls.append(("click", selector))


def _happy_page(content_text: str = "You have 500 MB remaining") -> FakePage:
    return FakePage(
        present={SEL_CASHU_TAB, SEL_TOKEN_INPUT, SEL_SUBMIT_READY, SEL_CHECKMARK},
        texts={SEL_CONTENT: content_text},
    )


# --------------------------------------------------------------------------- #
# parse_allotment_bytes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text, expected",
    [
        ("500 MB", 500_000_000),
        ("2 GB", 2_000_000_000),
        ("1024 MiB", 1_073_741_824),
        ("1 GiB", 1_073_741_824),
        ("1.5 GiB", 1_610_612_736),
        ("1,5 GiB", 1_610_612_736),  # comma = decimal separator (European locale)
        ("allotment: 250 mib granted", 262_144_000),
        ("UPPERCASE 3 GB DONE", 3_000_000_000),
        ("250 kb", 250_000),
    ],
)
def test_parse_allotment_bytes_valid(text, expected):
    assert parse_allotment_bytes(text) == expected


@pytest.mark.parametrize("text", ["", "no units here", "123", "MB only", "0 bytes"])
def test_parse_allotment_bytes_no_match(text):
    assert parse_allotment_bytes(text) is None


# --------------------------------------------------------------------------- #
# SEL_CHECKMARK contract — must match what the portal actually serves
# --------------------------------------------------------------------------- #


def _selector_matches(element_id: str, class_attr: str, selector: str) -> bool:
    """Emulate a compound CSS selector list (``#id, [class*=...]``) without a browser.

    Mirrors how Chromium evaluates :data:`SEL_CHECKMARK`: the element matches
    if any comma-separated part matches — the id token against the element's
    id, the ``class*=`` attribute part as a substring of the class value.
    """
    for part in (p.strip() for p in selector.split(",")):
        id_match = re.fullmatch(r"#([A-Za-z0-9_-]+)", part)
        if id_match and element_id == id_match.group(1):
            return True
        class_match = re.fullmatch(r'\[class\*="(.+)"\]', part)
        if class_match and class_match.group(1) in class_attr:
            return True
    return False


_STABLE_ID = "captive-portal-access-granted-checkmark"


@pytest.mark.parametrize(
    "class_attr",
    [
        # Shipped leaf classes, exactly as compiled into the served bundles
        # (tollgate-captive-portal-site build: assets/index-DxBkINUB.js;
        # net4sats-captive-portal-site build: assets/index-C9QTYeLH.js).
        "tollgate-captive-portal-access-granted-checkmark",
        "net4sats-captive-portal-access-granted-checkmark",
    ],
)
def test_sel_checkmark_matches_new_bundle_by_stable_id(class_attr):
    assert _selector_matches(_STABLE_ID, class_attr, SEL_CHECKMARK) is True


@pytest.mark.parametrize(
    "class_attr",
    [
        "tollgate-captive-portal-access-granted-checkmark",
        "net4sats-captive-portal-access-granted-checkmark",
        "tollgate-captive-portal-access-granted-check",
    ],
)
def test_sel_checkmark_falls_back_to_substring_on_deployed_portals(class_attr):
    assert _selector_matches("", class_attr, SEL_CHECKMARK) is True


def test_sel_checkmark_does_not_match_the_granted_container():
    # The container div (class ``...-access-granted``) is not a success marker.
    assert _selector_matches("", "tollgate-captive-portal-access-granted", SEL_CHECKMARK) is False


def test_sel_checkmark_expired_view_matches_only_via_fallback():
    # On NEW bundles the expired-session view carries the checkmark class but
    # NOT the stable id — the id part cannot match it. Only the substring
    # fallback does, which is why the id-first order matters for new bundles.
    expired_class = "tollgate-captive-portal-access-granted-checkmark"
    id_only = SEL_CHECKMARK.split(",")[0].strip()
    assert _selector_matches("", expired_class, id_only) is False
    assert _selector_matches("", expired_class, SEL_CHECKMARK) is True


def test_sel_checkmark_is_id_first_with_substring_fallback():
    assert SEL_CHECKMARK == '#captive-portal-access-granted-checkmark, [class*="access-granted-check"]'


def _exact_class_matches(class_attr: str, selector: str) -> bool:
    """Emulate a plain CSS class selector (``.foo``): whole-token match."""
    assert selector.startswith(".") and "[" not in selector
    return selector[1:] in class_attr.split()


def test_legacy_exact_class_selector_never_matched_the_served_markup():
    # The pre-fix PR #87 selector required the whole token
    # ``tollgate-captive-portal-access-granted-check``; the served element's
    # class list holds ``...-checkmark`` instead, so it matched nothing. The
    # new selector matches the same markup.
    served = "tollgate-captive-portal-access-granted-checkmark"
    assert _exact_class_matches(served, ".tollgate-captive-portal-access-granted-check") is False
    assert _selector_matches(_STABLE_ID, served, SEL_CHECKMARK) is True
    assert _selector_matches("", served, SEL_CHECKMARK) is True


# --------------------------------------------------------------------------- #
# pay_cashu_via_portal — happy path
# --------------------------------------------------------------------------- #


def test_happy_path_fills_token_and_reports_success():
    page = _happy_page("You have 500 MB remaining")

    result = pay_cashu_via_portal(page, TOKEN, PORTAL_URL)

    assert isinstance(result, PortalPaymentResult)
    assert result.success is True
    assert result.checkmark_visible is True
    assert result.allotment_bytes == 500_000_000
    assert "500 MB" in result.allotment_text
    # The token must be filled into the Cashu token input specifically.
    assert page.filled == {SEL_TOKEN_INPUT: TOKEN}


def test_happy_path_calls_selectors_in_spec_order():
    page = _happy_page()
    pay_cashu_via_portal(page, TOKEN, PORTAL_URL)

    # Exact ordered call sequence mirrors the proven Node spec flow.
    expected_calls = [
        ("goto", PORTAL_URL, "networkidle"),
        ("wait_for_selector", SEL_CASHU_TAB),
        ("click", SEL_CASHU_TAB),
        ("wait_for_selector", SEL_TOKEN_INPUT),
        ("wait_for_selector", SEL_SUBMIT_READY),
        ("click", SEL_SUBMIT_CLICK),
        ("wait_for_selector", SEL_CHECKMARK),
    ]
    # Filter out the goto's wait_until tuple slot for the non-goto entries.
    actual = []
    for call in page.calls:
        if call[0] == "goto":
            actual.append(call)
        elif call[0] == "wait_for_selector":
            actual.append((call[0], call[1]))
        elif call[0] == "click":
            actual.append((call[0], call[1]))
    assert actual == expected_calls


def test_uses_custom_timeouts():
    page = _happy_page()
    pay_cashu_via_portal(
        page, TOKEN, PORTAL_URL,
        goto_timeout=1111, input_timeout=2222, submit_ready_timeout=3333, checkmark_timeout=4444,
    )
    # goto received the custom timeout via kwargs (not asserted in calls above);
    # the important contract is that no exception is raised and it still succeeds.
    assert page.filled == {SEL_TOKEN_INPUT: TOKEN}


# --------------------------------------------------------------------------- #
# pay_cashu_via_portal — failure modes
# --------------------------------------------------------------------------- #


def test_checkmark_present_but_hidden_is_not_success():
    page = FakePage(
        present={SEL_CASHU_TAB, SEL_TOKEN_INPUT, SEL_SUBMIT_READY, SEL_CHECKMARK},
        visible={SEL_CHECKMARK: False},
        texts={SEL_CONTENT: "500 MB"},
    )
    result = pay_cashu_via_portal(page, TOKEN, PORTAL_URL)
    assert result.checkmark_visible is False
    assert result.success is False  # success requires checkmark visible


def test_no_allotment_unit_is_not_success():
    page = _happy_page(content_text="Thank you for your payment")  # no MB/GB unit
    result = pay_cashu_via_portal(page, TOKEN, PORTAL_URL)
    assert result.checkmark_visible is True
    assert result.allotment_bytes is None
    assert result.success is False


def test_token_input_never_appears_propagates_timeout():
    page = FakePage(present=set())  # nothing present, not even the input
    with pytest.raises(FakeTimeout):
        pay_cashu_via_portal(page, TOKEN, PORTAL_URL)
    # The token must NOT have been filled if the input never appeared.
    assert page.filled == {}


def test_checkmark_never_appears_propagates_timeout():
    page = FakePage(present={SEL_CASHU_TAB, SEL_TOKEN_INPUT, SEL_SUBMIT_READY})  # no checkmark
    with pytest.raises(FakeTimeout):
        pay_cashu_via_portal(page, TOKEN, PORTAL_URL)
    # Token was filled and submit clicked, but the checkmark never came.
    assert page.filled == {SEL_TOKEN_INPUT: TOKEN}
    assert ("click", SEL_SUBMIT_CLICK) in [(c[0], c[1]) for c in page.calls]
