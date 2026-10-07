"""Captive-portal Cashu payment E2E via Playwright (Part C).

This is the composition test that ties together the three pieces of WD6:

* **Part A** — ``minted_token`` (lib/cashu_fixture.py) auto-mints a Cashu token
  at setup and tears it down afterwards. No test shells out to a mint binary.
* **Part C** — :func:`lib.portal_payment.pay_cashu_via_portal` drives a real
  Playwright browser (the ``page`` fixture from pytest-playwright) through the
  captive portal: navigate → fill token → submit → wait for the checkmark.
* **Part B** — :func:`lib.session_verify.verify_session` confirms the client
  has an active session *read-only* (backend ``logread`` / ``ndsctl`` /
  ``/balance``), so success is verified without burning another token.

The selector contract matches the proven Node spec
``tests/captive-portal.spec.mjs`` (the "cashu token payment grants access and
shows checkmark" test); this is the pytest + auto-mint-fixture + log-verify
counterpart requested by the user.

When it runs
------------
Needs a reachable router (``TOLLGATE_SSH_HOST``), a reachable Cashu mint, and a
browser-capable test machine whose own IP/MAC is the captive client (container
or virtual-lab mode, or a phone via the ``connected_wifi`` flow). It skips
gracefully when any precondition is absent:

* no router → skip (the ``router`` fixture returns ``None``);
* backend in degraded mode (kind 21023) → skip (cannot pay);
* mint unreachable → skip (the ``minted_token`` fixture handles this).
"""
from __future__ import annotations

import json
import logging
import os

import pytest

from lib.portal_payment import pay_cashu_via_portal
from lib.session_verify import verify_session

log = logging.getLogger("tollgate.captive_portal_cashu_payment")

pytestmark = [pytest.mark.story("G2"), pytest.mark.tip("TIP-02"),pytest.mark.api, pytest.mark.extended, pytest.mark.timeout(120)]

#: Splash page path served by nodogsplash on the gateway port.
_SPLASH_PATH = "/splash.html"


def _portal_url(router) -> str:
    """Captive-portal splash URL the browser should open.

    Honours an explicit ``TOLLGATE_PORTAL_URL`` override; otherwise builds it
    from the router's SSH host and its nodogsplash gateway port (UCI-derived,
    falling back to 2050).
    """
    override = os.environ.get("TOLLGATE_PORTAL_URL", "").strip()
    if override:
        return override
    port = 2050
    get_port = getattr(router, "get_nds_portal_port", None)
    if callable(get_port):
        try:
            port_val = get_port()
            port = int(port_val) if isinstance(port_val, (int, str)) else 2050
        except Exception:  # pragma: no cover - defensive, UCI quirks
            port = 2050
    host = getattr(router, "host", "")
    return f"http://{host}:{port}{_SPLASH_PATH}"


def _merchant_mode_or_skip(router) -> None:
    """Skip unless the backend advertises merchant mode (kind 10021, priced).

    In degraded mode (kind 21023) the portal cannot process payments, so the
    payment flow is not exercisable.
    """
    try:
        body = router.api_body("/")
        data = json.loads(body)
    except Exception as exc:
        pytest.skip(f"backend discovery (/) unavailable: {exc}")
    if data.get("kind") == 21023:
        pytest.skip("backend in degraded mode (kind 21023) — cannot test payment")
    if data.get("kind") != 10021:
        pytest.skip(f"backend not in merchant mode (kind={data.get('kind')})")
    tags = data.get("tags") or []
    has_price = any(isinstance(t, list) and t and t[0] == "price_per_step" for t in tags)
    if not has_price:
        pytest.skip("merchant advertisement has no price_per_step tag — payment UI not active")


def test_cashu_payment_grants_session(minted_token, page, router):
    """Auto-minted token pays via the portal; session verified from logs.

    Steps:
      1. ``minted_token`` (Part A) already minted a token at setup.
      2. Drive the portal with Playwright (Part C) — fill the token, submit,
         wait for the success checkmark, read the granted allotment.
      3. Mark the token consumed so the fixture teardown records it as spent.
      4. Confirm the session read-only (Part B) via backend logs / ndsctl /
         ``/balance`` — no additional token is spent to verify success.
    """
    if router is None:
        pytest.skip("no router available (set TOLLGATE_SSH_HOST) — needs live captive portal")

    _merchant_mode_or_skip(router)

    portal_url = _portal_url(router)
    client_mac = getattr(router, "phone_mac", "") or ""
    client_ip = getattr(router, "phone_ip", "") or ""
    log.info(
        "captive-portal cashu payment: portal=%s client=%s/%s token_amount=%d",
        portal_url, client_ip, client_mac, minted_token.amount,
    )

    # --- Part C: drive the portal with a real browser --------------------- #
    result = pay_cashu_via_portal(page, minted_token.token, portal_url)
    assert result.success, (
        "portal payment did not succeed — "
        f"{result.describe()}. Check the Playwright trace/screenshot."
    )

    # The token has now been spent by the portal; record it for teardown audit.
    minted_token.mark_consumed()

    # --- Part B: verify the session read-only (no token burning) ---------- #
    verification = verify_session(router, mac=client_mac or None, ip=client_ip or None, timeout=30)
    assert verification.any_success, (
        "payment checkmark rendered but no active session found via logs/ndsctl/"
        f"balance — portal_result={result.describe()} verification={verification.summary()}"
    )
    log.info("session verified after portal payment: %s", verification.summary())


def test_portal_url_helper_honours_env_override(monkeypatch):
    """``_portal_url`` returns the explicit override when TOLLGATE_PORTAL_URL is set.

    A cheap unit-style guard inside the scenario module so the override path is
    covered without a router/browser.
    """

    class _StubRouter:
        host = "10.0.0.1"

        def get_nds_portal_port(self):
            return 2050

    monkeypatch.setenv("TOLLGATE_PORTAL_URL", "http://override.example:9999/portal")
    assert _portal_url(_StubRouter()) == "http://override.example:9999/portal"

    monkeypatch.delenv("TOLLGATE_PORTAL_URL", raising=False)
    assert _portal_url(_StubRouter()) == "http://10.0.0.1:2050/splash.html"
