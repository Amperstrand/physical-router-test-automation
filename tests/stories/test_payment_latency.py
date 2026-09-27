"""Performance benchmarks: payment latency, portal load time.

These stories measure how long key operations take. They don't have
hard pass/fail thresholds (which vary by hardware) — they report
the measurements and fail only if something takes unreasonably long.
"""
import logging
import os
import subprocess
import time

import pytest

from lib.contract import min_token_sats, probe_host
from lib.cashu import HttpMinter

log = logging.getLogger("tollgate.story.bench")

pytestmark = [pytest.mark.slow]

ROUTER_HOST = os.environ.get("TOLLGATE_SSH_HOST", "")
MAX_PAYMENT_SECONDS = 30
MAX_PORTAL_LOAD_MS = 5000


@pytest.mark.skipif(not ROUTER_HOST, reason="TOLLGATE_SSH_HOST not set")
class TestPaymentLatency:
    def test_mint_to_auth_latency(self, no_session, rate_limiter):
        """Measure: time from token mint to authenticated internet."""
        device = no_session

        t_start = time.time()

        # Mint
        mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL",
                                  "http://192.168.13.221:8383")
        token = HttpMinter(mint_url).mint(min_token_sats())
        t_minted = time.time()

        # Submit
        rate_limiter()
        assert device.submit_token(token), "payment failed"
        t_submitted = time.time()

        # Wait for internet
        deadline = time.time() + MAX_PAYMENT_SECONDS
        while time.time() < deadline:
            if device.has_internet(probe_host()):
                break
            time.sleep(1)
        t_internet = time.time()

        total = t_internet - t_start
        mint_time = t_minted - t_start
        submit_time = t_submitted - t_minted
        auth_time = t_internet - t_submitted

        assert device.has_internet(), "no internet after payment"

        log.info("╔══════════════════════════════════════╗")
        log.info("║ Payment Latency Benchmark             ║")
        log.info("╠══════════════════════════════════════╣")
        log.info("║ Mint time:     %6.2fs              ║", mint_time)
        log.info("║ Submit time:   %6.2fs              ║", submit_time)
        log.info("║ Auth wait:     %6.2fs              ║", auth_time)
        log.info("║──────────────────────────────────────║")
        log.info("║ TOTAL:         %6.2fs              ║", total)
        log.info("╚══════════════════════════════════════╝")

        assert total < MAX_PAYMENT_SECONDS, \
            f"payment took {total:.1f}s (limit: {MAX_PAYMENT_SECONDS}s)"

    def test_portal_load_time(self):
        """Measure: time for the portal page to load and render."""
        import subprocess as sp

        url = f"http://{ROUTER_HOST}:2051/splash.html"

        t_start = time.time()
        r = sp.run(
            ["curl", "-s", "-o", "/dev/null", "-w",
             "%{time_total}", "-m", str(MAX_PORTAL_LOAD_MS / 1000), url],
            capture_output=True, text=True, timeout=MAX_PORTAL_LOAD_MS / 1000 + 5)
        load_time = float(r.stdout.strip()) if r.stdout.strip() else -1

        log.info("Portal load time: %.3fs (HTTP %s)", load_time,
                 "ok" if load_time > 0 else "failed")
        assert 0 < load_time < MAX_PORTAL_LOAD_MS / 1000, \
            f"portal load took {load_time:.3}s (limit: {MAX_PORTAL_LOAD_MS / 1000}s)"

    def test_backend_response_time(self):
        """Measure: time for the backend discovery event."""
        import subprocess as sp

        t_start = time.time()
        r = sp.run(
            ["ssh", "-o", "ConnectTimeout=5",
             "-o", "StrictHostKeyChecking=no",
             f"root@{ROUTER_HOST}",
             "wget -qO- --timeout=5 'http://[::1]:2121/' 2>/dev/null | wc -c"],
            capture_output=True, text=True, timeout=15)
        elapsed = time.time() - t_start
        size = int(r.stdout.strip() or 0)

        log.info("Backend response: %.3fs (%d bytes)", elapsed, size)
        assert size > 0, "backend returned empty response"
        assert elapsed < 5.0, f"backend took {elapsed:.2f}s (limit: 5s)"
