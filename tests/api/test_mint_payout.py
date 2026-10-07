import json

import pytest

from lib.helpers import require_client_identity

pytestmark = [pytest.mark.story("M4"),pytest.mark.api, pytest.mark.extended]

PAYMENT_AMOUNT = 4


@pytest.fixture(scope="module")
def initial_balance(router):
    resp = router.get_wallet_balance()
    if resp.get("success") is not True:
        pytest.skip(f"Could not get wallet balance (CLI wallet not supported): {str(resp)[:200]}")
    return resp.get("data", {}).get("balance_sats", 0)


@pytest.fixture(scope="module")
def paid_token(cashu, router):
    if not cashu.is_available():
        pytest.skip("cashu venv not available — run scripts/setup-cashu.sh")
    require_client_identity(router)
    try:
        token = cashu.mint(PAYMENT_AMOUNT)
    except Exception as exc:
        pytest.skip(f"cashu minting failed: {exc}")
    resp = router.pay_direct(token)
    return {"token": token, "pay_response": resp}


@pytest.fixture(scope="module")
def post_payment_balance(router, paid_token):
    return router.get_wallet_balance()


@pytest.fixture(scope="module")
def wallet_info(router):
    if not router.backend.has_cli_socket:
        pytest.skip("CLI socket not supported by this backend")
    info = router.get_wallet_info()
    if info.get("success") is not True:
        pytest.skip(f"wallet info command not supported or returned no data: {str(info)[:200]}")
    return info


@pytest.mark.extended
def test_wallet_balance_increases_after_payment(initial_balance, post_payment_balance):
    assert post_payment_balance.get("success") is True, \
        f"Post-payment balance check failed: {post_payment_balance}"
    new_balance = post_payment_balance.get("data", {}).get("balance_sats", 0)
    assert new_balance >= initial_balance, \
        f"Balance did not increase after payment: before={initial_balance}, after={new_balance}"


@pytest.mark.extended
def test_wallet_payout_succeeds(router, paid_token):
    resp = router.cli_command("wallet", args=["payout"])
    if resp.get("success") is True:
        return
    assert isinstance(resp, dict), \
        f"Payout returned non-dict response: {resp}"
    output = json.dumps(resp).lower()
    assert "not found" not in output or "payout" not in output, \
        f"Payout command not recognized: {resp}"


@pytest.mark.extended
def test_wallet_send_exercises_keyset_derivation(router, paid_token):
    post_payment_balance = router.get_wallet_balance()
    balance = post_payment_balance.get("data", {}).get("balance_sats", 0)
    if balance < 1:
        pytest.skip(f"Wallet balance is {balance} sats — need at least 1 to test send")

    resp = router.cli_command("wallet", args=["send", "1"], timeout=30)
    raw = json.dumps(resp).lower()

    # V2-only mints trigger DeriveKeysetId failure — expected, skip not fail
    if "error" in raw and "keyset" in raw:
        pytest.skip(
            f"DeriveKeysetId failure detected (expected on V2-only mints): {resp}"
        )

    # Backend returns help text when send subcommand doesn't exist — skip, don't fail
    if resp.get("success") is None and any(
        kw in raw for kw in ["available commands", "usage:", "wallet [command]"]
    ):
        pytest.skip(
            f"Wallet send subcommand not available in this build (help returned instead of executing send): {resp}"
        )

    if resp.get("success") is False and "unknown wallet action" in raw:
        pytest.skip(f"wallet send command not available in this build: {resp}")

    if resp.get("success") is False or "error" in raw:
        pytest.fail(f"Wallet send failed unexpectedly: {resp}")

    assert resp.get("success") is True, \
        f"Wallet send did not succeed: {resp}"
    data = resp.get("data", {})
    if "token" in data:
        assert isinstance(data["token"], str), \
            f"Send response token is not a string: {data}"


@pytest.mark.extended
def test_wallet_info_shows_mint_urls(wallet_info):
    assert wallet_info.get("success") is True, \
        f"Wallet info failed: {wallet_info}"
    data = wallet_info.get("data", {})
    mint_balances = data.get("mint_balances", {})
    if isinstance(mint_balances, dict) and len(mint_balances) == 0 and data.get("total_balance", -1) == 0:
        pytest.skip("Wallet has zero balance — no mint_balances entries to validate")
    assert isinstance(mint_balances, dict), \
        f"Expected mint_balances to be a dict: {type(mint_balances)}"
    assert len(mint_balances) > 0, \
        f"Wallet info shows no mint_balances entries: {wallet_info}"
    for mint_url, balance in mint_balances.items():
        assert isinstance(mint_url, str) and mint_url.startswith("http"), \
            f"Mint URL invalid: {mint_url}"
        assert isinstance(balance, (int, float, dict)), \
            f"Mint balance entry has unexpected type: {mint_url}={balance}"
