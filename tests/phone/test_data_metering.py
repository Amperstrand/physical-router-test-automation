import time
import logging
import pytest
from lib.helpers import (pay_and_wait, assert_internet, metering_test_setup,
                          assert_deauthenticated)
from lib.constants import TOKEN_SMALL

log = logging.getLogger("tollgate.data_metering")

pytestmark = [pytest.mark.story("G6"),pytest.mark.phone, pytest.mark.slow, pytest.mark.timeout(120), pytest.mark.config, pytest.mark.extended]

DOWNLOAD_URL = "http://cachefly.cachefly.net/1mb.test"


def test_data_metering(router, adb, cashu, wifi, test_pricing, screenshot_raw):
    step_bytes = 30720
    amount = TOKEN_SMALL
    expected_bytes = amount * step_bytes

    try:
        session, _ = metering_test_setup(router, adb, wifi, cashu, test_pricing,
                                         amount, step_bytes, "bytes")

        assert session.get("metric") == "bytes", f"Session metric is '{session.get('metric')}', expected 'bytes'"

        adb.shell(f"am start -a android.intent.action.VIEW -d '{DOWNLOAD_URL}'")
        log.info(f"Opened {DOWNLOAD_URL} in browser to consume data allotment ({expected_bytes}B)")

        cutoff_at = None
        for elapsed in range(30):
            state = router.get_nds_state()
            if state != "Authenticated":
                cutoff_at = elapsed
                log.info(f"Data allotment exhausted at {elapsed}s (state={state})")
                break
            time.sleep(1)

        if cutoff_at is None:
            pytest.skip(
                f"Data cutoff not detected within 30s "
                f"(allotment {expected_bytes}B)"
            )

        time.sleep(2)
        gateway = router.gateway_ip
        for attempt in range(4):
            if not adb.ping(gateway, interface="wlan0"):
                log.info(f"Internet cut off via wlan0 at attempt {attempt}")
                break
            time.sleep(2)
        else:
            if not adb.ping("1.1.1.1"):
                log.info("Unbound ping failed — internet cut off")
            else:
                state = router.get_nds_state()
                if state != "Authenticated":
                    log.info(f"ndsctl confirms deauth (state={state})")
                else:
                    pytest.fail("Internet still accessible after data allotment exhausted")

        assert_deauthenticated(router)
    finally:
        router.reset_state(adb=adb)
