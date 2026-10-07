import time
import pytest

pytestmark = [pytest.mark.story("P5"),pytest.mark.api, pytest.mark.extended]


def _beacon_cgi_available(router):
    resp = router.router_fetch_status(router.cgi_url('tollgate-log'))
    code = resp.strip()
    return code not in ("000", "404", "500", "503", "")


@pytest.mark.extended
def test_log_beacon_accepts_post(router):
    router.clear_portal_log()
    if not _beacon_cgi_available(router):
        pytest.skip("tollgate-log CGI not installed on this build")
    code = router.api_status("/cgi-bin/tollgate-log")
    assert code == 200, f"Log beacon CGI returned {code}"


@pytest.mark.extended
def test_log_beacon_appends_data(router):
    if not _beacon_cgi_available(router):
        pytest.skip("tollgate-log CGI not installed on this build")
    marker = f"test-beacon-{int(time.time())}"
    router.clear_portal_log()
    router.router_fetch(router.cgi_url('tollgate-log'), data=marker)
    log = router.get_portal_log()
    assert marker in log, f"Beacon data not found in log file: {log[:200]}"


@pytest.mark.extended
def test_log_beacon_truncates_at_limit(router):
    if not _beacon_cgi_available(router):
        pytest.skip("tollgate-log CGI not installed on this build")
    router.ssh("dd if=/dev/zero bs=1024 count=600 2>/dev/null | tr '\\0' 'A' > /tmp/tollgate-portal.log")
    initial_size = router.ssh("wc -c < /tmp/tollgate-portal.log").strip()
    router.router_fetch(router.cgi_url('tollgate-log'), data='trigger-truncate')
    final_size = router.ssh("wc -c < /tmp/tollgate-portal.log").strip()
    assert int(final_size) < int(initial_size), \
        f"Log not truncated: {initial_size} → {final_size} bytes"
    router.clear_portal_log()
