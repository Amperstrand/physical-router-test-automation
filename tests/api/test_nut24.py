# NUT-24 (Cashu HTTP Header) — TIP-02 Cashu Payments via HTTP Headers
#
# Tests alternate Cashu token delivery methods: X-Cashu header and JSON POST.
# Gated behind ENABLE_NUT24=1 since the backend doesn't support it yet.

import json
import os
import pytest

pytestmark = [pytest.mark.story("R2"), pytest.mark.tip("TIP-02"),pytest.mark.api, pytest.mark.critical]


def _nut24_enabled():
    return os.environ.get("ENABLE_NUT24", "0") == "1"


@pytest.mark.critical
def test_nut24_header(router, cashu):
    if not _nut24_enabled():
        pytest.skip("Set ENABLE_NUT24=1 in .env when backend supports NUT-24")
    token = cashu.mint(4)
    resp = router.pay_via_header(token)
    assert '"kind":1022' in resp, f"Unexpected response: {resp[:200]}"


@pytest.mark.critical
def test_nut24_post_json(router, cashu):
    if not _nut24_enabled():
        pytest.skip("Set ENABLE_NUT24=1 in .env when backend supports NUT-24")
    token = cashu.mint(4)
    resp = router.backend_curl_xff(
        router.backend_url("/"),
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"token": token}),
    )
    assert '"kind":1022' in resp, f"Unexpected response: {resp[:200]}"
