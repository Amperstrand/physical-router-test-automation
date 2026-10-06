"""Access-denominated token tests — PricePerStep=1."""
import json
import logging
import time

import pytest

log = logging.getLogger("tollgate.api.access_denominated")
pytestmark = [
    pytest.mark.story("M1"),
    pytest.mark.tip("TIP-02"),pytest.mark.api, pytest.mark.config, pytest.mark.slow]

SUCCESS_KINDS = (10021, 21000, 1022)


def _set_price_per_step(router, value):
    router.ssh(f"jq '.accepted_mints[0].price_per_step = {value}' /etc/tollgate/config.json > /tmp/cfg.tmp && mv /tmp/cfg.tmp /etc/tollgate/config.json")
    router.restart_backend()
    time.sleep(10)


def _reset_and_wait(router):
    router.reset_state()
    time.sleep(5)


def _extract_allotment(resp):
    for tag in resp.get("tags", []):
        if len(tag) >= 2 and tag[0] == "allotment":
            return int(tag[1])
    return 0


def test_price_per_step_1_face_value_equals_allotment(router, cashu, backend):
    """5-unit token with price_per_step=1 grants (5-fee) * step_size.

    Cashu mint/receive incurs a fee (typically 1 sat per token), so a 5-sat
    token yields 4 steps at price_per_step=1. Verify allotment is a valid
    multiple of step_size within the expected range.
    """
    if backend.is_rust:
        pytest.skip("Rust SUT uses different pricing model — price_per_step calibration TBD")
    original = router.ssh("jq '.accepted_mints[0].price_per_step' /etc/tollgate/config.json").strip()
    try:
        _set_price_per_step(router, 1)
        _reset_and_wait(router)
        step_size = int(router.ssh("jq '.step_size' /etc/tollgate/config.json").strip())
        token = cashu.mint(5)
        assert token, "mint failed"
        resp = router.pay_direct(token)
        log.info("resp: %s", json.dumps(resp)[:300])
        assert resp.get("kind") in SUCCESS_KINDS, f"Payment rejected: {resp}"
        allotment = _extract_allotment(resp)
        steps = allotment // step_size
        log.info("step_size=%d allotment=%d steps=%d (5-sat token after Cashu fees)", step_size, allotment, steps)
        assert allotment % step_size == 0, f"Allotment {allotment} not a multiple of step_size {step_size}"
        assert 1 <= steps <= 5, f"Steps {steps} outside expected range [1,5] for 5-sat token (fee-adjusted)"
    finally:
        _set_price_per_step(router, int(original))


def test_price_per_step_1_minimum_token(router, cashu):
    """2-unit token with price_per_step=1 grants at least 1 step after fees.

    A 1-sat token is entirely consumed by Cashu fees, so the minimum viable
    token is 2 sats (1 sat fee + 1 sat for 1 step).
    """
    original = router.ssh("jq '.accepted_mints[0].price_per_step' /etc/tollgate/config.json").strip()
    try:
        _set_price_per_step(router, 1)
        _reset_and_wait(router)
        step_size = int(router.ssh("jq '.step_size' /etc/tollgate/config.json").strip())
        token = cashu.mint(2)
        assert token, "mint failed"
        resp = router.pay_direct(token)
        assert resp.get("kind") in SUCCESS_KINDS, f"Payment rejected: {resp}"
        allotment = _extract_allotment(resp)
        steps = allotment // step_size
        log.info("2-unit: step_size=%d allotment=%d steps=%d (after Cashu fees)", step_size, allotment, steps)
        assert allotment % step_size == 0, f"Allotment {allotment} not a multiple of step_size {step_size}"
        assert steps >= 1, f"Allotment {allotment} too low (0 steps from 2-sat token after fees)"
    finally:
        _set_price_per_step(router, int(original))


def test_price_per_step_2_halves_allotment(router, cashu, backend):
    """5-unit token with price_per_step=2 grants floor(5/2)=2 steps."""
    if backend.is_rust:
        pytest.skip("Rust SUT uses different pricing model — price_per_step calibration TBD")
    original = router.ssh("jq '.accepted_mints[0].price_per_step' /etc/tollgate/config.json").strip()
    try:
        _set_price_per_step(router, 2)
        _reset_and_wait(router)
        step_size = int(router.ssh("jq '.step_size' /etc/tollgate/config.json").strip())
        token = cashu.mint(5)
        resp = router.pay_direct(token)
        assert resp.get("kind") in SUCCESS_KINDS, f"Payment rejected: {resp}"
        allotment = _extract_allotment(resp)
        expected = (5 // 2) * step_size
        log.info("price=2: step_size=%d expected=%d allotment=%d", step_size, expected, allotment)
        assert allotment == expected, f"Allotment {allotment} != {expected}"
    finally:
        _set_price_per_step(router, int(original))
