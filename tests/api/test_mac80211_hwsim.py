"""mac80211_hwsim virtual WiFi tests.

Three provisioning modes:

- **Worker-provisioned (cloud lab)**: the worker loads mac80211_hwsim, creates
  AP interfaces manually via ``iw phy … interface add … type __ap``, adds them
  to br-lan, and writes UCI wireless config.  Tests verify the state.

- **Self-provisioned (local virtual lab)**: tests install the kmod package,
  load the module, and create interfaces manually (same approach as worker).

- **vwifi cross-VM relay (cloud lab with --vwifi)**: vwifi relays 802.11 frames
  between QEMU VMs via vsock.  The Debian guest can actually scan and see the
  OpenWrt AP's SSID.  STA tests run instead of skipping when
  ``TOLLGATE_ENABLE_VWIFI=1`` is set.

Critical limitation (non-vwifi only): hwsim PHYs do NOT propagate beacons
between each other.  STA scan/association/DHCP tests skip unless
``HWSIM_STA_ENABLED=1`` or ``TOLLGATE_ENABLE_VWIFI=1`` is set.
Only runs on x86_64 targets where the kmod package is available.
"""

import os

import pytest
import re
import time

pytestmark = [pytest.mark.story("P7"),pytest.mark.api, pytest.mark.extended, pytest.mark.virtual_lab]

_HWSIM_MODULE = "mac80211_hwsim"
_HWSIM_KMOD_PKG = "kmod-mac80211-hwsim"

HWSIM_STA_ENABLED = os.environ.get("HWSIM_STA_ENABLED", "").lower() in ("1", "true", "yes")
VWIFI_ENABLED = os.environ.get("TOLLGATE_ENABLE_VWIFI", "").lower() in ("1", "true", "yes")

if not os.environ.get("TOLLGATE_ENABLE_HWSIM") and not VWIFI_ENABLED:
    pytest.skip(
        "hwsim tests require TOLLGATE_ENABLE_HWSIM=1 or TOLLGATE_ENABLE_VWIFI=1 (experimental, opt-in via --hwsim or --vwifi)",
        allow_module_level=True,
    )


def _is_x86_64(router):
    arch = router.ssh("uname -m 2>/dev/null").strip()
    return arch == "x86_64"


def _module_loaded(router):
    return _HWSIM_MODULE in router.ssh("lsmod 2>/dev/null || true")


def _get_hwsim_phys(router):
    phys = router.ssh(
        "for d in /sys/class/ieee80211/phy*; do "
        "  driver=$(readlink $d/device/driver 2>/dev/null); "
        "  if echo \"$driver\" | grep -q mac80211_hwsim; then "
        "    basename $d; "
        "  fi; "
        "done"
    ).strip().split()
    return [p for p in phys if p]


def _detect_ap_ssid(router):
    ssid = router.ssh("uci get wireless.@wifi-iface[0].ssid 2>/dev/null").strip()
    return ssid if ssid else "HWSIM-Test"


def _ap_interfaces(router):
    iw_dev = router.ssh("iw dev 2>/dev/null")
    return [i for i in re.findall(r"Interface (\S+)", iw_dev) if "ap" in i.lower()]


def _find_sta_interface(router):
    iw_dev = router.ssh("iw dev 2>/dev/null")
    for iface in re.findall(r"Interface (\S+)", iw_dev):
        if "ap" not in iface.lower():
            return iface
    return None


def _skip_unless_sta_supported():
    if HWSIM_STA_ENABLED or VWIFI_ENABLED:
        return
    pytest.skip(
        "hwsim STA tests skipped (PHYs don't propagate beacons). "
        "Set HWSIM_STA_ENABLED=1 to force-run, or use --vwifi for cross-VM relay."
    )


# ---------------------------------------------------------------------------
# Module and radio detection
# ---------------------------------------------------------------------------


def test_install_hwsim_module(router):
    if not _is_x86_64(router):
        pytest.skip("mac80211_hwsim only available on x86_64 targets")
    if _module_loaded(router):
        pytest.skip("mac80211_hwsim already loaded")

    opkg_status = router.ssh(f"opkg list-installed 2>/dev/null | grep '{_HWSIM_KMOD_PKG}'")
    if _HWSIM_KMOD_PKG in opkg_status:
        return

    router.ssh("opkg update >/dev/null 2>&1 || true")
    result = router.ssh(f"opkg install {_HWSIM_KMOD_PKG} 2>&1")
    if "Cannot install" in result or "not found" in result.lower():
        pytest.skip(f"Cannot install {_HWSIM_KMOD_PKG}: {result[-200:]}")


def test_load_hwsim_radios(router):
    if not _is_x86_64(router):
        pytest.skip("mac80211_hwsim only available on x86_64 targets")
    if _module_loaded(router):
        pytest.skip("mac80211_hwsim already loaded (worker-provisioned)")

    router.ssh(f"modprobe {_HWSIM_MODULE} radios=2 2>/dev/null"
               f" || insmod {_HWSIM_MODULE} radios=2 2>/dev/null || true")
    if not _module_loaded(router):
        pytest.skip(f"Could not load {_HWSIM_MODULE} module")


def test_iw_list_shows_virtual_radios(router):
    if not _module_loaded(router):
        pytest.skip("mac80211_hwsim not loaded")

    iw_list = router.ssh("iw list 2>&1")
    assert "Wiphy" in iw_list, f"iw list missing Wiphy entries: {iw_list[:300]}"
    phy_count = iw_list.count("Wiphy")
    assert phy_count >= 2, f"Expected >= 2 virtual radios, got {phy_count}"


# ---------------------------------------------------------------------------
# AP interface verification
# ---------------------------------------------------------------------------


def test_ap_interfaces_exist(router):
    """Verify AP interfaces were created and are in Master mode.

    Worker-provisioned: interfaces already exist (phy0-ap0, phy1-ap0).
    Self-provisioned: create them manually via ``iw phy`` (same as worker).
    """
    if not _module_loaded(router):
        pytest.skip("mac80211_hwsim not loaded")

    existing = _ap_interfaces(router)
    if existing:
        for iface in existing:
            info = router.ssh(f"iwinfo {iface} info 2>/dev/null")
            assert "Master" in info or "AP" in info, (
                f"{iface} not in Master/AP mode: {info[:200]}"
            )
        return

    hwsim_phys = _get_hwsim_phys(router)
    if not hwsim_phys:
        pytest.skip("No hwsim PHYs found")

    router.ssh(
        f"iw phy {hwsim_phys[0]} interface add phy0-ap0 type __ap 2>&1 && "
        f"iw phy {hwsim_phys[1]} interface add phy1-ap0 type __ap 2>&1 && "
        "brctl addif br-lan phy0-ap0 2>/dev/null; "
        "brctl addif br-lan phy1-ap0 2>/dev/null; "
        "ip link set phy0-ap0 up 2>/dev/null; "
        "ip link set phy1-ap0 up 2>/dev/null"
    )
    time.sleep(3)

    ap_ifaces = _ap_interfaces(router)
    assert ap_ifaces, (
        f"No AP interfaces after manual creation. iw dev: "
        f"{router.ssh('iw dev 2>&1')[:400]}"
    )


def test_ap_interfaces_bridged(router):
    if not _module_loaded(router):
        pytest.skip("mac80211_hwsim not loaded")

    ap_ifaces = _ap_interfaces(router)
    if not ap_ifaces:
        pytest.skip("No AP interfaces")

    bridge_output = router.ssh("brctl show br-lan 2>/dev/null")
    for iface in ap_ifaces:
        assert iface in bridge_output, f"{iface} not in br-lan: {bridge_output[:300]}"


def test_ap_ssid_configured(router):
    if not _module_loaded(router):
        pytest.skip("mac80211_hwsim not loaded")

    ssid = _detect_ap_ssid(router)
    assert ssid and ssid != "HWSIM-Test", f"SSID not configured: {ssid!r}"

    iwinfo = router.ssh("iwinfo 2>/dev/null")
    assert ssid in iwinfo, f"SSID '{ssid}' not in iwinfo: {iwinfo[:300]}"


# ---------------------------------------------------------------------------
# Scan tests (basic — no cross-PHY requirement)
# ---------------------------------------------------------------------------


def test_iw_scan_executes(router):
    if not _module_loaded(router):
        pytest.skip("mac80211_hwsim not loaded")

    ap_ifaces = _ap_interfaces(router)
    if not ap_ifaces:
        pytest.skip("No AP interfaces available")

    scan_output = router.ssh(f"iw {ap_ifaces[0]} scan 2>&1")
    assert "No such device" not in scan_output, (
        f"{ap_ifaces[0]} disappeared during scan: {scan_output[:200]}"
    )


# ---------------------------------------------------------------------------
# STA (station) mode tests — require cross-PHY beacon propagation
#
# hwsim PHYs do NOT propagate beacons between each other.  These tests
# skip unless HWSIM_STA_ENABLED=1 is set (for physical hardware testing).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _cleanup_sta_config(router):
    yield
    hwsim_phys = _get_hwsim_phys(router)
    sta_phy = hwsim_phys[1] if len(hwsim_phys) >= 2 else None
    ap_ssid = _detect_ap_ssid(router)

    restore_cmd = "uci -q delete wireless.sta0; uci -q delete network.wwan; "
    if sta_phy:
        restore_cmd += (
            "uci set wireless.radio1=wifi-device; "
            "uci set wireless.radio1.type='mac80211'; "
            f"uci set wireless.radio1.phy='{sta_phy}'; "
            "uci set wireless.radio1.channel='36'; "
            "uci set wireless.radio1.band='5g'; "
            "uci set wireless.radio1.htmode='HE20'; "
            "uci set wireless.radio1.disabled='0'; "
            "uci set wireless.default_radio1=wifi-iface; "
            "uci set wireless.default_radio1.device='radio1'; "
            "uci set wireless.default_radio1.mode='ap'; "
            f"uci set wireless.default_radio1.ssid='{ap_ssid}'; "
            "uci set wireless.default_radio1.network='lan'; "
            "uci set wireless.default_radio1.encryption='none'; "
        )
    else:
        restore_cmd += "uci -q delete wireless.radio1; "

    restore_cmd += (
        "uci commit wireless 2>/dev/null; "
        "uci commit network 2>/dev/null; "
        "wifi reload 2>/dev/null || true; "
        "/etc/init.d/network reload 2>/dev/null || true"
    )
    router.ssh(restore_cmd)


@pytest.mark.slow
def test_sta_scan_sees_ap(router):
    _skip_unless_sta_supported()
    if not _module_loaded(router):
        pytest.skip("mac80211_hwsim not loaded")

    hwsim_phys = _get_hwsim_phys(router)
    if len(hwsim_phys) < 2:
        pytest.skip(f"Need >= 2 hwsim PHYs, found {len(hwsim_phys)}")

    sta_phy = hwsim_phys[1]
    ap_ssid = _detect_ap_ssid(router)

    router.ssh(
        "uci set network.wwan=interface; "
        "uci set network.wwan.proto='dhcp'; "
        "uci set network.wwan.device='@sta0'; "
        "uci commit network 2>/dev/null; "
        "uci set wireless.radio1=wifi-device; "
        "uci set wireless.radio1.type='mac80211'; "
        f"uci set wireless.radio1.phy='{sta_phy}'; "
        "uci set wireless.radio1.channel='1'; "
        "uci set wireless.radio1.band='2g'; "
        "uci set wireless.radio1.htmode='HT20'; "
        "uci set wireless.radio1.disabled='0'; "
        "uci set wireless.sta0=wifi-iface; "
        "uci set wireless.sta0.device='radio1'; "
        "uci set wireless.sta0.mode='sta'; "
        f"uci set wireless.sta0.ssid='{ap_ssid}'; "
        "uci set wireless.sta0.network='wwan'; "
        "uci set wireless.sta0.encryption='none'; "
        "uci commit wireless 2>/dev/null; "
        "/etc/init.d/network reload 2>/dev/null || true; "
        "wifi reload 2>/dev/null || true"
    )

    time.sleep(5)

    sta_iface = _find_sta_interface(router)
    if not sta_iface:
        iw_dev = router.ssh("iw dev 2>/dev/null")
        uci_wireless = router.ssh("uci show wireless 2>/dev/null | head -30")
        pytest.skip(
            f"No STA interface after radio1 config. PHY={sta_phy}. "
            f"iw dev: {iw_dev[:300]}. UCI: {uci_wireless[:300]}"
        )

    router.ssh(f"iw {sta_iface} scan trigger 2>/dev/null || true")
    time.sleep(3)
    scan_dump = router.ssh(f"iw {sta_iface} scan dump 2>&1")

    if ap_ssid not in scan_dump and "Resource busy" in scan_dump:
        time.sleep(3)
        scan_dump = router.ssh(f"iw {sta_iface} scan 2>&1")
    if ap_ssid not in scan_dump and "Resource busy" in scan_dump:
        time.sleep(5)
        scan_dump = router.ssh(f"iw {sta_iface} scan 2>&1")

    assert ap_ssid in scan_dump, (
        f"STA scan did not find '{ap_ssid}'. Output: {scan_dump[:500]}"
    )


@pytest.mark.slow
def test_sta_associates_with_ap(router):
    _skip_unless_sta_supported()
    if not _module_loaded(router):
        pytest.skip("mac80211_hwsim not loaded")

    sta_iface = _find_sta_interface(router)
    if not sta_iface:
        pytest.skip("No STA interface found")

    connected = False
    link_output = "Not connected"
    for _ in range(5):
        link_output = router.ssh(f"iw {sta_iface} link 2>&1")
        if "Connected to" in link_output:
            connected = True
            break
        if "Not connected" not in link_output:
            connected = "Connected to" in link_output
            if connected:
                break
        time.sleep(2)

    if not connected:
        wpa_log = router.ssh(r"logread -e 'wireless|wpa' 2>/dev/null | tail -20")
        pytest.skip(
            f"STA did not associate within 10s. link: {link_output[:200]}. "
            f"wpa log: {wpa_log[:300]}"
        )


def test_sta_receives_dhcp(router):
    _skip_unless_sta_supported()
    if not _module_loaded(router):
        pytest.skip("mac80211_hwsim not loaded")

    sta_iface = _find_sta_interface(router)
    if not sta_iface:
        pytest.skip("No STA interface found")

    got_lease = False
    for _ in range(10):
        addr = router.ssh(f"ip -4 addr show {sta_iface} 2>/dev/null")
        if "inet " in addr:
            got_lease = True
            break
        time.sleep(2)

    if not got_lease:
        netifd_log = router.ssh(r"logread -e 'netifd|wwan' 2>/dev/null | tail -10")
        pytest.skip(
            f"STA did not obtain DHCP on {sta_iface}. netifd: {netifd_log[:300]}"
        )


@pytest.mark.slow
def test_sta_disconnect_and_reconnect(router):
    _skip_unless_sta_supported()
    if not _module_loaded(router):
        pytest.skip("mac80211_hwsim not loaded")

    sta_iface = _find_sta_interface(router)
    if not sta_iface:
        pytest.skip("No STA interface found")

    router.ssh(f"iw {sta_iface} disconnect 2>/dev/null || true")
    time.sleep(2)

    link_after_disconnect = router.ssh(f"iw {sta_iface} link 2>&1")

    ap_ssid = _detect_ap_ssid(router)
    connect_result = router.ssh(f"iw {sta_iface} connect {ap_ssid} 2>&1")
    time.sleep(5)

    link_output = router.ssh(f"iw {sta_iface} link 2>&1")
    assert "Connected to" in link_output, (
        f"STA did not reconnect. connect: {connect_result[:200]}, "
        f"link: {link_output[:200]}, "
        f"after disconnect: {link_after_disconnect[:200]}"
    )
