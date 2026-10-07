import re

import pytest
import requests

pytestmark = [pytest.mark.story("R1"),pytest.mark.rust_basic_only, pytest.mark.api, pytest.mark.smoke]


def test_usage_returns_valid_format(rust_basic_server):
    resp = requests.get(f"{rust_basic_server['http_url']}/usage", timeout=5)
    assert resp.status_code in (200, 500), f"Expected 200 or 500, got {resp.status_code}: {resp.text[:200]}"
    body = resp.text.strip()
    assert re.match(r"^-?\d+/-?\d+$", body), (
        f"Usage body doesn't match X/Y format: {body!r}"
    )
