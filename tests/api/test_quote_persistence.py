"""Lightning quote persistence across restarts (PR #248).

Validates that Lightning invoice quotes survive tollgate-wrt restarts:
- quotes.json is created when an invoice is requested
- quotes survive a backend restart and are reloaded
- corrupt quotes.json doesn't crash the merchant on boot
- monitor goroutines are relaunched for unpaid quotes after restart

Feature gating: skip_if_no_quote_persistence checks for /etc/tollgate/quotes.json.
On firmware without PR #248, the file doesn't exist and tests skip cleanly.
"""

import json
import os
import time

import pytest
import requests

from lib.constants import BACKEND_PORT
from lib.helpers import skip_if_no_quote_persistence

pytestmark = [pytest.mark.story("O3"),pytest.mark.api, pytest.mark.slow, pytest.mark.go_only, pytest.mark.extended]


def _skip_if_no_ln_invoice(router):
    resp = router.api_status("/ln-invoice")
    if resp == 404 or resp == 0:
        pytest.skip(f"ln-invoice endpoint not available (status={resp})")


def _skip_if_degraded(router):
    discovery_raw = router.api_body("/")
    try:
        discovery = json.loads(discovery_raw)
    except json.JSONDecodeError:
        pytest.skip(f"Backend / did not return valid JSON: {discovery_raw[:200]}")
    if discovery.get("kind") == 21023:
        pytest.skip("Backend in degraded mode")
    return discovery


def _create_invoice(router, amount=21, retries=3):
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL", "http://10.99.99.2:8383")
    backend_ip = os.environ.get("TOLLGATE_SSH_HOST", "10.99.99.1")
    url = f"http://{backend_ip}:{BACKEND_PORT}/ln-invoice"
    payload = {"amount": amount, "mint_url": mint_url}
    for attempt in range(retries):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        time.sleep(5)
    last = resp.status_code if "resp" in dir() else "?"
    pytest.fail(f"POST /ln-invoice failed after {retries} attempts (last status: {last})")


def _get_quote_status(router, quote):
    backend_ip = os.environ.get("TOLLGATE_SSH_HOST", "10.99.99.1")
    url = f"http://{backend_ip}:{BACKEND_PORT}/ln-invoice?quote={quote}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


@pytest.mark.slow
def test_quotes_json_created_on_invoice(router):
    """An invoice request creates /etc/tollgate/quotes.json with the quote entry."""
    skip_if_no_quote_persistence(router)
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)

    router.ssh("rm -f /etc/tollgate/quotes.json")

    invoice = _create_invoice(router)
    quote = invoice.get("quote") or invoice.get("payment_hash") or invoice.get("r_hash")
    payment_request = invoice.get("payment_request") or invoice.get("invoice")
    assert quote, f"ln-invoice response missing quote/payment_hash: {json.dumps(invoice)[:300]}"
    assert payment_request, f"ln-invoice response missing payment_request: {json.dumps(invoice)[:300]}"

    deadline = time.time() + 10
    marker = "MISSING"
    while time.time() < deadline:
        marker = router.ssh("test -f /etc/tollgate/quotes.json && echo EXISTS || echo MISSING").strip()
        if marker == "EXISTS":
            break
        time.sleep(1)
    assert marker == "EXISTS", "quotes.json was not created within 10s of invoice request"

    raw = router.ssh("cat /etc/tollgate/quotes.json")
    data = json.loads(raw)
    assert isinstance(data, dict), f"quotes.json is not a dict: {raw[:300]}"
    assert quote in data, f"quote {quote} not found in quotes.json keys: {list(data.keys())}"

    entry = data[quote]
    bolt11 = entry.get("bolt11")
    assert bolt11 == payment_request, (
        f"quotes.json bolt11 does not match invoice payment_request: "
        f"got {bolt11!r}, expected {payment_request!r}"
    )

    router.ssh("rm -f /etc/tollgate/quotes.json")


@pytest.mark.slow
def test_quotes_survive_restart(router):
    """A persisted quote is reloaded after a backend restart and still queryable (bug #247)."""
    skip_if_no_quote_persistence(router)
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)

    invoice = _create_invoice(router)
    quote = invoice.get("quote") or invoice.get("payment_hash") or invoice.get("r_hash")
    payment_request = invoice.get("payment_request") or invoice.get("invoice")
    assert quote, f"ln-invoice response missing quote/payment_hash: {json.dumps(invoice)[:300]}"

    raw_before = router.ssh("cat /etc/tollgate/quotes.json")
    data_before = json.loads(raw_before)
    assert quote in data_before, f"quote {quote} not in quotes.json before restart: {list(data_before.keys())}"

    router.restart_backend(timeout=45)

    deadline = time.time() + 45
    healthy = False
    while time.time() < deadline:
        if router.api_status("/") == 200:
            healthy = True
            break
        time.sleep(2)
    assert healthy, "Backend did not return HTTP 200 on / within 45s after restart"

    raw_after = router.ssh("cat /etc/tollgate/quotes.json")
    data_after = json.loads(raw_after)
    assert quote in data_after, (
        f"quote {quote} did not survive restart; quotes.json keys: {list(data_after.keys())}"
    )

    status = _get_quote_status(router, quote)
    if status is not None:
        body_str = json.dumps(status).lower()
        assert "not found" not in body_str, (
            f"quote {quote} reported as not found after restart: {json.dumps(status)[:300]}"
        )


@pytest.mark.slow
def test_corrupt_quotes_json_recovery(router):
    """A corrupt quotes.json does not crash or hang the merchant on boot."""
    skip_if_no_quote_persistence(router)
    _skip_if_no_ln_invoice(router)

    router.ssh("echo '{not valid json' > /etc/tollgate/quotes.json")

    router.restart_backend(timeout=45)

    deadline = time.time() + 45
    healthy = False
    while time.time() < deadline:
        if router.api_status("/") == 200:
            healthy = True
            break
        time.sleep(2)
    assert healthy, "Backend did not return HTTP 200 on / within 45s after boot with corrupt quotes.json"

    logs = router.get_tollgate_logs(lines=200)
    assert (
        "failed to load persisted lightning quotes" in logs or "ERROR" in logs
    ), f"Expected error log for corrupt quotes.json, got last 200 lines:\n{logs}"


@pytest.mark.slow
def test_monitor_relaunch_after_restart(router):
    """Monitor goroutines are relaunched for unpaid quotes after a backend restart."""
    skip_if_no_quote_persistence(router)
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)

    invoice = _create_invoice(router)
    quote = invoice.get("quote") or invoice.get("payment_hash") or invoice.get("r_hash")
    assert quote, f"ln-invoice response missing quote/payment_hash: {json.dumps(invoice)[:300]}"

    logs_before = router.get_tollgate_logs(lines=500)
    monitor_count_before = logs_before.count("monitorLightningQuote")

    router.restart_backend(timeout=45)

    deadline = time.time() + 45
    healthy = False
    while time.time() < deadline:
        if router.api_status("/") == 200:
            healthy = True
            break
        time.sleep(2)
    assert healthy, "Backend did not return HTTP 200 on / within 45s after restart"

    time.sleep(5)

    logs_after = router.get_tollgate_logs(lines=500)
    if "Restored" not in logs_after and "relaunched" not in logs_after:
        assert "monitorLightningQuote" in logs_after, (
            "Neither restore/relaunch log nor monitorLightningQuote polling found "
            "in post-restart logs (last 500 lines)"
        )
