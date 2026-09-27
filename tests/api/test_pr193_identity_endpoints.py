"""Verify identity derivation endpoints (PR #193 lineage, issue #203).

Current firmware contract (tmbg main, ``handleIdentityRevealSeed``):

* ``GET /identity``                -> public attributes of the merchant key in
  identities.json: ``{npub, ipv4, macs}``. Registered only when a merchant
  private key exists (404 otherwise — pre-#193 firmware).
* ``POST /identity/reveal-seed``   -> loopback-only **derivation oracle**: the
  POST body is a raw 12-word BIP39 mnemonic (not JSON), and the response is
  the full identity derived from it via NIP-06 —
  ``{npub, ipv4, macs, mnemonic, privatekey, root_password, wifi_password}``.
  It no longer reveals the router's own stored seed: an empty or invalid
  body gets ``400 invalid mnemonic`` (this recontracting is PRTA #102).
  Non-POST -> 405; non-loopback -> 403.

History: the PR #193-era endpoint revealed the stored identity
unconditionally; the API was later hardened into the mnemonic-in/oracle-out
form above. Tests mint their own standard BIP39 test vectors and verify the
derivation, its determinism, and the rejection paths.

See: https://github.com/OpenTollGate/tollgate-module-basic-go/issues/203
      https://github.com/OpenTollGate/physical-router-test-automation/issues/102
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

import pytest

from lib.constants import BACKEND_PORT
from lib.helpers import gate_bug_fix

pytestmark = [pytest.mark.api, pytest.mark.extended, pytest.mark.virtual_lab]

# Standard BIP39 12-word test vectors (128-bit entropy, valid checksums).
MNEMONIC_A = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
MNEMONIC_B = "legal winner thank year wave sausage worth useful legal winner thank yellow"

_NPUB_RE = re.compile(r"^npub1[023456789acdefghjklmnpqrstuvwxyz]{6,}$")
_IPV4_RE = re.compile(r"^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.1$")
_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
# DeriveRootPassword/DeriveWiFiPassword (v2): six lowercase BIP39 words,
# hyphen-joined (supersedes the v1 Nato-Nato-Nato-NN / Nato-Nato-NNNN format)
_SIX_WORD_RE = re.compile(r"^[a-z]+(-[a-z]+){5}$")
_ROOTPW_RE = _SIX_WORD_RE
_WIFIPW_RE = _SIX_WORD_RE


def _http_call(router, path: str, method: str = "GET", timeout: int = 15) -> tuple[int, str]:
    """Call the backend directly from the test runner (Debian client → OpenWrt)."""
    url = f"http://{router.host}:{BACKEND_PORT}{path}"
    req = urllib.request.Request(url, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)


def _http_loopback_post(router, path: str, body: str = "", content_type: str = "text/plain",
                        timeout: int = 15) -> tuple[int, str]:
    """POST to the loopback address from inside the OpenWrt VM via SSH.

    Required for loopback-only endpoints like /identity/reveal-seed. Uses
    curl with an explicit content-type (curl is a lab test dep): since
    the CORS hardening (tmbg cd7a937) the backend answers 415 to the form
    content-type busybox wget sends, which used to leak wget's error text
    into callers as a JSONDecodeError. The HTTP status is appended via -w
    so non-2xx answers fail with a real status code, not a parse error.
    """
    url = f"http://[::1]:{BACKEND_PORT}{path}"
    raw = router.ssh(
        f"curl -s --max-time {timeout} -X POST -H 'Content-Type: {content_type}' "
        f"--data-binary '{body}' -w '\\n%{{http_code}}' '{url}' 2>&1 || true",
        timeout=timeout + 10,
    ).strip()
    parts = raw.rsplit("\n", 1)
    code = parts[1].strip() if len(parts) == 2 and parts[1].strip().isdigit() else "0"
    body_text = parts[0].strip()
    return int(code), body_text


def _identity_present(router) -> bool:
    """True iff GET /identity returns 200 JSON with an npub (PR #193 firmware)."""
    status, body = _http_call(router, "/identity")
    if status != 200:
        return False
    try:
        return bool(json.loads(body).get("npub"))
    except json.JSONDecodeError:
        return False


@pytest.fixture(autouse=True)
def _gate_identity(router):
    gate_bug_fix(
        _identity_present(router),
        bug_id="identity-password-derivation-pre-193",
        fix_pr="PR #193",
    )


@pytest.fixture(scope="module")
def public_identity(router):
    if not _identity_present(router):
        pytest.skip("PR #193 /identity endpoint not present on this firmware")
    status, body = _http_call(router, "/identity")
    assert status == 200, f"GET /identity returned {status}: {body[:200]}"
    return json.loads(body)


@pytest.fixture(scope="module")
def derived_identity(router):
    """Full identity derived by reveal-seed from MNEMONIC_A."""
    status, body = _http_loopback_post(router, "/identity/reveal-seed", body=MNEMONIC_A)
    if status == 404:
        pytest.skip("reveal-seed route not registered on this firmware")
    assert status == 200, f"POST /identity/reveal-seed returned {status}: {body[:200]}"
    return json.loads(body)


@pytest.mark.extended
def test_identity_public_attributes(public_identity):
    """GET /identity returns well-formed npub, IPv4 (CGNAT range), and MACs."""
    assert _NPUB_RE.match(public_identity["npub"]), f"bad npub: {public_identity.get('npub')}"
    assert _IPV4_RE.match(public_identity["ipv4"]), f"bad ipv4: {public_identity.get('ipv4')}"
    macs = public_identity.get("macs", {})
    assert macs, "/identity returned no MACs"
    for iface, mac in macs.items():
        assert _MAC_RE.match(mac), f"bad MAC for {iface}: {mac}"


@pytest.mark.extended
def test_reveal_seed_derives_full_identity(derived_identity):
    """reveal-seed derives a well-formed identity from the posted mnemonic."""
    assert derived_identity.get("mnemonic") == MNEMONIC_A, "mnemonic not echoed back"
    priv = derived_identity.get("privatekey", "")
    assert re.fullmatch(r"[0-9a-f]{64}", priv.lower()), f"privatekey not 64 hex: {priv[:8]}..."
    assert _NPUB_RE.match(derived_identity.get("npub", "")), f"bad npub: {derived_identity.get('npub')}"
    assert _IPV4_RE.match(derived_identity.get("ipv4", "")), f"bad ipv4: {derived_identity.get('ipv4')}"
    macs = derived_identity.get("macs", {})
    assert macs, "derived identity has no MACs"
    for iface, mac in macs.items():
        assert _MAC_RE.match(mac), f"bad MAC for {iface}: {mac}"


@pytest.mark.extended
def test_reveal_seed_is_deterministic(router, derived_identity):
    """Same mnemonic -> same derived key and passwords across calls (issue #203 #4)."""
    status, body = _http_loopback_post(router, "/identity/reveal-seed", body=MNEMONIC_A)
    assert status == 200, f"second POST returned {status}: {body[:200]}"
    again = json.loads(body)
    assert again["privatekey"] == derived_identity["privatekey"], "privatekey not deterministic"
    assert again["root_password"] == derived_identity["root_password"], "root_password not deterministic"
    assert again["wifi_password"] == derived_identity["wifi_password"], "wifi_password not deterministic"


@pytest.mark.extended
def test_reveal_seed_reflects_posted_mnemonic(router, derived_identity):
    """A different mnemonic derives a different identity — the endpoint must
    not collapse into an unconditional reveal of the stored key (the pre-hardening
    behavior whose regression this test guards)."""
    status, body = _http_loopback_post(router, "/identity/reveal-seed", body=MNEMONIC_B)
    assert status == 200, f"POST with second mnemonic returned {status}: {body[:200]}"
    other = json.loads(body)
    assert other["mnemonic"] == MNEMONIC_B, "mnemonic B not echoed back"
    assert other["privatekey"] != derived_identity["privatekey"], \
        "different mnemonic derived the same privatekey — endpoint is not deriving from the body"
    assert other["npub"] != derived_identity["npub"], "different mnemonic derived the same npub"


@pytest.mark.extended
def test_passwords_formatted_correctly(derived_identity):
    """Passwords match the NATO-word format and are non-empty."""
    rp = derived_identity.get("root_password", "")
    wp = derived_identity.get("wifi_password", "")
    assert rp, "root_password empty"
    assert wp, "wifi_password empty"
    assert _ROOTPW_RE.match(rp), f"root_password malformed: {rp!r}"
    assert _WIFIPW_RE.match(wp), f"wifi_password malformed: {wp!r}"


@pytest.mark.extended
def test_reveal_seed_rejects_invalid_mnemonic(router):
    """Invalid or empty mnemonics get 400 'invalid mnemonic' (the hardened contract)."""
    for bad_body in ("not a mnemonic", ""):
        status, body = _http_loopback_post(router, "/identity/reveal-seed", body=bad_body)
        assert status == 400, f"body {bad_body!r}: expected 400, got {status}: {body[:200]}"
        assert "invalid mnemonic" in body.lower(), f"body {bad_body!r}: unexpected error text: {body[:200]}"


@pytest.mark.extended
def test_reveal_seed_requires_post(router):
    """GET on the loopback reveal-seed route is 405 Method Not Allowed."""
    raw = router.ssh(
        f"curl -s --max-time 15 -o /dev/null -w '%{{http_code}}' 'http://[::1]:{BACKEND_PORT}/identity/reveal-seed'",
        timeout=25,
    ).strip()
    assert raw == "405", f"GET /identity/reveal-seed on loopback should 405, got {raw}"


@pytest.mark.extended
def test_reveal_seed_rejects_non_loopback(router):
    """POST /identity/reveal-seed from a non-loopback origin is forbidden."""
    status, body = _http_call(router, "/identity/reveal-seed", method="POST")
    assert status in (403, 405), f"reveal-seed not protected from non-loopback: status={status}"
    assert "mnemonic" not in body.lower(), "mnemonic leaked to non-loopback request"
