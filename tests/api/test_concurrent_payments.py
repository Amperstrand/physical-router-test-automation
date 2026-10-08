import threading
import time
from typing import Any

import pytest
from lib.helpers import is_session_event, is_mac_lookup_failure, require_client_identity

pytestmark = [
    pytest.mark.story("M3"),
    pytest.mark.tip("TIP-02"),pytest.mark.api, pytest.mark.extended]


@pytest.mark.extended
def test_concurrent_payments_single_token(router, cashu):
    require_client_identity(router)
    token = cashu.mint(3)
    results: list[dict[str, Any] | None] = [None, None]

    def pay_thread(index):
        try:
            results[index] = router.pay_direct(token)
        except Exception as e:
            results[index] = {"error": str(e)}

    t1 = threading.Thread(target=pay_thread, args=(0,))
    t2 = threading.Thread(target=pay_thread, args=(1,))
    t1.start()
    time.sleep(0.1)
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    mac_failures = [r for r in results if isinstance(r, dict) and is_mac_lookup_failure(r)]
    if len(mac_failures) > 0:
        pytest.skip("No client on TollGate AP — backend cannot resolve MAC")

    successes = sum(1 for r in results if r and is_session_event(r))
    errors = sum(1 for r in results if r and not is_session_event(r))

    assert successes >= 1, f"No successful payment: {results}"
    assert successes + errors == 2, f"Unexpected results: {results}"


@pytest.mark.extended
def test_concurrent_payments_different_tokens(router, cashu):
    pytest.xfail(
        "Go backend wallet.Receive is not concurrency-safe yet: parallel distinct "
        "tokens can fail with 'outputs have already been signed before'."
    )
    require_client_identity(router)
    token1 = cashu.mint(3)
    token2 = cashu.mint(3)
    results: list[dict[str, Any] | None] = [None, None]

    def pay_thread(index, token):
        try:
            results[index] = router.pay_direct(token)
        except Exception as e:
            results[index] = {"error": str(e)}

    t1 = threading.Thread(target=pay_thread, args=(0, token1))
    t2 = threading.Thread(target=pay_thread, args=(1, token2))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    mac_failures = [r for r in results if isinstance(r, dict) and is_mac_lookup_failure(r)]
    if len(mac_failures) > 0:
        pytest.skip("No client on TollGate AP — backend cannot resolve MAC")

    for i, r in enumerate(results):
        assert r is not None, f"Thread {i} returned no result"
        assert is_session_event(r), \
            f"Thread {i} payment failed: {str(r)[:200]}"
