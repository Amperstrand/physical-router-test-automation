"""Hardware-mint payment loop: the M5 Atom serves a fake-wallet Cashu mint
on the router's LAN; PRTA mints a token from the hardware and pays the live
TollGate backend, then proves the proofs were consumed (double-spend).

Requires: an M5 Atom running m5-cashu-mint firmware attached over USB
serial, and the pytest host being a client of the router under test (the
backend attributes the payment to the paying client's MAC).

Verified live 2026-09-30 against router 326D (tollgate-wrt v0.6.0-alpha4,
rust): kind=1022 with allotment granted, second submit kind=21023.
"""

import json
import os
import urllib.error
import urllib.request

import pytest

from lib.constants import TOKEN_DEFAULT

pytestmark = [pytest.mark.api, pytest.mark.critical, pytest.mark.hardware]


def _pay(router_host: str, token: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        f"http://{router_host}:2121/",
        data=token.encode(),
        headers={"Content-Type": "text/plain"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # The backend rejects payments with HTTP 400 + a kind=21023 event body.
        return json.loads(exc.read() or b"{}")


def _tag(event: dict, name: str) -> str | None:
    for tag in event.get("tags", []):
        if tag and tag[0] == name:
            return tag[1] if len(tag) > 1 else ""
    return None


@pytest.fixture
def m5_pinned_mints(router, m5_mint):
    """Add the M5 mint to accepted_mints for one test, restore after.

    Backs up config.json and wallet.db router-side; the wallet cache keeps
    stale mint URLs across config changes on some backends.
    """
    cfg = json.loads(router.ssh("cat /etc/tollgate/config.json"))
    router.ssh("cp /etc/tollgate/config.json /tmp/m5-config-backup.json "
               "&& cp /etc/tollgate/wallet.db /tmp/m5-wallet-backup.db 2>/dev/null")
    mint_url = m5_mint.mint_url
    changed = False
    if not any(m.get("url") == mint_url for m in cfg.get("accepted_mints", [])):
        cfg.setdefault("accepted_mints", []).append({
            "url": mint_url,
            "min_balance": 0,
            "balance_tolerance_percent": 0,
            "payout_interval_seconds": 60,
            "min_payout_amount": 0,
            "price_per_step": 1,
            "price_unit": "sat",
            "purchase_min_steps": 0,
        })
        router.write_remote_json("/etc/tollgate/config.json", cfg)
        router.restart_backend()
        router.wait_for_backend_ad()
        changed = True
    yield mint_url
    if changed:
        router.ssh("cp /tmp/m5-config-backup.json /etc/tollgate/config.json "
                   "&& cp /tmp/m5-wallet-backup.db /etc/tollgate/wallet.db 2>/dev/null")
        router.restart_backend()
        router.ssh("rm -f /tmp/m5-config-backup.json /tmp/m5-wallet-backup.db")


def test_m5_minted_token_pays(router, m5_mint, m5_pinned_mints):
    token = m5_mint.mint(TOKEN_DEFAULT)
    assert token.startswith("cashuA")

    event = _pay(os.environ["TOLLGATE_SSH_HOST"], token)
    assert event.get("kind") == 1022, f"payment rejected: {str(event)[:200]}"
    assert int(_tag(event, "allotment") or 0) > 0
    assert _tag(event, "metric") == "bytes"

    replay = _pay(os.environ["TOLLGATE_SSH_HOST"], token)
    assert replay.get("kind") == 21023, \
        f"double-spend not rejected: {str(replay)[:200]}"
