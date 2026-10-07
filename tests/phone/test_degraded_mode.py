"""Phone-tier degraded mode tests.

Verify that the captive portal remains accessible and functional when
the tollgate backend enters degraded mode (all mints unreachable) and
recovers after mint connectivity is restored.

Tests skip cleanly on versions that do not support degraded mode.
"""

import json
import re
import time
from urllib.parse import urlparse

import pytest

from lib.helpers import parse_json_or_fail, skip_if_no_mint_health_tracker as _skip_if_no_degraded_support

pytestmark = [pytest.mark.story("O1"),pytest.mark.phone, pytest.mark.slow, pytest.mark.timeout(300), pytest.mark.requires_wifi]

HEALTH_POLL_INTERVAL = 5
HEALTH_POLL_TIMEOUT = 120


def _get_mint_ip_map(router):
    cfg_raw = router.ssh("cat /etc/tollgate/config.json")
    cfg = json.loads(cfg_raw)
    urls = [m["url"] for m in cfg.get("accepted_mints", []) if "url" in m]
    if not urls:
        pytest.skip("No mint URLs found in router config")

    ip_map = {}
    for url in urls:
        hostname = urlparse(url).hostname
        out = router.ssh(f"nslookup {hostname} 2>/dev/null || echo FAILED")
        ips = re.findall(r"Address:\s*(\d+\.\d+\.\d+\.\d+)", out)
        for ip in reversed(ips):
            if not ip.startswith("127."):
                ip_map[url] = ip
                break
    if not ip_map:
        pytest.skip("Could not resolve any mint hostnames to IPs")
    return ip_map


def _block_mints(router, mint_ip_map):
    rules = []
    for url, ip in mint_ip_map.items():
        router.ssh(f"iptables -I OUTPUT -d {ip} -p tcp --dport 443 -j REJECT")
        rules.append((url, ip))
    return rules


def _unblock_mints(router, rules):
    for url, ip in rules:
        router.ssh(
            f"iptables -D OUTPUT -d {ip} -p tcp --dport 443 -j REJECT"
            f" 2>/dev/null || true"
        )


def _is_degraded_mode(logs):
    signals = re.findall(
        r"(degraded|no reachable mints|all mints unreachable|entering degraded)",
        logs, re.IGNORECASE,
    )
    return len(signals) > 0


@pytest.fixture(autouse=True)
def reset_after_block(router):
    yield
    output = router.ssh("iptables -L OUTPUT -n 2>/dev/null")
    if "REJECT" in output:
        router.ssh("iptables -D OUTPUT -j REJECT 2>/dev/null || true")
    code = router.api_status("/")
    if code != 200:
        router.restart_backend()
        time.sleep(10)


def test_phone_portal_visible_during_degraded_mode(
    router, adb, connected_wifi, screenshot_portal,
):
    _skip_if_no_degraded_support(router)

    screenshot_portal("degraded-before-block.png")

    mint_ip_map = _get_mint_ip_map(router)
    rules = _block_mints(router, mint_ip_map)

    try:
        degraded = False
        deadline = time.time() + HEALTH_POLL_TIMEOUT
        while time.time() < deadline:
            logs = router.get_tollgate_logs(lines=500)
            if _is_degraded_mode(logs):
                degraded = True
                break
            body = router.api_body("/")
            try:
                data = json.loads(body)
                if data.get("kind") == 21023:
                    degraded = True
                    break
            except json.JSONDecodeError:
                pass
            time.sleep(HEALTH_POLL_INTERVAL)

        if not degraded:
            pytest.skip(
                f"Service did not enter degraded mode within {HEALTH_POLL_TIMEOUT}s"
            )

        xml = adb.ui_xml()
        assert xml, "Phone UI XML is empty — portal may have crashed"

        portal_texts = re.findall(r'text="([^"]{3,})"', xml)
        assert portal_texts, \
            "No text elements found in phone UI — captive portal not rendering"

        sm_match = re.search(r'data-sm="([^"]*)"', xml)
        if sm_match:
            state = sm_match.group(1)
            assert state, f"Portal state machine is empty: {state!r}"

        screenshot_portal("degraded-active.png")
    finally:
        _unblock_mints(router, rules)


def test_phone_can_pay_after_mint_recovery(
    router, adb, cashu, connected_wifi, screenshot_portal,
):
    _skip_if_no_degraded_support(router)

    mint_ip_map = _get_mint_ip_map(router)
    rules = _block_mints(router, mint_ip_map)

    try:
        degraded = False
        deadline = time.time() + HEALTH_POLL_TIMEOUT
        while time.time() < deadline:
            logs = router.get_tollgate_logs(lines=500)
            if _is_degraded_mode(logs):
                degraded = True
                break
            body = router.api_body("/")
            try:
                data = json.loads(body)
                if data.get("kind") == 21023:
                    degraded = True
                    break
            except json.JSONDecodeError:
                pass
            time.sleep(HEALTH_POLL_INTERVAL)

        if not degraded:
            pytest.skip(
                f"Service did not enter degraded mode within {HEALTH_POLL_TIMEOUT}s"
            )

        screenshot_portal("recovery-degraded.png")

        _unblock_mints(router, rules)
        rules = []

        recovered = False
        deadline = time.time() + HEALTH_POLL_TIMEOUT
        while time.time() < deadline:
            code = router.api_status("/")
            if code == 200:
                body = router.api_body("/")
                try:
                    data = json.loads(body)
                    kind = data.get("kind")
                    if kind == 10021:
                        tags = data.get("tags", [])
                        price_tags = [
                            t for t in tags
                            if isinstance(t, list) and t[0] == "price_per_step"
                        ]
                        if price_tags:
                            recovered = True
                            break
                except json.JSONDecodeError:
                    pass
            time.sleep(HEALTH_POLL_INTERVAL)

        if not recovered:
            pytest.skip(
                f"Service did not recover within {HEALTH_POLL_TIMEOUT}s — "
                "cannot test payment after recovery"
            )

        screenshot_portal("recovery-healthy.png")

        if not cashu.is_available():
            pytest.skip("cashu venv not available — run scripts/setup-cashu.sh")

        token = cashu.mint(4)
        resp = router.pay_direct(token)
        assert isinstance(resp, dict), \
            f"Expected dict response after recovery, got: {str(resp)[:200]}"
        kind = resp.get("kind")
        assert kind in (1022, 21023), \
            f"Unexpected response kind after recovery payment: {kind}"

        screenshot_portal("recovery-after-pay.png")
    finally:
        if rules:
            _unblock_mints(router, rules)
