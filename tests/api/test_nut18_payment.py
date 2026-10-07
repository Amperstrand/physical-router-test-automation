import base64
import json
import pytest

pytestmark = [pytest.mark.story("R2"),pytest.mark.api, pytest.mark.extended]


def _v1_server_running(router):
    try:
        out = router.ssh(
            "curl -s -o /dev/null -w '%{http_code}' "
            "http://127.0.0.1:2121/ 2>/dev/null || echo 000"
        ).strip()
        return out not in ("000", "")
    except Exception:
        return False


def _nut24_supported(router):
    try:
        out = router.ssh(
            "curl -s -o /dev/null -w '%{http_code}' "
            "http://127.0.0.1:2121/pay 2>/dev/null || echo 000"
        ).strip()
        return out == "402"
    except Exception:
        return False


def test_get_pay_returns_402_with_creqa(router):
    if not _v1_server_running(router):
        pytest.skip("v1 server not running on :2121")
    if not _nut24_supported(router):
        pytest.skip("NUT-24 not implemented (Go backend)")
    resp = router.ssh(
        "curl -s -D /tmp/hdr -o /tmp/body "
        "http://127.0.0.1:2121/pay 2>/dev/null; "
        "head -1 /tmp/hdr; echo '---'; grep -i x-cashu /tmp/hdr; echo '---'; cat /tmp/body"
    )
    lines = resp.strip().split("\n")
    status_line = lines[0] if lines else ""
    assert "402" in status_line, f"Expected 402, got: {status_line}"

    creqa = ""
    for line in lines:
        if line.lower().startswith("x-cashu:"):
            creqa = line.split(":", 1)[1].strip()
            break
    assert creqa.startswith("creqA"), f"Expected creqA prefix, got: {creqa[:30]}"

    b64_data = creqa[5:]
    cbor_bytes = base64.b64decode(b64_data)
    assert b"sat" in cbor_bytes, "CBOR payload should contain 'sat' unit"
    assert b"post" in cbor_bytes, "CBOR payload should contain 'post' transport"

    sep_idx = resp.find("---")
    if sep_idx >= 0:
        body_start = resp.find("---", sep_idx + 3)
        if body_start >= 0:
            body = resp[body_start + 3:].strip()
            try:
                data = json.loads(body)
                assert data.get("price") is not None
                assert data.get("unit") == "sat"
                assert isinstance(data.get("mints"), list)
            except json.JSONDecodeError:
                pass


def test_portal_page_has_qr_and_payment_form(router):
    if not _v1_server_running(router):
        pytest.skip("v1 server not running on :2121")
    if not _nut24_supported(router):
        pytest.skip("Portal QR code not implemented (Go backend)")
    html = router.ssh("curl -s http://127.0.0.1:2121/ 2>/dev/null")
    assert "<svg" in html, "Portal should contain SVG QR code"
    assert "Cashu" in html or "cashu" in html, "Portal should mention Cashu"
    assert "<form" in html, "Portal should have a payment form"
    assert "pay-btn" in html or "submit" in html.lower(), "Portal should have submit button"


def test_nut24_payment_via_x_cashu_header(router, cashu):
    if not _v1_server_running(router):
        pytest.skip("v1 server not running on :2121")
    token = cashu.mint(2)
    assert token, "Failed to mint Cashu token"

    mac = router.phone_mac or "de:54:4e:91:49:da"
    resp = router.ssh(
        f"curl -s -w '\\n%{{http_code}}' "
        f"--header 'X-Cashu: {token}' "
        f"'http://127.0.0.1:2121/pay?mac={mac}' 2>/dev/null"
    )
    parts = resp.rsplit("\n", 1)
    body = parts[0].strip() if len(parts) > 1 else ""
    code = parts[1].strip() if len(parts) > 1 else ""
    assert code == "200", f"Expected 200 after NUT-24 payment, got {code}: {body[:200]}"


def test_nut18_post_payment_creates_session(router, cashu):
    if not _v1_server_running(router):
        pytest.skip("v1 server not running on :2121")
    token = cashu.mint(2)
    assert token

    mac = router.phone_mac or "de:54:4e:91:49:da"
    ip = "10.99.99.100"
    resp = router.ssh(
        f"curl -s -w '\\n%{{http_code}}' "
        f"-H 'Content-Type: text/plain' "
        f"-H 'X-Forwarded-For: {ip}' "
        f"-H 'X-TollGate-MAC: {mac}' "
        f"-d '{token}' "
        f"http://127.0.0.1:2121/ 2>/dev/null"
    )
    parts = resp.rsplit("\n", 1)
    body = parts[0].strip() if len(parts) > 1 else ""
    code = parts[1].strip() if len(parts) > 1 else ""
    assert code == "200", f"Expected 200 after POST payment, got {code}: {body[:200]}"

    import time
    time.sleep(1)
    bal = router.ssh(
        f"curl -s -H 'X-Forwarded-For: {ip}' "
        f"-H 'X-TollGate-MAC: {mac}' "
        f"http://127.0.0.1:2121/balance 2>/dev/null"
    )
    try:
        data = json.loads(bal)
        remaining = data.get("remaining", data.get("allotment", 0))
        assert remaining > 0, f"Expected positive balance after payment, got: {bal[:200]}"
    except json.JSONDecodeError:
        pytest.fail(f"Invalid JSON from /balance: {bal[:200]}")


def test_creqa_decodes_to_valid_structure(router):
    if not _v1_server_running(router):
        pytest.skip("v1 server not running on :2121")
    if not _nut24_supported(router):
        pytest.skip("CREQA not implemented (Go backend)")
    resp = router.ssh(
        "curl -s -D /tmp/h2 -o /dev/null http://127.0.0.1:2121/pay 2>/dev/null; "
        "grep -i x-cashu /tmp/h2"
    )
    creqa = ""
    for line in resp.strip().split("\n"):
        if line.lower().startswith("x-cashu:"):
            creqa = line.split(":", 1)[1].strip()
            break
    assert creqa.startswith("creqA")

    b64_data = creqa[5:]
    raw = base64.b64decode(b64_data)

    assert raw[0] == 0xa7, f"Expected CBOR map(7), got 0x{raw[0]:02x}"
    assert b"TollGate" in raw or b"Toll" in raw, "Should contain description"
    assert b"http" in raw, "Should contain POST endpoint URL"
