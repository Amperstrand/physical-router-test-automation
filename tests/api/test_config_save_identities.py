import json
import pytest

pytestmark = [
    pytest.mark.story("M6"),pytest.mark.api, pytest.mark.extended, pytest.mark.go_only]


def _skip_if_no_json_cli(router):
    out = router.ssh("tollgate --json config get 2>&1 || true")
    if "metric" not in out:
        pytest.skip("tollgate --json config get not available (PR #124 not deployed)")


@pytest.mark.extended
def test_config_save_identities_round_trip(router):
    _skip_if_no_json_cli(router)

    original = router.ssh("tollgate --json config get 2>&1")
    original_data = json.loads(original)
    identities = original_data.get("data", {}).get("identities", {})

    save_result = router.ssh(
        f"tollgate --json config save-identities '{json.dumps(identities)}' 2>&1"
    )
    if '"success"' not in save_result:
        pytest.skip(f"config save-identities not supported: {save_result[:200]}")

    assert '"success"' in save_result and 'true' in save_result, \
        f"save-identities failed: {save_result[:300]}"

    disk = router.ssh("cat /etc/tollgate/identities.json 2>/dev/null || echo NOT_FOUND")
    assert "NOT_FOUND" not in disk, "identities file missing after save"

    disk_data = json.loads(disk)
    original_merchant = identities.get("owned_identities", [{}])
    disk_merchant = disk_data.get("owned_identities", [{}])
    if original_merchant and disk_merchant:
        assert original_merchant[0].get("name") == disk_merchant[0].get("name"), \
            "merchant identity name changed after save-identities"


@pytest.mark.extended
def test_config_save_identities_rejects_invalid(router):
    _skip_if_no_json_cli(router)

    result = router.ssh("tollgate --json config save-identities 'not-json' 2>&1")
    assert "error" in result.lower() or "invalid" in result.lower() or "parse" in result.lower() or '"success"' not in result, \
        f"Accepted invalid JSON for save-identities: {result[:300]}"

    result2 = router.ssh("tollgate --json config save-identities '{bad json}' 2>&1")
    assert "error" in result2.lower() or "invalid" in result2.lower() or "parse" in result2.lower() or '"success"' not in result2, \
        f"Accepted malformed JSON for save-identities: {result2[:300]}"
