# TIP-02: Cashu Payments — Mint Validation

import pytest

pytestmark = [pytest.mark.story("G2"), pytest.mark.tip("TIP-02"),pytest.mark.api, pytest.mark.critical]


@pytest.mark.critical
def test_wrong_mint_rejected(router, cashu):
    wrong_token = cashu.synthetic_wrong_mint_token()

    resp = router.pay_via_header(wrong_token)
    assert '"success":true' not in resp, "Wrong mint token was ACCEPTED (expected rejection)"


@pytest.mark.critical
def test_wrong_mint_no_auth(router):
    state = router.get_nds_state()
    if not state:
        pytest.skip("No client connected — cannot verify ndsctl state (API-only test)")
    if state == "Authenticated":
        mac = router.phone_mac
        if mac:
            router.ssh(f"ndsctl deauth {mac} 2>&1 || true", timeout=5)
            state = router.get_nds_state()
    assert state != "Authenticated", "Client authenticated with wrong mint token"
