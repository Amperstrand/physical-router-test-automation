"""Invoice-layer double-spend prevention tests (regression for #257).

These tests verify that the tollgate backend's Lightning invoice layer handles
concurrent and repeated requests safely -- the closest local approximation of
Cashu double-spend rejection without a full NDS/DHCP MAC-resolution stack.

Background: #257 is a swap-counter race where the same blinded message can be
reused across swap operations. We cannot exercise the Cashu ``POST /`` path
locally because it requires MAC resolution via NDS/DHCP, which is only
available on a real router with an authenticated client. The invoice layer
(``POST /ln-invoice`` / ``GET /ln-invoice?quote=...``) is MAC-independent and
shares the same quote/swap state machine, making it the best available proxy.

Token creation reference (for tests that need a real Cashu token)::

    from lib.cashu_fixture import create_minter
    minter = create_minter('http://10.99.99.2:8383')
    token = minter.mint(4)

Environment: OpenWrt VM at 10.99.99.1:2121, CDK mint at
http://10.99.99.2:8383. Run with ``--backend go``.
"""

from __future__ import annotations

import json
import os
import time

import pytest
import requests

from lib.constants import BACKEND_PORT

pytestmark = [
    pytest.mark.story("M2"),
    pytest.mark.tip("TIP-02"),pytest.mark.api, pytest.mark.go_only, pytest.mark.extended]


# --- helpers ---------------------------------------------------------------

def _skip_if_no_ln_invoice(router):
    """Skip if the Go backend does not expose /ln-invoice."""
    resp = router.api_status("/ln-invoice")
    if resp in (404, 0):
        pytest.skip(f"ln-invoice endpoint not available (status={resp})")


def _skip_if_degraded(router):
    """Skip if the backend is in degraded mode (kind 21023)."""
    body = router.api_body("/")
    try:
        discovery = json.loads(body)
    except json.JSONDecodeError:
        pytest.skip(f"Backend / did not return valid JSON: {body[:200]}")
    if discovery.get("kind") == 21023:
        pytest.skip("Backend in degraded mode -- invoice layer unavailable")


def _backend_base(router) -> str:
    host = os.environ.get("TOLLGATE_SSH_HOST", router.host)
    return f"http://{host}:{BACKEND_PORT}"


def _create_invoice(router, amount=21) -> dict:
    """POST /ln-invoice and return the parsed invoice dict."""
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL", "http://10.99.99.2:8383")
    url = f"{_backend_base(router)}/ln-invoice"
    payload = {"amount": amount, "mint_url": mint_url}
    resp = requests.post(url, json=payload, timeout=15)
    assert resp.status_code == 200, (
        f"POST /ln-invoice returned {resp.status_code}: {resp.text[:300]}"
    )
    return resp.json()


def _get_quote_status(router, quote) -> dict | None:
    """GET /ln-invoice?quote=... and return the parsed status dict (or None)."""
    url = f"{_backend_base(router)}/ln-invoice?quote={quote}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return None


def _extract_quote(invoice: dict) -> str | None:
    """Extract the quote ID from an invoice response (handles field name variants)."""
    return invoice.get("quote") or invoice.get("payment_hash") or invoice.get("r_hash")


# Terminal states (string form) -- once reached, status must never regress.
_TERMINAL = {"settled", "paid", "complete"}


# --- tests -----------------------------------------------------------------

def test_same_invoice_queried_twice_returns_same_state(router):
    """Querying the same invoice quote twice must return identical state.

    A swap-counter race (#257) could cause the quote's state to mutate between
    reads if the same blinded message is reused concurrently. Two sequential
    GETs without any intervening payment must produce the same status.
    """
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)

    invoice = _create_invoice(router, amount=21)
    quote = _extract_quote(invoice)
    assert quote, (
        f"ln-invoice response missing quote ID: {json.dumps(invoice)[:300]}"
    )

    first = _get_quote_status(router, quote)
    assert first is not None, f"Quote {quote} not found on first query"

    second = _get_quote_status(router, quote)
    assert second is not None, f"Quote {quote} not found on second query"

    assert first.get("status") == second.get("status"), (
        f"Quote state changed between reads without a payment: "
        f"first={first.get('status')!r}, second={second.get('status')!r} "
        f"(possible swap-counter race, #257)"
    )


def test_duplicate_invoice_request_returns_distinct_quote(router):
    """Two POST /ln-invoice with the same amount must yield distinct quote IDs.

    If the swap counter reuses a quote ID, the same blinded message could be
    spent twice. Each invoice creation must allocate a fresh, unique quote.
    """
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)

    first = _create_invoice(router, amount=21)
    second = _create_invoice(router, amount=21)

    q1 = _extract_quote(first)
    q2 = _extract_quote(second)
    assert q1, f"First invoice missing quote ID: {json.dumps(first)[:200]}"
    assert q2, f"Second invoice missing quote ID: {json.dumps(second)[:200]}"

    assert q1 != q2, (
        f"Duplicate quote ID {q1!r} returned for two invoice requests -- "
        "swap counter may be reusing quote IDs (double-spend risk, #257)"
    )


def test_invoice_expiry_state_transitions(router):
    """Invoice state must not change unexpectedly within a short window.

    Creates an invoice, records its state, waits 5 seconds, then re-reads.
    The state must either remain stable or progress monotonically (e.g.
    pending -> settled). It must never regress from a terminal state, which
    would indicate a race in the state machine.
    """
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)

    invoice = _create_invoice(router, amount=21)
    quote = _extract_quote(invoice)
    assert quote, (
        f"ln-invoice response missing quote ID: {json.dumps(invoice)[:300]}"
    )

    state_before = _get_quote_status(router, quote)
    assert state_before is not None, f"Quote {quote} not found before wait"

    time.sleep(5)

    state_after = _get_quote_status(router, quote)
    assert state_after is not None, f"Quote {quote} not found after 5s wait"

    raw_before = state_before.get("status")
    raw_after = state_after.get("status")

    assert raw_before is not None, (
        f"Invoice status response missing 'status' field: {json.dumps(state_before)[:200]}"
    )
    assert raw_after is not None, (
        f"Invoice status response missing 'status' after 5s: {json.dumps(state_after)[:200]}"
    )

    # Numeric statuses (LUD-18 codes: 0=unknown, 1=unpaid, 2=paid):
    # status must not regress (higher value = more progressed).
    if isinstance(raw_before, (int, float)) and isinstance(raw_after, (int, float)):
        assert raw_after >= raw_before, (
            f"Invoice status regressed from {raw_before} to {raw_after} "
            "within 5s (state machine race, #257)"
        )
    else:
        # String statuses: terminal states must never regress.
        sb = str(raw_before).lower()
        sa = str(raw_after).lower()
        if sb in _TERMINAL:
            assert sa == sb, (
                f"Invoice was {sb!r} then became {sa!r} after 5s -- "
                "terminal state regressed (state machine race, #257)"
            )
