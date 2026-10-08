import json
import subprocess

import pytest

pytestmark = [pytest.mark.story("O8"),pytest.mark.api, pytest.mark.extended, pytest.mark.go_only]


def _skip_if_no_luci_app(router):
    out = router.ssh(
        "ls /usr/share/luci/menu.d/luci-app-tollgate-payments.json 2>/dev/null "
        "&& echo EXISTS || echo MISSING"
    )
    if "MISSING" in out:
        pytest.skip("PR #114 not installed (LuCI app files not found)")


@pytest.mark.extended
def test_luci_menu_registration_exists(router):
    _skip_if_no_luci_app(router)
    out = router.ssh(
        "ls -la /usr/share/luci/menu.d/luci-app-tollgate-payments.json 2>/dev/null "
        "&& echo EXISTS || echo MISSING"
    )
    assert "EXISTS" in out, (
        f"LuCI menu registration file missing: {out}"
    )


@pytest.mark.extended
def test_luci_rpcd_acl_exists(router):
    _skip_if_no_luci_app(router)
    out = router.ssh(
        "ls -la /usr/share/rpcd/acl.d/luci-app-tollgate-payments.json 2>/dev/null "
        "&& echo EXISTS || echo MISSING"
    )
    assert "EXISTS" in out, (
        f"RPCd ACL file missing: {out}"
    )

    raw = router.ssh("cat /usr/share/rpcd/acl.d/luci-app-tollgate-payments.json")
    acl = json.loads(raw)
    assert isinstance(acl, dict), f"ACL file is not a JSON object: {raw[:200]}"
    has_permissions = False
    for _role, perms in acl.items():
        if isinstance(perms, dict):
            for _scope, entries in perms.items():
                if isinstance(entries, list) and len(entries) > 0:
                    has_permissions = True
                    break
        if has_permissions:
            break
    assert has_permissions, f"ACL contains no permissions: {json.dumps(acl)[:300]}"


@pytest.mark.extended
def test_luci_css_assets_exist(router):
    _skip_if_no_luci_app(router)
    out = router.ssh(
        "ls -la /www/luci-static/resources/tollgate-payments/tg.css 2>/dev/null "
        "&& echo EXISTS || echo MISSING"
    )
    assert "EXISTS" in out, f"LuCI CSS asset missing: {out}"


@pytest.mark.extended
def test_luci_js_assets_exist(router):
    _skip_if_no_luci_app(router)
    out = router.ssh(
        "ls -la /www/luci-static/resources/view/tollgate-payments/settings.js 2>/dev/null "
        "&& echo EXISTS || echo MISSING"
    )
    assert "EXISTS" in out, f"LuCI JS asset missing: {out}"


@pytest.mark.extended
def test_config_schema_command_works(router):
    _skip_if_no_luci_app(router)
    result = router.cli_command("config", args=["schema", "--json"])
    if result.get("raw") and not result.get("success"):
        result = router.cli_command("config", args=["schema"])
    assert result.get("success") is True or isinstance(result.get("data"), (list, dict)), (
        f"config schema command failed: {result}"
    )
    data = result.get("data", result)
    assert isinstance(data, (list, dict)), (
        f"Schema data is not a list or dict: {type(data)} — {str(result)[:300]}"
    )


@pytest.mark.extended
def test_config_schema_has_expected_fields(router):
    _skip_if_no_luci_app(router)
    result = router.cli_command("config", args=["schema", "--json"])
    if result.get("raw") and not result.get("success"):
        result = router.cli_command("config", args=["schema"])
    data = result.get("data", result)
    schema_str = json.dumps(data).lower()
    expected_fields = ["config_version", "accepted_mints", "step_size", "metric", "profit_share"]
    found = [f for f in expected_fields if f in schema_str]
    assert len(found) >= 3, (
        f"Schema missing expected fields. Found: {found}. Expected at least 3 of {expected_fields}. "
        f"Schema preview: {schema_str[:500]}"
    )


@pytest.mark.extended
def test_config_get_command_works(router):
    _skip_if_no_luci_app(router)
    result = router.cli_command("config", args=["get"])
    assert result.get("success") is True, f"config get command failed: {result}"
    data = result.get("data", {})
    assert isinstance(data, dict), f"config get data is not a dict: {type(data)} — {result}"
    assert len(data) > 0, f"config get returned empty data: {result}"


@pytest.mark.extended
def test_health_command_works(router):
    _skip_if_no_luci_app(router)
    result = router.cli_command("health")
    assert "success" in result, f"health command response missing 'success' field: {result}"
    assert result.get("success") is True, f"health command failed: {result}"


@pytest.mark.extended
def test_cors_restricted_to_local(router):
    _skip_if_no_luci_app(router)
    evil_code = router.ssh(
        "curl -s -o /dev/null -w '%{http_code}' "
        "-H 'Origin: http://evil.com' "
        "http://127.0.0.1:2121/"
    ).strip()
    assert evil_code in ("403", "000"), (
        f"External origin should be rejected (got {evil_code}), not allowed through CORS"
    )

    local_code = router.ssh(
        f"curl -s -o /dev/null -w '%{{http_code}}' "
        f"-H 'Origin: http://{router.host}' "
        f"http://127.0.0.1:2121/"
    ).strip()
    assert local_code == "200", (
        f"Local origin should be accepted (got {local_code})"
    )


@pytest.mark.extended
def test_luci_page_loads(router):
    _skip_if_no_luci_app(router)
    r = subprocess.run(
        [
            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
            f"http://{router.host}/cgi-bin/luci/admin/tollgate-payments/settings",
        ],
        capture_output=True, text=True, timeout=15,
    )
    code = r.stdout.strip()
    assert code in ("200", "302", "303"), (
        f"LuCI tollgate-payments page returned {code} (expected 200/302/303)"
    )
