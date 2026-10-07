"""Tests for PR #104: Normalize and validate mint URLs in config.

Verifies that mint URLs in the config and discovery response are
properly formatted, have no trailing slashes, and use HTTPS.
"""

import json

import os

import pytest

from lib.helpers import parse_json_or_fail

pytestmark = [pytest.mark.story("O4"),pytest.mark.api, pytest.mark.extended]


def _skip_if_local_mint():
    if os.environ.get("TOLLGATE_VIRTUAL_LAB"):
        pytest.skip("Local mints use HTTP in virtual lab")


def _get_config_mint_urls(router):
    cfg_raw = router.ssh("cat /etc/tollgate/config.json")
    cfg = json.loads(cfg_raw)
    return [m["url"] for m in cfg.get("accepted_mints", []) if "url" in m]


@pytest.mark.extended
def test_config_mint_urls_use_https(router):
    _skip_if_local_mint()
    urls = _get_config_mint_urls(router)
    assert urls, "No mint URLs found in config"
    for url in urls:
        assert url.startswith("https://"), \
            f"Mint URL does not use HTTPS: {url}"


@pytest.mark.extended
def test_config_mint_urls_no_trailing_slash(router):
    urls = _get_config_mint_urls(router)
    for url in urls:
        path = url.split("//", 1)[1] if "//" in url else url
        assert not path.endswith("/"), \
            f"Mint URL has trailing slash: {url}"


@pytest.mark.extended
def test_config_mint_urls_valid_format(router):
    _skip_if_local_mint()
    urls = _get_config_mint_urls(router)
    for url in urls:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        assert parsed.scheme == "https", f"Invalid scheme: {url}"
        assert parsed.hostname, f"No hostname in: {url}"
        assert "." in parsed.hostname, f"Invalid hostname: {parsed.hostname}"


@pytest.mark.extended
def test_discovery_mint_urls_match_config(router):
    urls = _get_config_mint_urls(router)
    body = router.api_body("/")
    event = parse_json_or_fail(body, "discovery response")
    if event.get("kind") == 21023:
        pytest.skip("Discovery in degraded mode, cannot verify mint URLs in discovery")

    discovery_mint_tags = [
        t for t in event.get("tags", [])
        if isinstance(t, list) and len(t) >= 3 and t[0] == "price_per_step"
    ]
    if not discovery_mint_tags:
        pytest.skip("No price_per_step tags in discovery event")

    discovery_mints = set()
    for tag in discovery_mint_tags:
        for item in tag[2:]:
            if item.startswith("http"):
                discovery_mints.add(item.rstrip("/"))

    config_mints = {u.rstrip("/") for u in urls}
    assert discovery_mints.issubset(config_mints), \
        f"Discovery has mints not in config: {discovery_mints - config_mints}"
