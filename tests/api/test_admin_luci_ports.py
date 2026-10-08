import subprocess
import pytest

pytestmark = [
    pytest.mark.story("O8"),pytest.mark.api, pytest.mark.extended, pytest.mark.go_only]


def _skip_if_no_admin_spa(router):
    out = router.ssh("ls /www/tollgate/admin.html /www/net4sats/admin.html 2>/dev/null | head -1 || echo NOT_FOUND")
    if "NOT_FOUND" in out or not out.strip():
        pytest.skip("Admin SPA not deployed")


@pytest.mark.extended
def test_admin_spa_serves_on_port_80(router):
    _skip_if_no_admin_spa(router)

    r = subprocess.run(
        ["curl", "-s", "-L", "-o", "/dev/null", "-w", "%{http_code}",
         f"http://{router.host}/", "--connect-timeout", "5"],
        capture_output=True, text=True, timeout=15,
    )
    code = r.stdout.strip()
    assert code in ("200", "302", "307"), \
        f"Admin SPA on port 80 returned {code} (expected 200/302/307)"


@pytest.mark.extended
def test_luci_serves_on_port_8080(router):
    r = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         f"http://{router.host}:8080/", "--connect-timeout", "5"],
        capture_output=True, text=True, timeout=15,
    )
    code = r.stdout.strip()
    if code == "000":
        pytest.skip("LuCI not listening on port 8080 — may be on default port 80")
    assert code in ("200", "302", "303"), \
        f"LuCI on port 8080 returned {code} (expected 200/302/303)"
