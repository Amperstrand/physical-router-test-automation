"""Contract coverage for lib/mock_router.py (the api-tier mock).

The recording mock (scripts/record-portal-highlight.mjs) has had its
advertisement schema pinned since the TG003 drift
(tests/api/test_mock_api_advertisement_format.py); lib/mock_router.py — the
mock that makes ``TOLLGATE_MOCK=1`` runs possible without a router — had
drifted from the same contract (5-element ``price_per_step``, no
``min_purchase_steps``). These tests pin the mock's canned ad to the
backend schema (merchant.go:CreateAdvertisement()) and the #18-era
``wait_for_backend_ad`` semantics (require kind=10021, raise on timeout),
so the two mocks cannot diverge again.

The schema assertions run everywhere (they validate a module constant, no
router needed); the served-behavior assertions are gated on mock mode
because only then does the mock HTTP server run.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from lib.mock_router import MOCK_ADVERTISEMENT  # noqa: E402

pytestmark = [pytest.mark.api, pytest.mark.smoke]

SCHEMA_SOURCE = "tollgate-module-basic-go/src/merchant/merchant.go:CreateAdvertisement()"

IS_MOCK_MODE = os.environ.get("TOLLGATE_MOCK", "").lower() in ("1", "true", "yes")


def test_mock_router_advertisement_is_object():
    assert isinstance(MOCK_ADVERTISEMENT, dict)


def test_mock_router_advertisement_kind():
    assert MOCK_ADVERTISEMENT.get("kind") == 10021


def test_mock_router_advertisement_metric_tag():
    tags = MOCK_ADVERTISEMENT.get("tags", [])
    metric_tags = [t for t in tags if isinstance(t, list) and len(t) >= 2 and t[0] == "metric"]
    assert metric_tags, f"missing 'metric' tag: {tags}"
    assert metric_tags[0][1] in ("milliseconds", "bytes")


def test_mock_router_advertisement_step_size_tag():
    tags = MOCK_ADVERTISEMENT.get("tags", [])
    step_tags = [t for t in tags if isinstance(t, list) and len(t) >= 2 and t[0] == "step_size"]
    assert step_tags, f"missing 'step_size' tag: {tags}"
    assert str(step_tags[0][1]).isdigit()


def test_mock_router_advertisement_price_per_step_six_elements():
    """The 6-element form: [price_per_step, cashu, price, unit, url, min_steps]
    — the exact drift this lane fixed (the mock served 5 elements)."""
    tags = MOCK_ADVERTISEMENT.get("tags", [])
    price_tags = [
        t for t in tags
        if isinstance(t, list) and len(t) >= 2 and t[0] == "price_per_step"
    ]
    assert price_tags, f"missing 'price_per_step' tag: {tags}"
    price = price_tags[0]
    assert len(price) == 6, (
        "price_per_step must have 6 elements "
        "(price_per_step, cashu, price, unit, url, min_steps), "
        f"got {len(price)}: {price}"
    )
    assert price[1] == "cashu", f"bearer asset must be 'cashu', got {price[1]!r}"
    assert str(price[2]).isdigit(), f"price must be numeric, got {price[2]!r}"


def test_mock_router_advertisement_min_steps_at_least_one():
    """min_steps in the ad can never be 0 — tmbg #104 normalizes config-side
    (0/absent -> 1) and a purchase of <1 step is meaningless."""
    tags = MOCK_ADVERTISEMENT.get("tags", [])
    price_tags = [
        t for t in tags
        if isinstance(t, list) and len(t) >= 6 and t[0] == "price_per_step"
    ]
    assert price_tags, "no price_per_step tag to check min_steps on"
    for tag in price_tags:
        assert str(tag[5]).isdigit(), f"min_steps must be a digit string, got {tag[5]!r}"
        assert int(tag[5]) >= 1, f"min_steps must be >= 1, got {tag[5]}"


@pytest.mark.skipif(not IS_MOCK_MODE, reason="mock HTTP server only runs in TOLLGATE_MOCK=1")
class TestServedMockBackend:
    def _router(self, request):
        return request.getfixturevalue("router")

    def test_served_advertisement_matches_constant(self, router):
        body = router.api_body("/")
        served = json.loads(body)
        assert served == MOCK_ADVERTISEMENT, (
            "the ad served by the mock backend diverged from "
            "MOCK_ADVERTISEMENT — the contract tests pin the constant, so "
            "serve the constant"
        )

    def test_wait_for_backend_ad_requires_kind_10021(self, router):
        """#18 parity: readiness is the kind=10021 ad, and the mock's
        wait_for_backend_ad validates it (raises on timeout) instead of
        silently passing."""
        router.wait_for_backend_ad(timeout=5.0, interval=0.1)  # must not raise

    def test_wait_for_backend_ad_raises_on_garbage(self, router, monkeypatch):
        def fake_api_body(path):
            return '{"error": "This TollGate is starting up"}'

        monkeypatch.setattr(router, "api_body", fake_api_body)
        with pytest.raises(TimeoutError):
            router.wait_for_backend_ad(timeout=0.4, interval=0.1)
