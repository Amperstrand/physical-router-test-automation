# TIP-02: Cashu Payments — Minimum Token Handling

import re
import pytest
from lib.helpers import require_client_identity

pytestmark = [pytest.mark.story("M1"), pytest.mark.tip("TIP-02"),pytest.mark.api, pytest.mark.critical]


@pytest.mark.critical
def test_minimum_token(router, cashu):
    require_client_identity(router)
    token = cashu.mint(1)
    resp = router.pay_via_header(token)

    if '"success":true' in resp:
        m = re.search(r'"remaining["\s:]+(\d+)', resp)
        remaining = int(m.group(1)) if m else 0
        assert remaining > 0, f"Token accepted but no remaining time: {resp[:200]}"
    else:
        assert '"kind"' in resp, \
            f"1-sat token response has neither success nor kind: {resp[:200]}"
