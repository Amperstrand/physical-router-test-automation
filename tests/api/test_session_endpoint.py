import json
import pytest
from lib.helpers import parse_json_or_fail

pytestmark = [pytest.mark.story("G4"),pytest.mark.api, pytest.mark.extended]


def _skip_if_degraded(router):
    resp = router.api_body("/")
    if not resp:
        pytest.skip("Backend not responding (likely degraded mode)")


@pytest.mark.extended
def test_session_response_structure(router):
    _skip_if_degraded(router)
    resp = router.api_body("/balance")
    data = parse_json_or_fail(resp, "balance response")
    assert isinstance(data, dict), f"Balance response is not a dict: {type(data)}"


@pytest.mark.extended
def test_session_has_remaining_or_error(router):
    if router.backend.is_rust:
        pytest.skip("Rust v1 session API format differs from Go (Amperstrand/tollgate-rs#42)")
    _skip_if_degraded(router)
    resp = router.api_body("/balance")
    data = parse_json_or_fail(resp, "balance response")
    assert "remaining" in data or "allotment" in data or "error" in data or "raw" in data or data.get("kind") == 10021, \
        f"Balance response missing expected fields: {resp[:200]}"


@pytest.mark.extended
def test_session_with_client_ip(router):
    _skip_if_degraded(router)
    if not router.phone_ip:
        pytest.skip("No client IP configured (TOLLGATE_CLIENT_IP)")
    resp = router.backend_curl_xff(router.backend_url("/balance"), router.phone_ip)
    if not resp:
        pytest.skip("Balance endpoint returned empty (no active client session)")
    data = parse_json_or_fail(resp, "balance response with X-Forwarded-For")
    assert isinstance(data, dict), f"Session response is not a dict: {type(data)}"
