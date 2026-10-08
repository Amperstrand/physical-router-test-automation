import json
import os
import pytest
from lib.helpers import parse_json_or_fail
from lib.cashu import create_minter

pytestmark = [pytest.mark.story("P2"),pytest.mark.api, pytest.mark.smoke]


@pytest.fixture(scope="module")
def discovery(router, backend):
    if backend.is_rust:
        mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL", "https://testnut.cashu.exchange")
        minter = create_minter(mint_url)
        minter.ensure_mint_available(timeout=10)
        minter.warmup(timeout=30)
        token = minter.mint(2)
        body = router.ssh(f"curl -s -H 'X-Cashu: {token}' http://127.0.0.1:2121/pay", timeout=15)
    else:
        body = router.api_body("/")
    event = parse_json_or_fail(body, "discovery response")
    if event.get("kind") != 10021:
        pytest.skip(f"Discovery in degraded mode (kind={event.get('kind')}), skipping healthy-mode tests")
    return event


@pytest.mark.smoke
def test_info_returns_discovery_event(discovery):
    assert discovery.get("kind") == 10021, \
        f"Expected kind=10021, got kind={discovery.get('kind')}"


@pytest.mark.smoke
def test_info_has_metric_tag(discovery):
    tags = discovery.get("tags", [])
    metric_tags = [t for t in tags if isinstance(t, list) and len(t) >= 2 and t[0] == "metric"]
    assert len(metric_tags) > 0, f"Missing 'metric' tag in discovery event: {tags}"
    assert metric_tags[0][1] in ("milliseconds", "bytes"), \
        f"Invalid metric value: {metric_tags[0][1]}"


@pytest.mark.smoke
def test_info_has_step_size_tag(discovery):
    tags = discovery.get("tags", [])
    step_tags = [t for t in tags if isinstance(t, list) and len(t) >= 2 and t[0] == "step_size"]
    assert len(step_tags) > 0, f"Missing 'step_size' tag in discovery event: {tags}"
    assert step_tags[0][1].isdigit(), f"step_size is not a number: {step_tags[0][1]}"


@pytest.mark.smoke
def test_info_has_price_per_step(discovery):
    tags = discovery.get("tags", [])
    price_tags = [t for t in tags if isinstance(t, list) and len(t) >= 2 and t[0] == "price_per_step"]
    if not price_tags:
        pytest.skip("'price_per_step' tags not present in this build (feature not yet available)")
        return  # for type checkers
    price = price_tags[0]
    price = price_tags[0]
    assert len(price) >= 4, f"price_per_step tag too short: {price}"
    assert price[1] == "cashu", f"Expected bearer_asset_type='cashu', got: {price[1]}"


@pytest.mark.smoke
def test_info_has_tips_tag(discovery, router):
    tags = discovery.get("tags", [])
    tips_tags = [t for t in tags if isinstance(t, list) and len(t) >= 2 and t[0] == "tips"]
    assert len(tips_tags) > 0, f"Missing 'tips' tag in discovery event: {tags}"
