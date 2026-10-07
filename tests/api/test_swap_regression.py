"""Cashu swap-path regression tests (gonuts version bumps).

Catches the #1 bug pattern in tollgate-module-basic-go: a gonuts (Cashu
library) upgrade silently breaking the wallet / swap. Six of six past
version bumps shipped a regression -- #253, #257, #266, #281, #286, #291.

These tests drive the live backend's Lightning-invoice flow
(``POST``/``GET /ln-invoice``) and ``/balance`` endpoint, which exercise
wallet initialisation, keyset fetch, quote creation/state, and quote
persistence -- the exact surfaces that break when gonuts changes its
keyset encoding, swap-output counter semantics, or mint-quote state
parsing.

All tests hit the real backend at ``$TOLLGATE_SSH_HOST:2121`` over HTTP
via ``requests``; none are mocked. The ``router`` fixture (session-scoped,
from ``tests/conftest.py``) supplies SSH access for the backend restart
in S4.
"""

import json
import os
import time

import pytest
import requests

from lib.constants import BACKEND_PORT

pytestmark = [pytest.mark.story("M4"),pytest.mark.api, pytest.mark.go_only, pytest.mark.critical]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _backend_ip(router):
    """Backend HTTP host (the router's LAN IP, from the SSH host env)."""
    return os.environ.get("TOLLGATE_SSH_HOST", getattr(router, "host", "10.99.99.1"))


def _mint_url():
    """The test mint the backend should fetch keysets / create quotes against."""
    return os.environ.get("TOLLGATE_TEST_MINT_URL", "http://10.99.99.2:8383")


#: Valid NUT-04 mint-quote states (mirrors gonuts ``nut04.State.String()``).
#: The CDK FakeWallet at the test mint auto-settles invoices within a few
#: seconds, so a *queried* quote may legitimately read PAID/ISSUED rather
#: than UNPAID. Any of these proves the wallet state is intact; an
#: absent/garbled state is the corruption signal we are guarding against.
_VALID_STATES = {"UNPAID", "PAID", "ISSUED", "PENDING"}


def _valid_state(value):
    return str(value or "").upper() in _VALID_STATES


def _skip_if_no_ln_invoice(router):
    """Skip when ``/ln-invoice`` is absent (404) or backend unreachable (0)."""
    resp = router.api_status("/ln-invoice")
    if resp == 404 or resp == 0:
        pytest.skip(f"ln-invoice endpoint not available (status={resp})")


def _skip_if_degraded(router):
    """Skip when backend discovery reports degraded mode (NIP kind 21023)."""
    discovery_raw = router.api_body("/")
    try:
        discovery = json.loads(discovery_raw)
    except json.JSONDecodeError:
        pytest.skip(f"Backend / did not return valid JSON: {discovery_raw[:200]}")
    if discovery.get("kind") == 21023:
        pytest.skip("Backend in degraded mode")
    return discovery


def _create_invoice(router, amount=21, retries=3):
    """``POST /ln-invoice`` and return the parsed invoice JSON.

    Retries tolerate transient mint flakiness; fails loudly once exhausted.
    """
    url = f"http://{_backend_ip(router)}:{BACKEND_PORT}/ln-invoice"
    payload = {"amount": amount, "mint_url": _mint_url()}
    last_detail = ""
    for attempt in range(retries):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            last_detail = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.RequestException as exc:
            last_detail = str(exc)
        time.sleep(3)
    pytest.fail(f"POST /ln-invoice failed after {retries} attempts (last: {last_detail})")


def _get_quote_status(router, quote):
    """``GET /ln-invoice?quote=<id>``; return parsed JSON or ``None``."""
    url = f"http://{_backend_ip(router)}:{BACKEND_PORT}/ln-invoice?quote={quote}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return None


def _wait_backend_healthy(router, timeout=45):
    """Poll ``GET /`` until the backend reports HTTP 200 again."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if router.api_status("/") == 200:
            return True
        time.sleep(2)
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.critical
def test_invoice_creation_succeeds(router):
    """S1: ``POST /ln-invoice`` with a valid amount + mint_url creates an unpaid invoice.

    Asserts HTTP 200 and a response carrying ``quote``, ``invoice``
    (bolt11), and ``state="UNPAID"``. This exercises wallet init plus the
    keyset fetch from the mint -- if a gonuts bump breaks keyset parsing
    (#253, #281, #286, #291), invoice creation fails here, before any
    payment is ever attempted.
    """
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)

    invoice = _create_invoice(router, amount=21)

    assert invoice.get("quote"), \
        f"response missing 'quote': {json.dumps(invoice)[:300]}"
    assert invoice.get("invoice"), \
        f"response missing 'invoice' (bolt11 payment request): {json.dumps(invoice)[:300]}"
    state = str(invoice.get("state", "")).upper()
    assert state == "UNPAID", \
        f"expected state=UNPAID for a fresh invoice, got state={invoice.get('state')!r}"


@pytest.mark.critical
def test_sequential_invoices_increment_correctly(router):
    """S2: three sequential invoices each return a unique quote ID.

    Catches swap-output / counter races (#257, #266): if the counter gets
    stuck or collides, the second or third invoice creation fails with a
    duplicate-output error from the mint.
    """
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)

    quotes = []
    for i in range(3):
        invoice = _create_invoice(router, amount=21)
        quote = invoice.get("quote")
        assert quote, f"invoice #{i + 1} missing 'quote': {json.dumps(invoice)[:200]}"
        quotes.append(quote)

    assert len(set(quotes)) == 3, \
        f"quote IDs are not unique ({quotes}); swap-output counter may be stuck"


@pytest.mark.critical
def test_invoice_status_queryable(router):
    """S3: a created invoice is queryable via ``GET /ln-invoice?quote=<id>``.

    Catches wallet state corruption: the quote must round-trip through the
    merchant's quote store and return the same quote ID with a valid state.
    The state is not pinned to UNPAID because the FakeWallet mint
    auto-settles invoices within seconds (see ``_VALID_STATES``).
    """
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)

    invoice = _create_invoice(router, amount=21)
    quote = invoice["quote"]

    status = _get_quote_status(router, quote)
    assert status is not None, (
        f"GET /ln-invoice?quote={quote} did not return 200 "
        f"(MAC resolution for the test host may have failed)"
    )
    assert status.get("quote") == quote, \
        f"GET returned mismatched quote: expected {quote!r}, got {status.get('quote')!r}"
    assert _valid_state(status.get("state")), \
        f"status query returned invalid/corrupt state: {status.get('state')!r}"


@pytest.mark.critical
def test_invoice_after_backend_restart(router):
    """S4: a quote survives a backend restart with its state preserved.

    Catches persistence bugs (#247, #248): the quote must be written to
    ``/etc/tollgate/quotes.json`` on creation, reloaded on boot, and the
    monitor goroutine relaunched so the quote is still queryable
    post-restart. The state value is captured before restart and asserted
    equal afterwards (settled quotes stay settled; unsettled stay
    unsettled), falling back to any valid state if the FakeWallet settled
    during the restart window.
    """
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)

    invoice = _create_invoice(router, amount=21)
    quote = invoice["quote"]

    before = _get_quote_status(router, quote)
    state_before = str((before or {}).get("state", "")).upper() if before else ""

    router.restart_backend(timeout=45)
    assert _wait_backend_healthy(router, timeout=45), \
        "backend did not return HTTP 200 on / within 45s after restart"

    status = _get_quote_status(router, quote)
    assert status is not None, \
        f"quote {quote!r} not queryable after restart (persistence may be broken)"
    assert status.get("quote") == quote, \
        f"post-restart quote mismatch: expected {quote!r}, got {status.get('quote')!r}"
    state_after = str(status.get("state", "")).upper()
    assert _valid_state(state_after), \
        f"post-restart state invalid/corrupt: {status.get('state')!r}"
    if state_before:
        assert state_after == state_before, (
            f"state not preserved across restart: before={state_before!r}, "
            f"after={state_after!r}"
        )


@pytest.mark.critical
def test_wallet_balance_returns(router):
    """S5: ``GET /balance`` returns the session-state schema.

    Asserts HTTP 200 with ``status`` and ``session_active`` fields. This
    catches wallet-initialisation failures that prevent the balance query
    from completing -- a gonuts bump that breaks wallet construction
    surfaces here as a non-200 or a malformed body.
    """
    url = f"http://{_backend_ip(router)}:{BACKEND_PORT}/balance"
    try:
        resp = requests.get(url, timeout=10)
    except requests.RequestException as exc:
        pytest.fail(f"GET /balance failed: {exc}")

    assert resp.status_code == 200, \
        f"expected HTTP 200 from /balance, got {resp.status_code}: {resp.text[:200]}"

    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError):
        pytest.fail(f"/balance did not return valid JSON: {resp.text[:200]}")

    assert "status" in data, f"response missing 'status' field: {resp.text[:200]}"
    assert "session_active" in data, \
        f"response missing 'session_active' field: {resp.text[:200]}"
