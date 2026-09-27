import re
import time
import json
import logging

import pytest

from lib.constants import PING_HOST

log = logging.getLogger("tollgate.helpers")


def skip_if_no_mint_health_tracker(router, lines: int = 2000) -> None:
    """Skip if backend lacks MintHealthTracker (degraded mode unsupported).

    MintHealthTracker (merged via PR #139/#140, present in all modern Go and Rust
    backends) emits 'RunInitialProbe' at startup and 'runProactiveCheck' every 5min.
    Either signal in recent syslog confirms the feature is loaded.

    Replaces the pre-2026-06 status-JSON probe that checked for
    'degraded|reachable|mint_health' keywords. Those strings were never present
    in ServiceStatus / handleHealthCommand, so the old gate always skipped even
    on backends that fully supported the feature.
    """
    try:
        logs = router.get_tollgate_logs(lines=lines)
    except Exception as exc:
        pytest.skip(f"cannot read tollgate logs: {exc}")
    if not re.search(r"RunInitialProbe|runProactiveCheck|MintHealthTracker", logs):
        pytest.skip("backend lacks MintHealthTracker (degraded mode unsupported)")


def is_session_event(resp: dict) -> bool:
    if not isinstance(resp, dict):
        return False
    tags = resp.get("tags", [])
    return resp.get("kind") == 1022 or any(
        isinstance(t, list) and len(t) > 0 and t[0] == "allotment" for t in tags
    )


def is_mac_lookup_failure(resp: dict) -> bool:
    if resp.get("kind") != 21023:
        return False
    tags = resp.get("tags", [])
    return any(isinstance(t, list) and len(t) >= 2 and t[0] == "code"
               and "mac-address-lookup-failed" in t[1] for t in tags)


def is_payment_swap_succeeded(resp: dict) -> bool:
    """Check if the payment SWAP succeeded, even if gate-open failed.

    Distinguishes between:
    - Token parsing failure (invalid V3 token, NUT02 ID length)
    - Swap failure at mint (token_already_spent, not accepted)
    - Payment PROCESSED but MAC auth failed (gate-open: exit status)
    - Full success (kind=1022 session event)

    Returns True for the last two cases (swap succeeded).
    """
    if not isinstance(resp, dict):
        return False
    if is_session_event(resp):
        return True
    if is_mac_lookup_failure(resp):
        return True
    body = json.dumps(resp)
    token_failures = [
        "NUT02", "ID length invalid", "invalid V3 token",
        "invalid token", "not accepted", "token_already_spent",
    ]
    if any(f in body for f in token_failures):
        return False
    gate_open_failures = [
        "failed to open gate", "session-error",
    ]
    return any(f in body for f in gate_open_failures)


def require_client_identity(router):
    if not router.phone_ip and not router.phone_mac:
        pytest.skip("Set TOLLGATE_CLIENT_IP/TOLLGATE_CLIENT_MAC or run a phone client on the TollGate AP")


def parse_json_or_fail(text, label="response", skip=False):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if skip:
            pytest.skip(f"Non-JSON {label}: {text[:200]}")
        else:
            pytest.fail(f"Non-JSON {label}: {text[:200]}")


def assert_session_active(router, ip=None):
    session = router.get_session(ip)
    if session.get("session_active") is True or "allotment" in str(session):
        return session
    if router.get_nds_state() == "Authenticated":
        return session
    raise AssertionError(f"Session not active: {str(session)[:200]}")


def assert_deauthenticated(router, mac=None):
    state = router.get_nds_state(mac)
    assert state != "Authenticated", f"Client still authenticated (state: {state})"


def pay_and_wait(router, adb, token, timeout=60, wake=True):
    if wake:
        adb.wake_and_unlock()
        time.sleep(2)
    resp = router.pay_direct(token)
    log.info(f"pay_direct: kind={resp.get('kind')}, tags={str(resp.get('tags', []))[:200]}")
    assert is_session_event(resp), \
        f"Payment failed: {str(resp)[:200]}"
    assert router.wait_for_auth(timeout=timeout), \
        f"Not authenticated after {timeout}s"
    return resp


def assert_internet(adb, host=PING_HOST, retries=3):
    for i in range(retries):
        if adb.ping(host):
            return True
        time.sleep(1)
    return False


def pay_expire_cutoff(router, adb, cashu, amount=3):
    token = cashu.mint(amount)
    pay_and_wait(router, adb, token)
    assert assert_internet(adb), "No internet during active session"
    return wait_expiry_and_verify_cutoff(router, adb), token


def wait_expiry_and_verify_cutoff(router, adb, post_expiry_wait=5):
    elapsed = router.wait_for_session_expiry()
    time.sleep(post_expiry_wait)

    for attempt in range(8):
        state = router.get_nds_state()
        if state != "Authenticated":
            log.info(f"ndsctl confirmed deauth at {elapsed + attempt}s (state={state})")
            break
        time.sleep(1)
    assert state != "Authenticated", \
        f"Client still authenticated after expiry (state: {state})"

    return _verify_internet_cutoff(router, adb, elapsed)


def _verify_internet_cutoff(router, adb, elapsed):
    gateway = router.gateway_ip
    for attempt in range(4):
        if not adb.ping(gateway, interface="wlan0"):
            log.info(f"Internet confirmed cut off via wlan0 at attempt {attempt}")
            return elapsed
        time.sleep(2)

    if not adb.ping(PING_HOST):
        log.info(f"Ping to {PING_HOST} failed — internet cut off (unbound ping)")
        return elapsed

    raise AssertionError("Internet still accessible after session expiry (wlan0 + unbound ping)")


def metering_test_setup(router, adb, wifi, cashu, test_pricing_fn,
                        amount, step_size, metric):
    router.reset_state(adb=adb)
    test_pricing_fn(step_size=step_size, metric=metric)
    token = cashu.mint(amount)
    assert wifi.reconnect(skip_portal=True), f"WiFi reconnect failed for {metric} metering test"
    router.resolve_phone_client(adb)
    resp = pay_and_wait(router, adb, token)

    allotment = 0
    for tag in resp.get("tags", []):
        if isinstance(tag, list) and tag[0] == "allotment":
            allotment = int(tag[1])
    assert allotment > 0, f"Allotment is 0: {resp}"

    gross_expected = amount * step_size
    assert allotment <= gross_expected, \
        f"Allotment {allotment}{metric[0]} exceeds gross expected {gross_expected}{metric[0]}"

    return {"allotment": allotment, "metric": metric}, token


def post_payment_event(router, token):
    from lib.nostr import payment_event
    event = payment_event(token)
    body = json.dumps(event, separators=(",", ":"))
    return router.ssh(
        f"curl -s -X POST '{router.backend_url('/')}' "
        f"-H 'Content-Type: application/json' "
        f"-d '{body}'"
    )


def skip_if_no_cli_socket(router):
    try:
        out = router.ssh("ls -S /var/run/tollgate.sock 2>/dev/null", timeout=5)
        if not out.strip():
            pytest.skip("No CLI socket at /var/run/tollgate.sock")
    except Exception:
        pytest.skip("Cannot check CLI socket")


def skip_if_no_luci(router):
    try:
        code = router.ssh(
            "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/ 2>/dev/null",
            timeout=5,
        )
        if not code.strip().startswith("2"):
            pytest.skip("LuCI admin UI not available on port 8080")
    except Exception:
        pytest.skip("Cannot check LuCI availability")


INCIDENTS_URL = "https://github.com/OpenTollGate/tollgate-knowledgebase/tree/main/incidents"


def gate_bug_fix(fix_present, *, bug_id="", fix_pr=""):
    """Gate a test on the presence of a bug fix in the deployed firmware.

    If ``fix_present`` is False, marks the test as xfail (appears as
    "known issue" in reports).  If True, does nothing — the test runs
    normally and any failure is a real regression.

    Usage (inline probe)::

        from lib.helpers import gate_bug_fix

        def test_something(router):
            gate_bug_fix(
                _has_profit_share_validation(router),
                bug_id="profit-share-no-validation",
                fix_pr="PR #86",
            )
            # ... test body ...

    Usage (session-scoped, avoids repeating the probe)::

        @pytest.fixture(scope="session")
        def _validation_available(router):
            return _has_profit_share_validation(router)

        @pytest.fixture(autouse=True)
        def _gate(request, _validation_available):
            if request.node.originalname in _MUTATING_TESTS and not _validation_available:
                gate_bug_fix(
                    _validation_available,
                    bug_id="profit-share-no-validation",
                    fix_pr="PR #86",
                )

    Incident cross-reference — link to reported bugs in the knowledgebase::

        See: https://github.com/OpenTollGate/tollgate-knowledgebase/tree/main/incidents/2026-05-01-crypto-rand.md

    Args:
        fix_present: Boolean — True if the fix is confirmed in firmware.
        bug_id: Human-readable identifier (e.g. "crypto-rand-passwords").
        fix_pr: PR or commit that fixes the bug (e.g. "PR #111").
    """
    if not fix_present:
        reason = f"{bug_id} not fixed — expected in {fix_pr}" if fix_pr else f"{bug_id} not fixed in this firmware"
        pytest.xfail(reason=reason)


def skip_if_no_sessions_json(router):
    try:
        out = router.ssh("ls /etc/tollgate/sessions.json 2>/dev/null", timeout=5)
        if not out.strip():
            pytest.skip("No /etc/tollgate/sessions.json (backend uses in-memory sessions)")
    except Exception:
        pytest.skip("Cannot check sessions.json")


def skip_if_no_quote_persistence(router):
    """Skip if backend binary lacks Lightning quote persistence code."""
    try:
        out = router.ssh(
            "strings /usr/bin/tollgate-wrt 2>/dev/null | grep -c 'loadLightningQuotesFromDisk'",
            timeout=10,
        )
        if out.strip() == "0":
            pytest.skip("Binary lacks loadLightningQuotesFromDisk (quote persistence not supported)")
    except Exception:
        pytest.skip("Cannot check quote persistence support")


def is_full_merchant(router) -> bool:
    code = router.api_status("/")
    if code != 200:
        return False
    body = router.api_body("/")
    try:
        data = json.loads(body)
        if data.get("kind") != 10021:
            return False
        tags = data.get("tags", [])
        return any(
            isinstance(t, list) and len(t) > 0 and t[0] == "price_per_step"
            for t in tags
        )
    except json.JSONDecodeError:
        return False


def is_degraded(router) -> bool:
    body = router.api_body("/")
    try:
        data = json.loads(body)
        return data.get("kind") == 21023
    except json.JSONDecodeError:
        return False


def wait_for_full_merchant(router, timeout=120, interval=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_full_merchant(router):
            return True
        time.sleep(interval)
    return False


def wait_for_degraded(router, timeout=120, interval=5):
    import re
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_degraded(router):
            return True
        logs = router.get_tollgate_logs(lines=500)
        if re.search(r"(degraded|no reachable mints|all mints unreachable)", logs, re.IGNORECASE):
            return True
        time.sleep(interval)
    return False


def get_mint_ip_map(router):
    """Resolve configured mint URLs to IPs via nslookup on the router.

    Returns dict mapping mint URL -> resolved IP address.
    Handles bare-IP mint URLs (e.g. ``http://10.99.99.2:8385``) by
    skipping DNS resolution — the hostname IS the IP.
    """
    import re as _re
    from urllib.parse import urlparse

    cfg_raw = router.ssh("cat /etc/tollgate/config.json")
    cfg = json.loads(cfg_raw)
    urls = [m["url"] for m in cfg.get("accepted_mints", []) if "url" in m]
    ip_map = {}
    for url in urls:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            continue
        # Bare IP — no DNS resolution needed
        if _re.match(r"^\d+\.\d+\.\d+\.\d+$", hostname):
            ip_map[url] = hostname
            continue
        out = router.ssh(f"nslookup {hostname} 2>/dev/null || echo FAILED")
        ips = _re.findall(r"Address:\s*(\d+\.\d+\.\d+\.\d+)", out)
        for ip in reversed(ips):
            if not ip.startswith("127."):
                ip_map[url] = ip
                break
    return ip_map


def _mint_url_port(url: str) -> int:
    """Port a mint actually listens on — blocking only 443 is a no-op for
    plain-http lab mints (e.g. http://10.99.99.2:8383)."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def block_mints(router, mint_ip_map):
    """Block all mint IPs via iptables OUTPUT REJECT on the mint's real port.
    Returns list of (url, ip, port) rules."""
    rules = []
    for url, ip in mint_ip_map.items():
        port = _mint_url_port(url)
        router.ssh(f"iptables -I OUTPUT -d {ip} -p tcp --dport {port} -j REJECT")
        rules.append((url, ip, port))
    return rules


def unblock_mints(router, rules):
    """Remove the OUTPUT REJECT rules created by block_mints()."""
    for url, ip, port in rules:
        router.ssh(f"iptables -D OUTPUT -d {ip} -p tcp --dport {port} -j REJECT"
                   f" 2>/dev/null || true")


def skip_if_no_ssl_cli(router):
    result = router.ssh("tollgate ssl status 2>&1 || true")
    if "unknown command" in result.lower() or "not found" in result.lower():
        pytest.skip("'tollgate ssl' subcommand not available")


def ssl_is_applied(router):
    result = router.ssh("tollgate ssl status 2>&1")
    return any(kw in result.lower() for kw in ("active", "applied", "installed", "configured")) and "not configured" not in result.lower()


def skip_if_no_openssl(router):
    if not router.ssh_bool("which openssl 2>/dev/null"):
        pytest.skip("openssl not available on router")


def get_uhttpd_cert(router):
    return router.ssh("uci get uhttpd.main.cert 2>/dev/null || echo NOT_SET").strip()


def get_uhttpd_key(router):
    return router.ssh("uci get uhttpd.main.key 2>/dev/null || echo NOT_SET").strip()


def get_lan_ip(router):
    return router.ssh("uci get network.lan.ipaddr 2>/dev/null || echo UNKNOWN").strip()


def get_hostname(router):
    return router.ssh("uci get system.@system[0].hostname 2>/dev/null || echo TollGate").strip()


def skip_if_no_degraded_support(router):
    """Skip if backend lacks MintHealthTracker (degraded mode unsupported).

    Delegates to :func:`skip_if_no_mint_health_tracker` which probes syslog
    for MintHealthTracker activity.  Kept as a public alias because many test
    files import this name directly.
    """
    skip_if_no_mint_health_tracker(router)
