"""Drive the TollGate captive portal with a Playwright page to pay a Cashu token.

This module is the bridge between Part A (the auto-minting ``minted_token``
fixture in :mod:`lib.cashu_fixture`) and Part B (the read-only
:mod:`lib.session_verify`) for Part C: a Playwright captive-portal payment test.

It contains *only* the browser-interaction orchestration, factored out so that:

* the pytest-playwright integration test (``tests/scenarios/
  test_captive_portal_cashu_payment.py``) stays a thin composition of
  ``minted_token`` + ``page`` + ``router`` + :func:`lib.session_verify.verify_session`;
* the selector sequence can be unit-tested with a lightweight ``FakePage``
  (``tests/unit/test_portal_payment.py``) without a browser or router.

The selector contract is a faithful port of the proven Node spec
``tests/captive-portal.spec.mjs`` ("cashu token payment grants access and shows
checkmark"), so the Python port tracks the same DOM contract that already runs
in the lab. The differences from the Node spec are intentional and match the
user requirement:

* the token comes from the auto-minting ``minted_token`` fixture rather than a
  shell-out to the ``mint-token`` binary;
* success is additionally confirmed read-only via :mod:`lib.session_verify`
  (backend logs / ``ndsctl`` / ``/balance``) so no extra tokens are burned to
  "check" the session.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from playwright.sync_api import Page

log = logging.getLogger("tollgate.portal_payment")

# --------------------------------------------------------------------------- #
# Selectors — kept identical to tests/captive-portal.spec.mjs so the Python
# port tracks the same DOM contract as the Node Playwright suite.
# --------------------------------------------------------------------------- #
# The captive-portal UI is now tabbed (Cashu / Lightning). The Cashu token
# input only renders after the Cashu tab is clicked, and it has no stable id —
# it is an ``<input type="text" placeholder="cashuxyz…">`` — so we locate it by
# placeholder substring. The submit button (text "Continue") lives inside
# .tollgate-captive-portal-method-submit and is enabled (``disabled`` attr
# removed) once a valid token is entered.
SEL_CASHU_TAB = ".tollgate-captive-portal-tabs-tab-cashu"
SEL_TOKEN_INPUT = 'input[placeholder*="cashu"]'
SEL_SUBMIT_READY = ".tollgate-captive-portal-method-submit button:not([disabled])"
SEL_SUBMIT_CLICK = ".tollgate-captive-portal-method-submit button:not([disabled])"
# The post-payment success indicator is a leaf element *inside*
# ``.tollgate-captive-portal-access-granted``. New portal bundles
# (tollgate-captive-portal-site#42, net4sats-captive-portal-site#3) mark the
# success-state checkmark with the shared, prefix-free id
# ``captive-portal-access-granted-checkmark`` — present on the success view
# only, so the expired-session view (which reuses the same checkmark class
# with an error icon) can never match. Already-deployed portals predate the
# id, so fall back to the tolerant ``class*=`` match from PRs #114/#115 (on
# old bundles only, that fallback also matches the expired view — a known
# limitation of the compatibility path).
SEL_CHECKMARK = '#captive-portal-access-granted-checkmark, [class*="access-granted-check"]'
SEL_CONTENT = ".tollgate-captive-portal-method-content"

# Allotment text like "500 MB", "2 GB", "1024 MiB", "1.5 GiB", "1,024 KB".
# Matches decimal (KB/MB/GB/TB) and binary (KiB/MiB/GiB/TiB) IEC units so we
# accept whatever the portal renders on either backend.
_ALLOTMENT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(KB|MB|GB|TB|KiB|MiB|GiB|TiB)",
    re.IGNORECASE,
)

_BIN_BYTES = {
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
}


@dataclass
class PortalPaymentResult:
    """Outcome of a single portal Cashu payment.

    Attributes:
        success: True only when the checkmark rendered AND a positive allotment
            was parsed from the post-payment content. The integration test
            additionally asserts :func:`verify_session` succeeds.
        checkmark_visible: Whether the success checkmark element was visible
            after submitting (:data:`SEL_CHECKMARK` — the stable
            ``captive-portal-access-granted-checkmark`` id on new bundles,
            ``class*=`` fallback on already-deployed ones).
        allotment_text: Raw inner text of ``.tollgate-captive-portal-method-content``
            captured after payment (for evidence/diagnostics).
        allotment_bytes: Allotment parsed into bytes, or ``None`` if no unit was
            found in ``allotment_text``.
    """

    success: bool
    checkmark_visible: bool
    allotment_text: str
    allotment_bytes: int | None

    def describe(self) -> str:
        return (
            f"success={self.success} checkmark={self.checkmark_visible} "
            f"allotment={self.allotment_text!r} bytes={self.allotment_bytes}"
        )


def parse_allotment_bytes(text: str) -> int | None:
    """Parse the first ``<amount> <unit>`` allotment in *text* into bytes.

    Returns the byte count (rounded to int) or ``None`` when no recognizable
    unit is present. Accepts both decimal (MB/GB/…) and binary (MiB/GiB/…)
    units. A comma is treated as a *decimal* separator (European locale,
    e.g. "1,5 GB" → 1.5 GB), not a thousands grouping — portal allotment text
    is always a single compact number ("500 MB", "2 GB", "1.5 GiB").

    >>> parse_allotment_bytes("You have 500 MB remaining")
    500000000
    >>> parse_allotment_bytes("Allotment: 2 GiB")
    2147483648
    >>> parse_allotment_bytes("no units here") is None
    True
    """
    if not text:
        return None
    match = _ALLOTMENT_RE.search(text)
    if not match:
        return None
    raw_amount, unit = match.group(1), match.group(2).lower()
    amount = float(raw_amount.replace(",", "."))
    factor = _BIN_BYTES.get(unit)
    if factor is None:  # defensive — regex already restricts the unit set
        return None
    return int(amount * factor)


def pay_cashu_via_portal(
    page: Page | Any,
    token: str,
    portal_url: str,
    *,
    goto_timeout: int = 30_000,
    input_timeout: int = 15_000,
    submit_ready_timeout: int = 10_000,
    checkmark_timeout: int = 35_000,
) -> PortalPaymentResult:
    """Pay a Cashu *token* through the captive portal via a Playwright *page*.

    Drives the portal exactly like the Node spec: navigate to *portal_url*,
    fill the token input, wait for the submit button to enable, click it, then
    wait for the success checkmark and read the granted allotment.

    Playwright timeouts (e.g. a checkmark that never renders) **propagate** so
    pytest-playwright captures a screenshot/trace on failure rather than the
    failure being silently swallowed. Callers that want a soft result should
    catch :class:`playwright.sync_api.TimeoutError` themselves.

    Args:
        page: A ``playwright.sync_api.Page`` (or any duck-typed object exposing
            ``goto``/``wait_for_selector``/``locator``/``click`` — used by the
            unit-test FakePage).
        token: The serialized ``cashuA...``/``cashuB...`` token to submit.
        portal_url: Full URL of the captive-portal splash page
            (e.g. ``http://192.168.41.1:2050/splash.html``).
        goto_timeout / input_timeout / submit_ready_timeout / checkmark_timeout:
            Per-step Playwright timeouts in milliseconds.

    Returns:
        :class:`PortalPaymentResult` with ``success`` reflecting both the
        checkmark and a positive parsed allotment.
    """
    log.info("portal payment: navigating to %s", portal_url)
    page.goto(portal_url, wait_until="networkidle", timeout=goto_timeout)

    # The portal is tabbed — click the Cashu tab so the token input renders.
    log.debug("portal payment: clicking cashu tab %s", SEL_CASHU_TAB)
    page.wait_for_selector(SEL_CASHU_TAB, timeout=input_timeout)
    page.click(SEL_CASHU_TAB)

    log.debug("portal payment: waiting for token input %s", SEL_TOKEN_INPUT)
    page.wait_for_selector(SEL_TOKEN_INPUT, timeout=input_timeout)
    page.locator(SEL_TOKEN_INPUT).fill(token)
    log.info("portal payment: token filled (%d chars)", len(token))

    log.debug("portal payment: waiting for enabled submit %s", SEL_SUBMIT_READY)
    page.wait_for_selector(SEL_SUBMIT_READY, timeout=submit_ready_timeout)
    page.click(SEL_SUBMIT_CLICK)

    log.debug("portal payment: waiting for checkmark %s", SEL_CHECKMARK)
    page.wait_for_selector(SEL_CHECKMARK, timeout=checkmark_timeout)
    checkmark_visible = bool(page.locator(SEL_CHECKMARK).is_visible())

    content_text = page.locator(SEL_CONTENT).inner_text()
    allotment_bytes = parse_allotment_bytes(content_text)

    success = checkmark_visible and allotment_bytes is not None and allotment_bytes > 0
    result = PortalPaymentResult(
        success=success,
        checkmark_visible=checkmark_visible,
        allotment_text=content_text.strip(),
        allotment_bytes=allotment_bytes,
    )
    log.info("portal payment result: %s", result.describe())
    return result
