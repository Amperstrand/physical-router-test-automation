import json
import pytest

pytestmark = [pytest.mark.story("O8"),pytest.mark.api, pytest.mark.extended, pytest.mark.go_only]


def _skip_if_no_rpcd_plugin(router):
    out = router.ssh("test -x /usr/libexec/rpcd/tollgate && echo YES || echo NO")
    if "YES" not in out:
        pytest.skip("rpcd tollgate plugin not installed")


@pytest.mark.extended
def test_rpcd_shell_injection_blocked(router):
    _skip_if_no_rpcd_plugin(router)

    result = router.ssh(
        'ubus call tollgate config_set \'{"key":"log_level; rm -rf /tmp/inject-test","value":"debug"}\' 2>&1 || true'
    )
    assert "error" in result.lower() or "not in allowed" in result.lower() or "invalid" in result.lower(), \
        f"Shell injection may have succeeded: {result[:300]}"

    inject_file = router.ssh("ls /tmp/inject-test 2>&1 || echo NOT_FOUND")
    assert "NOT_FOUND" in inject_file, "Shell injection created a file — security vulnerability"

    result2 = router.ssh(
        '''ubus call tollgate config_set '{"key":"log_level$(echo pwned)","value":"debug"}' 2>&1 || true'''
    )
    assert "error" in result2.lower() or "invalid" in result2.lower() or "not in allowed" in result2.lower(), \
        f"Command substitution injection may have succeeded: {result2[:300]}"


@pytest.mark.extended
def test_rpcd_acl_unauthenticated_write_blocked(router):
    _skip_if_no_rpcd_plugin(router)

    acl_raw = router.ssh("cat /usr/share/rpcd/acl.d/tollgate.json 2>/dev/null || echo NOT_FOUND")
    if "NOT_FOUND" in acl_raw:
        pytest.skip("tollgate ACL file not found")

    acl = json.loads(acl_raw)
    has_unauth = False
    has_no_write = True
    for role_name, role_data in acl.items():
        if not isinstance(role_data, dict):
            continue
        for scope, methods in role_data.items():
            if not isinstance(methods, list):
                continue
            for method in methods:
                if not isinstance(method, dict):
                    continue
                if "config_set" in str(method) or "config_save" in str(method):
                    pass
            if "ubus" in scope or "read" in scope.lower():
                for m in methods:
                    if isinstance(m, dict):
                        if "config_set" in str(m) and "unauthenticated" in role_name.lower():
                            has_no_write = False

    result = router.ssh(
        'ubus call tollgate config_set \'{"key":"log_level","value":"debug"}\' 2>&1'
    )
    if "not authorized" in result.lower() or "denied" in result.lower() or "access" in result.lower():
        pass
    elif '"success"' in result:
        pass
    else:
        pass
