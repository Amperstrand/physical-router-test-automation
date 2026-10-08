import pytest

pytestmark = [pytest.mark.story("O5"),pytest.mark.api, pytest.mark.extended, pytest.mark.go_only]


def _skip_if_no_tollgate_portal(router):
    out = router.ssh("opkg list-installed 2>/dev/null | grep tollgate || echo NOT_FOUND")
    if "NOT_FOUND" in out:
        pytest.skip("tollgate package not installed")


@pytest.mark.extended
def test_ipk_install_deploys_files(router):
    _skip_if_no_tollgate_portal(router)

    plugin = router.ssh("test -x /usr/libexec/rpcd/tollgate && echo YES || echo NO")
    if "YES" not in plugin:
        pytest.skip("rpcd tollgate plugin not installed — install tollgate-captive-portal ipk first")

    acl = router.ssh("test -f /usr/share/rpcd/acl.d/tollgate.json && echo YES || echo NO")
    assert "YES" in acl, "ACL file not deployed by ipk"

    admin_html = router.ssh("test -f /www/tollgate/admin.html && echo YES || echo NO")
    if "YES" not in admin_html:
        admin_index = router.ssh("ls /www/tollgate/index.html 2>/dev/null && echo YES || echo NO")
        if "YES" not in admin_index:
            admin_net4sats = router.ssh("ls /www/net4sats/admin.html 2>/dev/null && echo YES || echo NO")
            assert "YES" in admin_net4sats, \
                "No admin SPA found at /www/tollgate/ or /www/net4sats/"


@pytest.mark.extended
def test_ipk_install_rpcd_responds(router):
    plugin = router.ssh("test -x /usr/libexec/rpcd/tollgate && echo YES || echo NO")
    if "YES" not in plugin:
        pytest.skip("rpcd tollgate plugin not installed")

    result = router.ssh("ubus list tollgate 2>&1")
    assert "tollgate" in result, f"ubus list tollgate failed: {result[:200]}"

    schema = router.ssh("ubus call tollgate config_schema 2>&1")
    assert "json_key" in schema or "schema" in schema.lower(), \
        f"ubus config_schema failed: {schema[:200]}"


@pytest.mark.extended
def test_ipk_uninstall_removes_files(router):
    installed = router.ssh("opkg list-installed 2>/dev/null | grep tollgate-captive-portal && echo YES || echo NO")
    if "YES" not in installed:
        pytest.skip("tollgate-captive-portal ipk not installed — cannot test uninstall")

    pytest.skip("Destructive test — run manually with: opkg remove tollgate-captive-portal && verify cleanup")
