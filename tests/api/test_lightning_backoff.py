"""Lightning quote monitor backoff and jitter (PR #249/#270)."""

import calendar
import json
import os
import re
import time

import pytest
import requests

from lib.constants import BACKEND_PORT

pytestmark = [
    pytest.mark.story("O2"),pytest.mark.api, pytest.mark.slow, pytest.mark.go_only, pytest.mark.extended]


def _skip_if_no_ln_invoice(router):
    resp = router.api_status("/ln-invoice")
    if resp == 404 or resp == 0:
        pytest.skip(f"ln-invoice endpoint not available (status={resp})")


def _skip_if_degraded(router):
    discovery_raw = router.api_body("/")
    try:
        discovery = json.loads(discovery_raw)
    except json.JSONDecodeError:
        pytest.skip(f"Backend not returning JSON: {discovery_raw[:200]}")
    if discovery.get("kind") == 21023:
        pytest.skip("Backend in degraded mode")
    return discovery


def _create_invoice(router, amount=21, retries=3):
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL", "")
    if not mint_url:
        discovery = _skip_if_degraded(router)
        for tag in discovery.get("tags", []):
            if isinstance(tag, list) and len(tag) >= 5 and tag[0] == "price_per_step":
                mint_url = tag[4]
                break
    if not mint_url:
        pytest.skip("Cannot determine mint URL")

    backend_ip = os.environ.get("TOLLGATE_SSH_HOST", "10.99.99.1")
    url = f"http://{backend_ip}:{BACKEND_PORT}/ln-invoice"
    for attempt in range(retries):
        try:
            resp = requests.post(url, json={"amount": amount, "mint_url": mint_url}, timeout=15)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        time.sleep(5)
    pytest.fail(f"POST /ln-invoice failed after {retries} attempts")


def _extract_log_timestamps(logs, pattern):
    timestamps = []
    current_year = time.localtime().tm_year
    for line in logs.splitlines():
        if pattern not in line:
            continue
        m = re.search(r"(\w{3})\s+(\d+)\s+(\d{2}):(\d{2}):(\d{2})", line)
        if not m:
            continue
        month_str, day, hour, minute, second = m.groups()
        months = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
                  "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
        month = months.get(month_str)
        if not month:
            continue
        ts = calendar.timegm((current_year, month, int(day), int(hour), int(minute), int(second), 0, 0, -1))
        timestamps.append(ts)
    return timestamps


def test_backoff_progression_on_mint_error(router):
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)
    invoice = _create_invoice(router)
    quote_id = invoice.get("quote", "")
    time.sleep(35)
    logs = router.get_tollgate_logs(lines=5000)
    timestamps = _extract_log_timestamps(logs, quote_id)
    assert len(timestamps) >= 2, f"Expected >=2 entries for {quote_id[:12]}, got {len(timestamps)}"
    intervals = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
    assert intervals[0] >= 4.0, f"First interval {intervals[0]:.1f}s < 4s"
    if len(intervals) >= 2:
        assert intervals[1] >= 8.0, f"Second interval {intervals[1]:.1f}s < 8s"


def test_jitter_present_in_polling(router):
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)
    invoice = _create_invoice(router)
    quote_id = invoice.get("quote", "")
    time.sleep(40)
    logs = router.get_tollgate_logs(lines=5000)
    timestamps = _extract_log_timestamps(logs, quote_id)
    assert len(timestamps) >= 2, f"Need >=2 timestamps, got {len(timestamps)}"
    intervals = [round(timestamps[i + 1] - timestamps[i], 3) for i in range(len(timestamps) - 1)]
    identical = sum(1 for i in range(1, len(intervals)) if intervals[i] == intervals[i - 1])
    assert identical <= max(1, len(intervals) // 4), f"{identical}/{len(intervals)} identical — no jitter"


def test_no_backoff_hammering_on_mint_error(router):
    _skip_if_no_ln_invoice(router)
    _skip_if_degraded(router)
    invoice = _create_invoice(router)
    quote_id = invoice.get("quote", "")
    time.sleep(35)
    logs = router.get_tollgate_logs(lines=5000)
    timestamps = _extract_log_timestamps(logs, quote_id)
    cutoff = time.time() - 40
    recent = [t for t in timestamps if t >= cutoff]
    assert len(recent) >= 1, f"No monitor entries for {quote_id[:12]} in 35s (total log entries: {len(timestamps)})"
    assert len(recent) <= 6, f"Hammered {len(recent)} times in 35s — old 2s ticker"
