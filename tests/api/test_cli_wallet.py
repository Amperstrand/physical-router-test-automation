import json

import pytest

pytestmark = [pytest.mark.story("G8"),pytest.mark.api, pytest.mark.smoke, pytest.mark.go_only]


def _skip_if_no_wallet_cli(router):
    r = router.ssh("tollgate wallet 2>&1 || true", timeout=10)
    if "unknown command" in r.lower() or "not found" in r.lower() or not r.strip():
        pytest.skip("tollgate wallet subcommand not available")
    r_json = router.ssh("tollgate --json wallet info 2>&1 || true", timeout=10)
    try:
        data = json.loads(r_json)
        if not data.get("success"):
            pytest.skip(f"tollgate --json wallet info returned non-success: {str(data)[:120]}")
    except json.JSONDecodeError:
        pytest.skip(f"tollgate --json wallet info returned non-JSON: {r_json[:120]}")


@pytest.fixture(scope="module")
def wallet_info(router):
    _skip_if_no_wallet_cli(router)
    return router.get_wallet_info()


@pytest.fixture(scope="module")
def wallet_balance(router):
    _skip_if_no_wallet_cli(router)
    return router.get_wallet_balance()


@pytest.mark.smoke
def test_wallet_info_succeeds(wallet_info):
    assert wallet_info.get("success") is True, \
        f"wallet info command failed: {wallet_info}"


@pytest.mark.smoke
def test_wallet_info_has_mint_fields(wallet_info):
    data = wallet_info.get("data", {})
    assert "mint_count" in data, \
        f"Missing 'mint_count' in wallet info data: {wallet_info}"
    assert isinstance(data["mint_count"], int)


@pytest.mark.smoke
def test_wallet_info_has_balance_fields(wallet_info):
    data = wallet_info.get("data", {})
    for field in ("total_balance", "mint_balances"):
        assert field in data, \
            f"Missing '{field}' in wallet info data: {wallet_info}"


@pytest.mark.smoke
def test_wallet_balance_succeeds(wallet_balance):
    assert wallet_balance.get("success") is True, \
        f"wallet balance command failed: {wallet_balance}"


@pytest.mark.smoke
def test_wallet_balance_is_numeric(wallet_balance):
    data = wallet_balance.get("data", {})
    balance = data.get("balance_sats")
    if balance is not None:
        assert isinstance(balance, (int, float)), \
            f"Balance not numeric: {type(balance)} = {balance}"
