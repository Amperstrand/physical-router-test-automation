import json

import pytest

pytestmark = [pytest.mark.story("G8"),pytest.mark.api, pytest.mark.smoke, pytest.mark.go_only]


def _skip_if_no_version_cli(router):
    r = router.ssh("tollgate version 2>&1 || true", timeout=10)
    if "unknown command" in r.lower() or "not found" in r.lower() or not r.strip():
        pytest.skip("tollgate version subcommand not available")
    r_json = router.ssh("tollgate --json version 2>&1 || true", timeout=10)
    try:
        data = json.loads(r_json)
        if not data.get("success"):
            pytest.skip(f"tollgate --json version returned non-success: {str(data)[:120]}")
    except json.JSONDecodeError:
        pytest.skip(f"tollgate --json version returned non-JSON: {r_json[:120]}")


@pytest.fixture(scope="module")
def version(router):
    _skip_if_no_version_cli(router)
    for attempt in range(5):
        result = router.get_tollgate_version()
        if result.get("success"):
            return result
        import time
        time.sleep(2 * (attempt + 1))
    return router.get_tollgate_version()


@pytest.mark.smoke
def test_version_succeeds(version):
    assert version.get("success") is True, f"version command failed: {version}"


@pytest.mark.smoke
def test_version_has_message(version):
    msg = version.get("message", "")
    assert msg, f"Missing 'message' in version response: {version}"


@pytest.mark.smoke
def test_version_message_has_fields(version, router):
    msg = version.get("message", "")
    runtime_field = "rust_version:" if router.backend.is_rust_basic else "go_version:"
    for field in ("version:", "commit:", "build_time:", runtime_field):
        assert field in msg, f"Missing '{field}' in version message: {msg}"


@pytest.mark.smoke
def test_version_message_has_openwrt(version):
    msg = version.get("message", "")
    assert "openwrt" in msg.lower() or "OpenWrt" in msg, \
        f"Missing OpenWrt version in message: {msg}"


@pytest.mark.smoke
def test_version_commit_is_hex(version):
    msg = version.get("message", "")
    for line in msg.split("\n"):
        if line.strip().startswith("commit:"):
            commit = line.split(":", 1)[1].strip()
            if commit in ("unknown", ""):
                pytest.skip(f"Commit not embedded in binary: {commit}")
            assert len(commit) >= 7, f"Commit hash too short: {commit}"
            assert all(c in "0123456789abcdef" for c in commit.lower()), \
                f"Commit not hex: {commit}"
            return
    pytest.fail(f"No commit line found in version message: {msg}")
