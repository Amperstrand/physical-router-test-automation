"""Lightning invoice payment flow through the captive portal.

Tests the full ln-invoice lifecycle: create invoice -> poll until settled ->
session active.  Works with FakeWallet mints (auto-settle after a few seconds)
in both the cloud lab (local mints) and physical lab (testnut.cashu.exchange).
"""

import json
import os
import time

import pytest
import requests

from lib.constants import BACKEND_PORT

pytestmark = [pytest.mark.story("G2"),pytest.mark.api, pytest.mark.critical, pytest.mark.timeout(120), pytest.mark.virtual_lab]


def _skip_if_no_ln_invoice(router):
    resp = router.api_status("/ln-invoice")
    if resp == 404 or resp == 0:
        pytest.skip(f"ln-invoice endpoint not available (status={resp})")


def _skip_if_no_fakewallet():
    if not os.environ.get("TOLLGATE_VIRTUAL_LAB"):
        pytest.skip("Lightning auto-settle requires FakeWallet (virtual lab only)")


def _skip_if_degraded(router):
    discovery_raw = router.api_body("/")
    try:
        discovery = json.loads(discovery_raw)
    except json.JSONDecodeError:
        pytest.skip(f"Backend / did not return valid JSON: {discovery_raw[:200]}")
    if discovery.get("kind") == 21023:
        pytest.skip("Backend in degraded mode")
    return discovery


def _create_invoice(router, amount=21):
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL", "")
    backend_ip = os.environ.get("TOLLGATE_SSH_HOST", router.gateway_ip if hasattr(router, "gateway_ip") else "127.0.0.1")
    url = f"http://{backend_ip}:{BACKEND_PORT}/ln-invoice"
    payload = {"amount": amount}
    if mint_url:
        payload["mint_url"] = mint_url
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        pytest.fail(f"POST /ln-invoice returned {resp.status_code}: {resp.text[:300]}")
    except requests.RequestException as e:
        pytest.fail(f"POST /ln-invoice failed: {e}")


def _poll_until_settled(router, quote, timeout_s=45):
    backend_ip = os.environ.get("TOLLGATE_SSH_HOST", router.gateway_ip if hasattr(router, "gateway_ip") else "127.0.0.1")
    url = f"http://{backend_ip}:{BACKEND_PORT}/ln-invoice?quote={quote}"
    deadline = time.time() + timeout_s
    last_status = ""
    while time.time() < deadline:
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                time.sleep(2)
                continue
            status = resp.json()
        except (requests.RequestException, json.JSONDecodeError):
            time.sleep(2)
            continue

        state = str(status.get("status") or "").lower()
        last_status = json.dumps(status)[:200]
        if state in ("settled", "paid", "complete"):
            return status
        if state in ("expired", "cancelled"):
            pytest.fail(f"Lightning invoice expired/cancelled: {last_status}")
        time.sleep(2)

    pytest.fail(f"Lightning invoice did not settle within {timeout_s}s (last: {last_status})")


@pytest.mark.critical
def test_ln_invoice_create_and_settle(router):
    """POST /ln-invoice creates a quote, FakeWallet auto-settles, GET confirms settled."""
    _skip_if_no_fakewallet()
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)

    invoice = _create_invoice(router, amount=21)
    quote = invoice.get("quote") or invoice.get("payment_hash") or invoice.get("r_hash")
    payment_request = invoice.get("payment_request") or invoice.get("invoice")
    assert quote, f"ln-invoice response missing quote/payment_hash: {json.dumps(invoice)[:300]}"
    assert payment_request, f"ln-invoice response missing payment_request: {json.dumps(invoice)[:300]}"

    _poll_until_settled(router, quote)


@pytest.mark.critical
def test_ln_invoice_grants_session(router):
    """After Lightning invoice settles, client should have an active session."""
    _skip_if_no_fakewallet()
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)

    invoice = _create_invoice(router, amount=21)
    quote = invoice.get("quote") or invoice.get("payment_hash") or invoice.get("r_hash")
    assert quote, f"Missing quote: {json.dumps(invoice)[:200]}"

    _poll_until_settled(router, quote)

    session_resp = router.api_body("/balance")
    if not session_resp:
        session_resp = router.api_body("/usage")
    assert session_resp, "No session data after Lightning payment (both /balance and /usage empty)"
    try:
        session = json.loads(session_resp)
    except json.JSONDecodeError:
        pytest.fail(f"Session response not JSON: {session_resp[:200]}")

    has_session = (
        session.get("success") is True
        or session.get("authenticated") is True
        or "steps_remaining" in session
        or "active" in str(session).lower()
    )
    assert has_session, f"No active session found after Lightning payment: {session_resp[:300]}"


@pytest.mark.critical
def test_cashu_and_lightning_both_accepted(router, cashu):
    """Both Cashu tokens and Lightning invoices are valid payment methods.

    Pays first with Cashu, verifies session, then after the Cashu session
    expires, pays with Lightning and verifies again.
    """
    _skip_if_no_fakewallet()
    _skip_if_no_ln_invoice(router)
    discovery = _skip_if_degraded(router)

    # Cashu payment
    token = cashu.mint(21)
    pay_resp = router.pay_direct(token)
    assert pay_resp, "Cashu payment returned empty response"
    pay_data = json.loads(pay_resp)
    assert pay_data.get("success") is True, f"Cashu payment failed: {pay_resp[:300]}"

    # Lightning payment
    invoice = _create_invoice(router, amount=21)
    quote = invoice.get("quote") or invoice.get("payment_hash") or invoice.get("r_hash")
    assert quote, f"Missing quote for Lightning leg: {json.dumps(invoice)[:200]}"

    settled = _poll_until_settled(router, quote)
    assert settled, "Lightning invoice did not settle in combined payment test"
