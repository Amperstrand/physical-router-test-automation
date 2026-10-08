import pytest
import requests

pytestmark = [pytest.mark.story("R1"),pytest.mark.rust_basic_only, pytest.mark.api, pytest.mark.smoke]


def test_balance_returns_go_compatible_schema(rust_basic_server):
    """S3: GET /balance returns 200 with Go-compatible session-state schema.

    Matches tollgate-module-basic-go HandleBalance: {status, session_active,
    usage, allotment, remaining} (with metric/start_time/error omitted via
    Go's omitempty when no active session).
    """
    resp = requests.get(f"{rust_basic_server['http_url']}/balance", timeout=5)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    expected_fields = {"status", "session_active", "usage", "allotment", "remaining"}
    actual_fields = set(data.keys())
    assert expected_fields.issubset(actual_fields), (
        f"Missing required fields. Expected superset of {expected_fields}, got {actual_fields}"
    )
    assert isinstance(data["status"], int), f"status must be int: {type(data['status'])}"
    assert isinstance(data["session_active"], bool), (
        f"session_active must be bool: {type(data['session_active'])}"
    )
    assert isinstance(data["usage"], int), f"usage must be int: {type(data['usage'])}"
    assert isinstance(data["allotment"], int), f"allotment must be int: {type(data['allotment'])}"
    assert isinstance(data["remaining"], int), f"remaining must be int: {type(data['remaining'])}"
    assert data["remaining"] >= 0, f"remaining must be >= 0: {data['remaining']}"
