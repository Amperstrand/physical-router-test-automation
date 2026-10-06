import json
import pytest
from lib.helpers import parse_json_or_fail

pytestmark = [
    pytest.mark.story("G1"),pytest.mark.api, pytest.mark.smoke]


def _captive_api_available(router):
    resp = router.router_fetch_status(router.cgi_url('captive-portal-api'))
    code = resp.strip()
    return code not in ("000", "404", "500", "503", "")


@pytest.mark.smoke
def test_captive_api_pre_auth(router):
    if not _captive_api_available(router):
        pytest.skip("captive-portal-api CGI not installed on this build")
    resp = router.router_fetch(router.cgi_url('captive-portal-api'))
    assert resp, "Empty response from captive portal API"
    data = parse_json_or_fail(resp, "captive portal API response")
    assert "captive" in data, f"Missing 'captive' field: {resp[:200]}"
    assert "user-portal-url" in data, f"Missing 'user-portal-url' field: {resp[:200]}"
    assert data["captive"] is True, f"Expected captive=true, got: {data}"


@pytest.mark.smoke
def test_captive_api_content_type(router):
    if not _captive_api_available(router):
        pytest.skip("captive-portal-api CGI not installed on this build")
    resp = router.ssh(f"curl -s -D - -o /dev/null '{router.cgi_url('captive-portal-api')}' 2>&1")
    assert "application/captive+json" in resp, \
        f"Missing application/captive+json content type: {resp[:200]}"


@pytest.mark.smoke
def test_captive_api_no_cache(router):
    if not _captive_api_available(router):
        pytest.skip("captive-portal-api CGI not installed on this build")
    resp = router.ssh(f"curl -s -D - -o /dev/null '{router.cgi_url('captive-portal-api')}' 2>&1")
    assert "no-store" in resp.lower(), \
        f"Missing Cache-Control: no-store header: {resp[:200]}"
