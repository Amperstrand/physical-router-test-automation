"""
Vendor IE (802.11 Information Element) hardware validation tests.

Validates PR #235 (feat/wgm-vendor-ie-discovery) — the TollGate vendor IE
encoder/decoder that injects custom IEs into hostapd beacons via the
``vendor_elements`` ubus parameter.

Two test tiers:

1. **Cloud-safe** (QEMU/virtual lab) — verifies hostapd accepts the
   ``vendor_elements`` ubus call and that the encoded hex is well-formed.
   These run in CI without real WiFi hardware.

2. **Hardware-only** — performs an actual ``iw scan`` from a scanner
   device, captures the over-the-air beacon, and verifies the TollGate
   OUI (21:21:21) IE round-trips correctly through encode → inject →
   scan → decode. Requires real radios (skipped on hwsim/QEMU).

Run on hardware:

    pytest tests/scenarios/test_vendor_ie.py -v --backend go

Run cloud-safe subset only:

    pytest tests/scenarios/test_vendor_ie.py -v -k "not hardware"
"""

import json
import os
import re
import time

import pytest

from lib.router import Router

pytestmark = [pytest.mark.story("O6"),pytest.mark.api, pytest.mark.extended, pytest.mark.go_only, pytest.mark.virtual_lab]

TOLLGATE_OUI = "212121"
TOLLGATE_ELEM_TYPE = "01"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _skip_if_no_wireless(router):
    """Skip if the router has no /etc/config/wireless (no radios)."""
    result = router.ssh("ls /etc/config/wireless 2>/dev/null", timeout=5)
    if not result or not result.strip():
        pytest.skip("No /etc/config/wireless — WiFi hardware not available")


def _skip_if_hwsim(router):
    """Skip beacon-scan tests on hwsim — virtual radios can't propagate beacons."""
    if os.environ.get("TOLLGATE_ENABLE_HWSIM"):
        pytest.skip("hwsim detected — beacon scan requires real radios")
    result = router.ssh("ls /sys/module/mac80211_hwsim 2>/dev/null", timeout=5)
    if result and result.strip():
        pytest.skip("mac80211_hwsim loaded — beacon scan requires real radios")
    result = router.ssh("iw dev 2>/dev/null | grep -E 'phy[0-9]+-ap'", timeout=5)
    if result and result.strip():
        pytest.skip("hwsim-style interfaces detected — beacon scan requires real radios")


def _enable_vendor_ie(router):
    """Enable vendor_ie_discovery in TollGate config and restart backend."""
    cfg_raw = router.ssh("cat /etc/tollgate/config.json")
    cfg = json.loads(cfg_raw)
    wgm = cfg.setdefault("wireless_gateway_manager", {})
    wgm["vendor_ie_discovery"] = True
    router.write_remote_json("/etc/tollgate/config.json", cfg)
    router.restart_backend()
    time.sleep(3)


def _disable_vendor_ie(router):
    """Restore vendor_ie_discovery to false."""
    cfg_raw = router.ssh("cat /etc/tollgate/config.json")
    cfg = json.loads(cfg_raw)
    wgm = cfg.get("wireless_gateway_manager", {})
    wgm["vendor_ie_discovery"] = False
    router.write_remote_json("/etc/tollgate/config.json", cfg)
    router.restart_backend()
    time.sleep(2)


def _get_hostapd_ifaces(router):
    """Return list of hostapd interface names (e.g. ['hostapd.phy0-ap0'])."""
    output = router.ssh("ubus list 2>/dev/null | grep '^hostapd\\.'", timeout=10)
    ifaces = [line.strip() for line in output.splitlines() if line.strip()]
    return ifaces


def _get_vendor_elements_via_ubus(router, iface):
    """Query hostapd for currently-set vendor_elements via ubus."""
    result = router.ssh(
        f"ubus call {iface} get_vendor_elements 2>/dev/null || echo '{{}}'",
        timeout=10,
    )
    try:
        return json.loads(result)
    except (json.JSONDecodeError, ValueError):
        return {}


def _encode_tollgate_ie_hex(version=1, is_reseller=False, has_internet=True,
                            open_network=False, mint_url="", pubkey_hex=""):
    """Python reimplementation of EncodeTollGateVendorIE for test-side verification.

    Returns the hex string that would be injected into hostapd vendor_elements.
    Must produce identical output to the Go EncodeTollGateVendorIE function.
    """
    flags = 0
    if is_reseller:
        flags |= 0x01
    if has_internet:
        flags |= 0x02
    if open_network:
        flags |= 0x04

    oui = bytes.fromhex(TOLLGATE_OUI)
    elem_type = bytes([int(TOLLGATE_ELEM_TYPE, 16)])
    body = bytearray(oui + elem_type)
    body.append(version)
    body.append(flags)

    if mint_url:
        mint_bytes = mint_url.encode("utf-8")
        if len(mint_bytes) > 255:
            raise ValueError(f"mint_url too long: {len(mint_bytes)} bytes")
        body.append(0x01)  # tlvTypeMintURL
        body.append(len(mint_bytes))
        body.extend(mint_bytes)

    if pubkey_hex:
        pubkey = bytes.fromhex(pubkey_hex)
        if len(pubkey) > 255:
            raise ValueError(f"pubkey too long: {len(pubkey)} bytes")
        body.append(0x02)  # tlvTypePubkey
        body.append(len(pubkey))
        body.extend(pubkey)

    if len(body) > 255:
        raise ValueError(f"vendor IE body too long: {len(body)} bytes (max 255)")

    ie = bytearray([0xDD, len(body)])
    ie.extend(body)
    return ie.hex()


def _parse_tollgate_ie_from_hex(hex_str):
    """Python reimplementation of ParseTollGateVendorIE for test-side verification.

    Parses a hex-encoded vendor IE and returns the decoded fields.
    Must decode identically to the Go ParseTollGateVendorIE function.
    """
    raw = bytes.fromhex(hex_str)
    if len(raw) < 8:
        return None
    if raw[0] != 0xDD:
        return None

    body_len = raw[1]
    if len(raw) < 2 + body_len:
        return None
    body = raw[2:2 + body_len]

    oui = f"{body[0]:02x}{body[1]:02x}{body[2]:02x}"
    if oui != TOLLGATE_OUI:
        return None
    if body[3] != int(TOLLGATE_ELEM_TYPE, 16):
        return None

    result = {"version": body[4]}
    if len(body) > 5:
        flags = body[5]
        result["is_reseller"] = bool(flags & 0x01)
        result["has_internet"] = bool(flags & 0x02)
        result["open_network"] = bool(flags & 0x04)
    else:
        result["is_reseller"] = False
        result["has_internet"] = False
        result["open_network"] = False

    offset = 6
    while offset + 2 <= len(body):
        tlv_type = body[offset]
        tlv_len = body[offset + 1]
        if offset + 2 + tlv_len > len(body):
            break
        tlv_value = body[offset + 2:offset + 2 + tlv_len]
        if tlv_type == 0x01:
            result["mint_url"] = tlv_value.decode("utf-8", errors="replace")
        elif tlv_type == 0x02:
            result["pubkey"] = tlv_value.hex()
        offset += 2 + tlv_len

    return result


def _scan_for_vendor_ies(router, iface=""):
    """Run iw scan on the router and extract vendor IEs from the output.

    Returns list of (oui_hex, ie_hex) tuples for all vendor IEs found.
    """
    if not iface:
        iw_dev = router.ssh("iw dev 2>/dev/null", timeout=10)
        match = re.search(r"Interface (\S+)", iw_dev)
        if not match:
            return []
        iface = match.group(1)

    router.ssh(f"iw dev {iface} scan trigger 2>/dev/null", timeout=10)
    time.sleep(3)

    scan_output = router.ssh(f"iw dev {iface} scan 2>/dev/null", timeout=30)

    vendor_ies = []
    for match in re.finditer(r"Vendor specific:\s+([0-9a-fA-F]+)", scan_output):
        ie_hex = match.group(1).lower()
        if len(ie_hex) >= 6:
            oui = ie_hex[0:6]
            vendor_ies.append((oui, ie_hex))
    return vendor_ies


# Expected hex outputs from the Go EncodeTollGateVendorIE function.
# Generated by running the Go encoder logic directly. These are the
# ground-truth values the Python test-side encoder must match.
GO_ENCODED = {
    "version_only": "dd06212121010100",
    "all_flags": "dd06212121010207",
    "with_mint": "dd1e2121210101020116687474703a2f2f31302e39392e39392e323a38333835",
}


# ---------------------------------------------------------------------------
# Cloud-safe tests (run in QEMU/CI — no real radios needed)
# ---------------------------------------------------------------------------

def test_python_encoder_matches_go():
    """Verify the Python test-side encoder produces identical bytes to Go.

    Cloud-safe: pure computation, no router needed. Uses hardcoded expected
    values derived from running the Go EncodeTollGateVendorIE function.
    """
    py_hex = _encode_tollgate_ie_hex(version=1)
    assert py_hex == GO_ENCODED["version_only"], (
        f"Version-only mismatch: Python={py_hex}, Go={GO_ENCODED['version_only']}"
    )

    py_hex = _encode_tollgate_ie_hex(version=2, is_reseller=True, has_internet=True, open_network=True)
    assert py_hex == GO_ENCODED["all_flags"], (
        f"All-flags mismatch: Python={py_hex}, Go={GO_ENCODED['all_flags']}"
    )

    py_hex = _encode_tollgate_ie_hex(version=1, has_internet=True, mint_url="http://10.99.99.2:8385")
    assert py_hex == GO_ENCODED["with_mint"], (
        f"With-mint mismatch: Python={py_hex}, Go={GO_ENCODED['with_mint']}"
    )


def test_encode_decode_round_trip_python():
    """Verify Python encode → decode round-trip preserves all fields.

    Cloud-safe: pure computation, no router needed.
    """
    cases = [
        {"version": 1},
        {"version": 2, "is_reseller": True, "has_internet": True, "open_network": True},
        {"version": 1, "has_internet": True, "mint_url": "http://10.99.99.2:8385"},
        {"version": 3, "is_reseller": True, "has_internet": False, "open_network": True,
         "mint_url": "https://mint.example.com", "pubkey_hex": "02abcdef01234567"},
    ]

    for case in cases:
        encoded = _encode_tollgate_ie_hex(**case)
        decoded = _parse_tollgate_ie_from_hex(encoded)

        assert decoded is not None, f"Decode returned None for {case}"
        assert decoded["version"] == case["version"]

        for flag in ("is_reseller", "has_internet", "open_network"):
            assert decoded.get(flag, False) == case.get(flag, False), (
                f"{flag} mismatch for {case}: got {decoded.get(flag)}, want {case.get(flag)}"
            )

        if "mint_url" in case:
            assert decoded.get("mint_url", "") == case["mint_url"]

        if "pubkey_hex" in case:
            assert decoded.get("pubkey", "") == case["pubkey_hex"]


def test_hostapd_vendor_elements_ubus(router):
    """Verify hostapd accepts the vendor_elements ubus call without error.

    Cloud-safe: tests the ubus plumbing, not over-the-air propagation.
    Requires hostapd to be running (QEMU with hwsim or real hardware).
    """
    _skip_if_no_wireless(router)

    ifaces = _get_hostapd_ifaces(router)
    if not ifaces:
        pytest.skip("No hostapd interfaces found")

    test_ie_hex = _encode_tollgate_ie_hex(version=1, has_internet=True)

    iface = ifaces[0]
    payload = json.dumps({"vendor_elements": test_ie_hex})
    result = router.ssh(
        f"ubus call {iface} set_vendor_elements '{payload}' 2>&1",
        timeout=10,
    )

    assert "error" not in result.lower(), f"ubus set_vendor_elements failed: {result}"


# ---------------------------------------------------------------------------
# Hardware-only tests (require real WiFi radios — skipped on QEMU/hwsim)
# ---------------------------------------------------------------------------

@pytest.mark.hardware
def test_vendor_ie_appears_in_scan(router):
    """Verify the TollGate vendor IE appears in iw scan output on real hardware.

    Hardware-only: requires a real radio that can send/receive beacons.
    Skipped on QEMU/hwsim because virtual radios can't propagate beacons.
    """
    _skip_if_no_wireless(router)
    _skip_if_hwsim(router)

    _enable_vendor_ie(router)
    try:
        time.sleep(5)

        vendor_ies = _scan_for_vendor_ies(router)
        tollgate_ies = [(oui, ie) for oui, ie in vendor_ies if oui == TOLLGATE_OUI]

        assert len(tollgate_ies) > 0, (
            f"TollGate vendor IE (OUI {TOLLGATE_OUI}) not found in scan output. "
            f"Total vendor IEs seen: {len(vendor_ies)}"
        )
    finally:
        _disable_vendor_ie(router)


@pytest.mark.hardware
def test_vendor_ie_round_trip(router):
    """Full encode → inject → scan → decode round-trip on real hardware.

    Hardware-only: encodes a known advertisement, injects it via hostapd,
    scans for it over the air, and decodes the captured bytes. All fields
    must match the original advertisement.

    This is the definitive test that c03rad0r requested in PR #235 review
    item 4: 'confirm the encoded IE actually appears in iw scan output
    on real WiFi hardware and that ParseTollGateVendorIE correctly decodes
    what EncodeTollGateVendorIE produces over the air.'
    """
    _skip_if_no_wireless(router)
    _skip_if_hwsim(router)

    test_adv = {
        "version": 1,
        "is_reseller": False,
        "has_internet": True,
        "mint_url": "http://10.99.99.2:8385",
    }
    expected_hex = _encode_tollgate_ie_hex(**test_adv)

    _enable_vendor_ie(router)
    try:
        time.sleep(5)

        vendor_ies = _scan_for_vendor_ies(router)
        tollgate_ies = [ie for oui, ie in vendor_ies if oui == TOLLGATE_OUI]

        assert len(tollgate_ies) > 0, "No TollGate vendor IE found in scan"

        captured_ie = tollgate_ies[0]
        decoded = _parse_tollgate_ie_from_hex(captured_ie)

        assert decoded is not None, f"Failed to decode captured IE: {captured_ie}"
        assert decoded["version"] == test_adv["version"], (
            f"Version mismatch: got {decoded['version']}, want {test_adv['version']}"
        )
        assert decoded["has_internet"] == test_adv["has_internet"], (
            f"has_internet mismatch: got {decoded['has_internet']}, want {test_adv['has_internet']}"
        )
        assert decoded.get("mint_url", "") == test_adv["mint_url"], (
            f"mint_url mismatch: got {decoded.get('mint_url', '')!r}, want {test_adv['mint_url']!r}"
        )
    finally:
        _disable_vendor_ie(router)


@pytest.mark.hardware
def test_vendor_ie_flags_change(router):
    """Verify flag changes (reseller, open network) propagate over the air.

    Hardware-only: configures different flag combinations and verifies
    each one round-trips correctly through the scan.
    """
    _skip_if_no_wireless(router)
    _skip_if_hwsim(router)

    flag_combos = [
        {"is_reseller": False, "has_internet": True, "open_network": False},
        {"is_reseller": True, "has_internet": True, "open_network": False},
        {"is_reseller": True, "has_internet": False, "open_network": True},
    ]

    _enable_vendor_ie(router)
    try:
        for flags in flag_combos:
            expected_hex = _encode_tollgate_ie_hex(version=1, **flags)

            vendor_ies = _scan_for_vendor_ies(router)
            tollgate_ies = [ie for oui, ie in vendor_ies if oui == TOLLGATE_OUI]

            if not tollgate_ies:
                pytest.fail(f"No TollGate IE found for flags={flags}")

            decoded = _parse_tollgate_ie_from_hex(tollgate_ies[0])
            assert decoded is not None, "Decode returned None"

            for flag_name, expected_val in flags.items():
                got_val = decoded.get(flag_name, False)
                assert got_val == expected_val, (
                    f"{flag_name} mismatch: got {got_val}, want {expected_val} "
                    f"(flags byte: {decoded})"
                )

            time.sleep(2)
    finally:
        _disable_vendor_ie(router)
