"""Lightning quote persistence across tollgate-wrt restarts (PR #248).

Validates that Lightning invoice quotes survive process restarts:
  1. Create a Lightning invoice via POST /ln-invoice
  2. Restart tollgate-wrt
  3. Verify the quote is still queryable via GET /ln-invoice?quote=...
  4. Verify the monitor goroutine was relaunched (check logread for
     "Restored N lightning quote(s) from disk")

Feature gating: these tests check for quotes.json on the router.
On older firmware without PR #248, they skip.
"""

import json
import os
import re
import time

import pytest

from lib.constants import BACKEND_PORT

pytestmark = [pytest.mark.story("O3"),pytest.mark.api, pytest.mark.slow, pytest.mark.timeout(180), pytest.mark.extended]


def _skip_if_no_ln_invoice(router):
    resp = router.api_status("/ln-invoice")
    if resp != 405:
        pytest.skip(f"ln-invoice endpoint not available (status={resp}, expected 405 on GET)")


def _skip_if_degraded(router):
    discovery_raw = router.api_body("/")
    try:
        discovery = json.loads(discovery_raw)
    except json.JSONDecodeError:
        pytest.skip(f"Backend / did not return valid JSON: {discovery_raw[:200]}")
    if discovery.get("kind") == 21023:
        pytest.skip("Backend in degraded mode")


def _skip_if_no_persistence_support(router):
    try:
        out = router.ssh(
            "strings /usr/bin/tollgate-wrt 2>/dev/null | grep -c 'loadLightningQuotesFromDisk'",
            timeout=10,
        )
        if out.strip() == "0":
            pytest.skip("Binary lacks loadLightningQuotesFromDisk (persistence not supported)")
    except Exception:
        pytest.skip("Cannot check persistence support")


def _create_invoice(router, amount=21):
    create_resp = router.ssh(
        f"wget -qO- --timeout=15 --post-data='{{\"amount\": {amount}}}' "
        f"--header='Content-Type: application/json' "
        f"'http://[::1]:{BACKEND_PORT}/ln-invoice'",
        timeout=30,
    )
    assert create_resp, "Empty response from POST /ln-invoice"
    try:
        invoice = json.loads(create_resp)
    except json.JSONDecodeError:
        pytest.fail(f"ln-invoice response not JSON: {create_resp[:300]}")
    return invoice


def _get_quote_status(router, quote):
    status_resp = router.ssh(
        f"wget -qO- --timeout=10 'http://[::1]:{BACKEND_PORT}/ln-invoice?quote={quote}'",
        timeout=15,
    )
    try:
        return json.loads(status_resp)
    except json.JSONDecodeError:
        return None


def _restart_tollgate(router, timeout=60):
    router.ssh("/etc/init.d/tollgate-wrt restart 2>&1", timeout=timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if router.api_status("/") == 200:
            return True
        time.sleep(2)
    pytest.fail(f"tollgate-wrt did not come back within {timeout}s after restart")


def test_ln_quote_survives_restart(router):
    """A Lightning invoice quote must survive a tollgate-wrt restart."""
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)
    _skip_if_no_persistence_support(router)

    invoice = _create_invoice(router, amount=21)
    quote = invoice.get("quote") or invoice.get("payment_hash") or invoice.get("r_hash")
    assert quote, f"Missing quote ID: {json.dumps(invoice)[:300]}"

    status_before = _get_quote_status(router, quote)
    assert status_before is not None, f"Quote {quote} not found before restart"

    _restart_tollgate(router)

    status_after = _get_quote_status(router, quote)
    assert status_after is not None, (
        f"Quote {quote} vanished after restart — persistence not working"
    )


def test_ln_quote_monitor_relaunched_after_restart(router):
    """After restart, persisted quotes must have their monitor goroutines relaunched."""
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)
    _skip_if_no_persistence_support(router)

    invoice = _create_invoice(router, amount=21)
    quote = invoice.get("quote") or invoice.get("payment_hash") or invoice.get("r_hash")
    assert quote, f"Missing quote ID: {json.dumps(invoice)[:300]}"

    _restart_tollgate(router)

    logs = router.ssh("logread | grep -i 'Restored.*lightning quote' | tail -5", timeout=15)
    assert "Restored" in logs and "lightning quote" in logs, (
        f"No 'Restored N lightning quote(s)' log found after restart. Logs: {logs[:300]}"
    )


def test_ln_quote_quotes_json_exists_after_create(router):
    """quotes.json must be written to disk after creating an invoice."""
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)
    _skip_if_no_persistence_support(router)

    _create_invoice(router, amount=21)
    time.sleep(2)

    result = router.ssh("find / -name quotes.json -maxdepth 5 2>/dev/null | head -3", timeout=15)
    assert result.strip(), "quotes.json not found on disk after creating invoice"

    content = router.ssh(f"cat {result.strip().split()[0]}", timeout=10)
    try:
        data = json.loads(content)
        assert len(data) > 0, "quotes.json is empty"
    except json.JSONDecodeError:
        pytest.fail(f"quotes.json is not valid JSON: {content[:200]}")
