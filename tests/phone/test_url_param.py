# TIP-03: Token Delivery via URL Parameter
#
# Portal JS reads ?token= from URL and submits to TIP-03 POST /.
# Falls back to router LAN IP when TOLLGATE_DOMAIN is not configured.

import urllib.parse
import logging
import pytest
from lib.helpers import assert_session_active
from lib.constants import TOKEN_DEFAULT

pytestmark = [pytest.mark.story("G5"), pytest.mark.tip("TIP-03"),pytest.mark.phone, pytest.mark.slow, pytest.mark.timeout(120), pytest.mark.extended, pytest.mark.requires_wifi]

log = logging.getLogger("tollgate.test_url_param")


def test_url_param_token(router, adb, cashu, wifi, connected_wifi, screenshot_portal):
    portal_host = router.domain or router.get_nds_gateway_domain() or wifi._get_portal_host()
    portal_port = router.get_nds_portal_port()
    token = cashu.mint(TOKEN_DEFAULT)
    encoded = urllib.parse.quote(token)
    portal_url = f"http://{portal_host}:{portal_port}/?token={encoded}"

    adb.open_url(portal_url)

    assert router.wait_for_auth(timeout=60), "Auth not detected after URL param"

    screenshot_portal("url-param-final.png")
    assert_session_active(router)
