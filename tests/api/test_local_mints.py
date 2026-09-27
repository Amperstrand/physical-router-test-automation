"""Health checks for local mints in cloud lab / virtual lab environments."""

from __future__ import annotations

import json
import os
from urllib import request
from urllib.error import URLError

import pytest

pytestmark = [pytest.mark.api, pytest.mark.virtual_lab]


def _skip_if_no_local_mints():
    if not os.environ.get("TOLLGATE_VIRTUAL_LAB"):
        pytest.skip("Local mints only available in virtual lab")


def _mint_available(url: str, timeout: int = 5) -> bool:
    try:
        request.urlopen(f"{url}/v1/keys", timeout=timeout)
        return True
    except (URLError, OSError):
        return False


@pytest.mark.extended
def test_cdk_v2_mint_healthy():
    _skip_if_no_local_mints()
    url = os.environ.get("TOLLGATE_CDK_MINT_URL", "http://10.99.99.2:8383")
    resp = request.urlopen(f"{url}/v1/keys", timeout=10)
    assert resp.status == 200, f"Expected status 200 for CDK mint keys endpoint, got {resp.status}"
    data = json.loads(resp.read())
    keysets = data.get("keysets", [])
    assert len(keysets) > 0, f"Expected at least one keyset, got {len(keysets)}"
    keyset_ids = [k.get("id", "") for k in keysets]
    assert any(k.startswith("01") for k in keyset_ids), f"CDK should have V2 keyset, got: {keyset_ids}"


@pytest.mark.extended
def test_nutshell_v2_mint_healthy():
    _skip_if_no_local_mints()
    url = os.environ.get("TOLLGATE_NUTSHELL_V2_MINT_URL", "http://10.99.99.2:8384")
    if not _mint_available(url):
        pytest.skip("Nutshell V2 mint not available")
    resp = request.urlopen(f"{url}/v1/keys", timeout=10)
    assert resp.status == 200, f"Expected status 200 for Nutshell V2 mint keys endpoint, got {resp.status}"


@pytest.mark.extended
def test_nutshell_v1_mint_healthy():
    _skip_if_no_local_mints()
    url = os.environ.get("TOLLGATE_NUTSHELL_V1_MINT_URL", "http://10.99.99.2:8385")
    if not _mint_available(url):
        pytest.skip("Nutshell V1 mint not available")
    resp = request.urlopen(f"{url}/v1/keys", timeout=10)
    assert resp.status == 200, f"Expected status 200 for Nutshell V1 mint keys endpoint, got {resp.status}"


@pytest.mark.extended
def test_cdk_v2_mint_info():
    _skip_if_no_local_mints()
    url = os.environ.get("TOLLGATE_CDK_MINT_URL", "http://10.99.99.2:8383")
    resp = request.urlopen(f"{url}/v1/info", timeout=10)
    assert resp.status == 200, f"Expected status 200 for CDK mint info endpoint, got {resp.status}"
    data = json.loads(resp.read())
    assert isinstance(data, dict) and len(data) > 0, f"Expected non-empty dict, got type {type(data).__name__} with {len(data)} keys: {data}"


@pytest.mark.extended
def test_local_mints_reachable_from_openwrt(router):
    _skip_if_no_local_mints()
    v1_url = os.environ.get("TOLLGATE_NUTSHELL_V1_MINT_URL", "http://10.99.99.2:8385")
    if not _mint_available(v1_url):
        pytest.skip(f"Nutshell V1 mint not running at {v1_url} (single-mint local topology)")
    result = ""
    for attempt in range(3):
        try:
            result = router.ssh(f"wget -qO- --timeout=10 {v1_url}/v1/keys 2>/dev/null | head -c 100", timeout=20)
            if "keysets" in result:
                break
        except Exception:
            if attempt == 2:
                raise
            import time
            time.sleep(3)
    assert "keysets" in result, f"OpenWrt should reach local mint at {v1_url}, result: {result[:200]}..."
