"""Config file permissions tests — verify sensitive config files are root-only (0600)."""
import pytest

pytestmark = [pytest.mark.story("O8"),pytest.mark.api, pytest.mark.smoke]


@pytest.mark.smoke
def test_config_json_permissions(router):
    """Verify /etc/tollgate/config.json is 0600 (not world-readable)."""
    output = router.ssh("stat -c '%a' /etc/tollgate/config.json 2>/dev/null || echo 'NOT_FOUND'")
    if "NOT_FOUND" in output:
        pytest.skip("config.json not found on this router")
    perms = output.strip()
    assert perms == "600", (
        f"config.json permissions are {perms}, expected 600 (root-only).\n"
        f"This means PR #221 (fix/config-file-permissions) is not deployed."
    )


def test_identities_json_permissions(router):
    """Verify /etc/tollgate/identities.json is 0600 (contains the private key)."""
    output = router.ssh("stat -c '%a' /etc/tollgate/identities.json 2>/dev/null || echo 'NOT_FOUND'")
    if "NOT_FOUND" in output:
        pytest.skip("identities.json not found on this router")
    perms = output.strip()
    assert perms == "600", (
        f"identities.json permissions are {perms}, expected 600.\n"
        f"identities.json contains the merchant private key — must be root-only."
    )


def test_no_world_readable_config_files(router):
    output = router.ssh(
        "find /etc/tollgate/ -type f "
        "-not -path '/etc/tollgate/tollgate-captive-portal-site/*' "
        r"-not -name '*.test-backup' -not -name '*.ps-backup' -not -name '*.bak' "
        r"\( -perm /go+w -o -perm /go+r \) 2>/dev/null | head -5"
    )
    leaking = [f for f in output.strip().split("\n") if f and "No such" not in f]
    assert not leaking, (
        f"World-readable/writable files found in /etc/tollgate/: {leaking}\n"
        f"All config files should be 0600 (root-only)."
    )
