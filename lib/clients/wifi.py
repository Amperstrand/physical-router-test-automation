import re
import time
import logging
import os

from lib.ssid import (
    DEFAULT_CAPTIVE_PREFIX,
    captive_prefixes_from_env,
    extract_ssids,
    matches_prefix,
    normalize_prefix,
)

log = logging.getLogger("tollgate.wifi")


def _is_desktop_client(adb):
    return getattr(adb, "is_desktop", False)


class WiFi:
    def __init__(self, adb, router, ssid: str, captive_prefixes: list[str] | None = None):
        self.adb = adb
        self.router = router
        # Brand prefix varies per router (TollGate- default, Net4sats- branded).
        self.captive_prefixes = (
            [normalize_prefix(p) for p in captive_prefixes if normalize_prefix(p)]
            if captive_prefixes
            else captive_prefixes_from_env()
        )
        if not self.captive_prefixes:
            self.captive_prefixes = [DEFAULT_CAPTIVE_PREFIX]
        # Backward-compat attribute; scanning uses the full prefix list now.
        self.ssid_prefix = self.captive_prefixes[0]
        self.ssid = self._resolve_ssid(ssid)

    def _ensure_phone_can_connect(self):
        self.router.fix_nodogsplash_dhcp()
        self.router.disable_ipv6_on_lan()

    def _resolve_ssid(self, fallback: str) -> str:
        """Pick the router's captive SSID by matching ANY configured prefix.

        Probes live radios (iwinfo) then UCI config; the dead
        'grep -v private' heuristic is gone — private SSIDs are excluded by
        prefix classification, not by name substring.
        """
        for cmd in (
            "iwinfo 2>/dev/null | grep ESSID",
            "uci show wireless 2>/dev/null | grep '\\.ssid='",
        ):
            try:
                out = self.router.ssh(cmd)
            except Exception as e:
                log.debug("SSID auto-detection via %r failed: %s", cmd.split()[0], e)
                continue
            for ssid in extract_ssids(out):
                if any(matches_prefix(ssid, p) for p in self.captive_prefixes):
                    log.info("Auto-detected SSID: %s", ssid)
                    return ssid
        return fallback

    def _scan_prefix_pattern(self) -> str:
        """Regex alternation matching any captive prefix + '-suffix'."""
        return "(?:" + "|".join(re.escape(p) for p in self.captive_prefixes) + ")-[^\"]*"

    def _tap_ssid(self, xml: str, ssid: str) -> bool:
        if _is_desktop_client(self.adb):
            return False
        esc = re.escape(ssid)

        # Strategy 1: Find the clickable parent row node.
        # Android WiFi settings wraps each network in a clickable LinearLayout
        # with content-desc="SSID,...". The parent has full-row bounds.
        # e.g. content-desc="TollGate-101B,Sign in to network,..."
        parent_match = re.search(
            rf'<node[^>]*content-desc="{esc}[,"][^>]*clickable="true"[^>]*'
            rf'bounds="\[([^]]*)\]\[([^]]*)\]"[^>]*>',
            xml,
        )
        if not parent_match:
            # Try bounds before clickable (attribute order varies)
            parent_match = re.search(
                rf'<node[^>]*content-desc="{esc}[,"][^>]*'
                rf'bounds="\[([^]]*)\]\[([^]]*)\]"[^>]*clickable="true"[^>]*>',
                xml,
            )
        if parent_match:
            bounds_str = f"[{parent_match.group(1)}][{parent_match.group(2)}]"
            log.info(f"Tapping parent row for {ssid}: {bounds_str}")
            self.adb.tap_bounds(bounds_str)
            return True

        # Strategy 2: Find child node with text="SSID" and tap its bounds.
        # The child is usually inside the clickable parent so taps bubble up.
        node_match = re.search(
            rf'<node[^>]*text="{esc}"[^>]*bounds="\[([^]]*)\]\[([^]]*)\]"[^>]*>',
            xml,
        )
        if not node_match:
            # Fallback: any node containing the SSID as a full quoted attribute value
            node_match = re.search(rf'<node[^>]*"{esc}"[^>]*>', xml)
            if node_match:
                node = node_match.group(0)
                bounds_match = re.search(r'bounds="\[([^]]*)\]\[([^]]*)\]"', node)
                if bounds_match:
                    self.adb.tap_bounds(
                        f"[{bounds_match.group(1)}][{bounds_match.group(2)}]"
                    )
                    return True
            return False

        bounds_str = f"[{node_match.group(1)}][{node_match.group(2)}]"
        log.info(f"Tapping child text node for {ssid}: {bounds_str}")
        self.adb.tap_bounds(bounds_str)
        return True

    def open_wifi_settings(self):
        if _is_desktop_client(self.adb):
            return
        self.adb.shell("am start -a android.settings.WIFI_SETTINGS")
        time.sleep(3)
        xml = self.adb.ui_xml()
        wifi_indicators = ["Available networks", "Current network", "Wi-Fi.*On",
                           "Turn on Wi-Fi", self.ssid]
        if not any(re.search(p, xml) for p in wifi_indicators):
            log.info("WIFI_SETTINGS opened wrong page, navigating via Settings")
            self.adb.press_key("KEYCODE_BACK")
            time.sleep(1)
            self.adb.press_key("KEYCODE_BACK")
            time.sleep(1)
            self.adb.shell("am start -n com.android.settings/.Settings")
            time.sleep(3)
            xml = self.adb.ui_xml()
            conn_match = re.search(r'text="Connections"[^>]*bounds="\[([^]]*)\]\[([^]]*)\]"', xml)
            if conn_match:
                full = re.search(r'bounds="\[([^]]*)\]\[([^]]*)\]"',
                                 re.search(r'text="Connections"[^>]*>', xml).group(0))
                if full:
                    self.adb.tap_bounds(f"[{full.group(1)}][{full.group(2)}]")
                    time.sleep(3)
            xml = self.adb.ui_xml()
            wifi_match = re.search(r'text="Wi-Fi"[^>]*>', xml)
            if wifi_match:
                bounds = re.search(r'bounds="\[([^]]*)\]\[([^]]*)\]"', wifi_match.group(0))
                if bounds:
                    self.adb.tap_bounds(f"[{bounds.group(1)}][{bounds.group(2)}]")
                    time.sleep(3)

    def _connect_to_wifi(self) -> bool:
        if _is_desktop_client(self.adb):
            return self._connect_to_wifi_desktop()

        self._ensure_phone_can_connect()

        if self.adb.is_wifi_connected(self.ssid):
            log.info(f"Already connected to {self.ssid}, skipping reconnect")
            return True

        log.info("Ensuring airplane mode is off")
        self.adb.shell("settings put global airplane_mode_on 0")
        self.adb.shell("cmd connectivity airplane-mode disable")

        log.info("Disabling WiFi to force fresh scan")
        self.adb.shell("svc wifi disable")
        time.sleep(3)

        log.info("Waking phone")
        self.adb.wake_and_unlock()

        log.info("Enabling WiFi")
        self.adb.shell("svc wifi enable")
        time.sleep(4)

        log.info("Opening WiFi settings")
        self.open_wifi_settings()
        time.sleep(4)

        found = False
        for attempt in range(1, 11):
            xml = self.adb.ui_xml()
            if self.ssid in xml:
                log.info(f"Found {self.ssid} on scan attempt {attempt}")
                found = True
                break
            m = re.search(f'text="({self._scan_prefix_pattern()})"', xml)
            if m:
                self.ssid = m.group(1)
                log.info(f"Found SSID via prefix match: {self.ssid}")
                found = True
                break
            self.adb.shell("input swipe 540 1600 540 800 300")
            time.sleep(1)
            xml = self.adb.ui_xml()
            if self.ssid in xml:
                log.info(f"Found {self.ssid} after scrolling on attempt {attempt}")
                found = True
                break
            m = re.search(f'text="({self._scan_prefix_pattern()})"', xml)
            if m:
                self.ssid = m.group(1)
                log.info(f"Found SSID via prefix match after scroll: {self.ssid}")
                found = True
                break
            log.info(f"Scan {attempt}: {self.ssid} not visible yet")
            if attempt == 5:
                log.info("Re-opening WiFi settings for fresh scan")
                self.open_wifi_settings()
                time.sleep(3)
            else:
                time.sleep(4)

        if not found:
            log.warning(f"{self.ssid} not found after 10 scans")
            return False

        xml = self.adb.ui_xml()
        if not self._tap_ssid(xml, self.ssid):
            log.warning(f"Failed to tap {self.ssid} in UI")
            return False
        log.info(f"Tapped {self.ssid}, waiting for connection...")
        time.sleep(6)

        for attempt in range(1, 6):
            if self.adb.is_wifi_connected(self.ssid):
                log.info(f"WiFi connected after {attempt} checks")
                return True
            time.sleep(3)

        log.warning("SSID tapped but not connected after 5 checks")
        return False

    def _connect_to_wifi_desktop(self) -> bool:
        log.info(f"Connecting to {self.ssid} via desktop WiFi client")
        self.adb.wake_and_unlock()

        for attempt in range(1, 4):
            if self.adb.connect_wifi(self.ssid):
                log.info(f"Connected to {self.ssid} on attempt {attempt}")
                return True
            log.info(f"Desktop WiFi connect attempt {attempt} failed, retrying...")
            time.sleep(3)

        log.error(f"Failed to connect to {self.ssid} from desktop")
        return False

    def is_connected(self) -> bool:
        if _is_desktop_client(self.adb):
            return True
        dump = self.adb.shell("dumpsys wifi | grep 'mWifiInfo'").strip()
        return self.ssid in dump

    def reconnect(self, skip_portal: bool = False) -> bool:
        if not self._connect_to_wifi():
            return False

        if _is_desktop_client(self.adb):
            return self._reconnect_desktop()

        log.info(f"Connected to {self.ssid}")

        if skip_portal:
            log.info("Portal detection skipped")
            return True

        return self._open_portal_on_phone(
            r'data-sm="[^"]*"|Tollgate Captive Portal|TollGate.*portal_ready',
        )

    def _get_portal_host(self) -> str:
        portal_host = os.environ.get("TOLLGATE_PORTAL_HOST")
        if portal_host:
            log.info(f"Using configured portal host: {portal_host}")
            return portal_host
        domain = self.router.get_nds_gateway_domain()
        if domain:
            log.info(f"Using NDS gateway domain: {domain}")
            return domain
        ip = self.router.ssh("ip addr show br-lan | grep 'inet ' | awk '{print $2}' | cut -d/ -f1")
        if not ip:
            raise RuntimeError("Could not determine router LAN IP from br-lan interface")
        log.info(f"Detected router LAN IP: {ip}")
        return ip

    def _open_portal_via_native_captive(self, state_pattern: str, timeout: int = 30) -> bool:
        log.info("Attempting native captive portal popup...")

        self.adb.shell("am force-stop com.android.captiveportallogin 2>/dev/null")
        time.sleep(1)

        self.adb.shell("am start -a android.settings.WIFI_SETTINGS")
        time.sleep(3)

        xml = self.adb.ui_xml()
        ssid_escaped = re.escape(self.ssid)

        # Match TollGate SSID title followed by "Sign in" in the same UI row
        entry_match = re.search(
            rf'<node[^>]*text="{ssid_escaped}"[^>]*resource-id="[^"]*title"[^>]*/>'
            rf'.*?text="[^"]*[Ss]ign in[^"]*"',
            xml, re.DOTALL,
        )

        if not entry_match:
            entry_match = re.search(
                rf'<node[^>]*clickable="true"[^>]*bounds="\[([^]]*)\]\[([^]]*)\]"[^>]*>.*?'
                rf'text="{ssid_escaped}"',
                xml, re.DOTALL,
            )

        if not entry_match:
            log.warning("Could not find TollGate entry in WiFi settings for native portal")
            return False

        # Re-search to capture clickable container bounds for the tap
        container_match = re.search(
            rf'<node[^>]*clickable="true"[^>]*bounds="\[([^]]*)\]\[([^]]*)\]"[^>]*>.*?'
            rf'text="{ssid_escaped}"',
            xml, re.DOTALL,
        )
        if container_match:
            bounds_str = f"[{container_match.group(1)}][{container_match.group(2)}]"
            self.adb.tap_bounds(bounds_str)
            log.info(f"Tapped TollGate WiFi entry at {bounds_str}")
        else:
            # Tap the first entry under "Current network"
            connected_match = re.search(
                r'text="Current network".*?'
                r'<node[^>]*clickable="true"[^>]*bounds="\[([^]]*)\]\[([^]]*)\]"',
                xml, re.DOTALL,
            )
            if connected_match:
                bounds_str = f"[{connected_match.group(1)}][{connected_match.group(2)}]"
                self.adb.tap_bounds(bounds_str)
                log.info(f"Tapped current network entry at {bounds_str}")
            else:
                log.warning("Could not find clickable TollGate entry to tap")
                return False

        time.sleep(5)

        xml = self.adb.ui_xml()
        if "captiveportallogin" in xml.lower() or "Tollgate Captive Portal" in xml:
            log.info("Native captive portal opened successfully")

        start = time.time()
        while time.time() - start < timeout:
            xml = self.adb.ui_xml()
            if re.search(state_pattern, xml):
                sm = re.search(r'data-sm="([^"]*)"', xml)
                if sm:
                    log.info(f"Native portal reached state '{sm.group(1)}' after {int(time.time()-start)}s")
                else:
                    log.info(f"Native portal page loaded after {int(time.time()-start)}s")
                return True
            time.sleep(3)

        log.warning(f"Native captive portal did not render within {timeout}s")
        return False

    def _open_portal_via_browser(self, portal_url: str, state_pattern: str, timeout: int = 30) -> bool:
        log.info(f"Falling back to browser: opening portal at {portal_url}")

        if hasattr(self.adb, 'force_stop_browser'):
            self.adb.force_stop_browser()
        else:
            self.adb.shell("am force-stop com.android.chrome")
            self.adb.shell("am force-stop com.sec.android.app.sbrowser")

        self.adb.start_activity(action="android.intent.action.VIEW", data_uri=portal_url)
        time.sleep(3)

        log.info("Waiting for portal to render in browser...")
        start = time.time()
        while time.time() - start < timeout:
            xml = self.adb.ui_xml()
            if re.search(state_pattern, xml):
                sm = re.search(r'data-sm="([^"]*)"', xml)
                if sm:
                    log.info(f"Browser portal reached state '{sm.group(1)}' after {int(time.time()-start)}s")
                else:
                    log.info(f"Browser portal page loaded after {int(time.time()-start)}s")
                return True
            time.sleep(3)

        log.warning(f"Browser portal did not reach expected state within {timeout}s")
        return False

    def _open_portal_on_phone(self, state_pattern: str, timeout: int = 30) -> bool:
        if self.router.get_nds_gateway_domain():
            self.router.ensure_nds_gateway_domain_supported()
        portal_host = self._get_portal_host()
        portal_url = f"http://{portal_host}/"

        if not _is_desktop_client(self.adb):
            try:
                if self._open_portal_via_native_captive(state_pattern, timeout=timeout):
                    return True
                log.info("Native captive portal failed, falling back to browser")
            except Exception as e:
                log.warning(f"Native captive portal error: {e}, falling back to browser")

        return self._open_portal_via_browser(portal_url, state_pattern, timeout=timeout)

    def _type_token_in_portal(self, token: str, timeout: int = 60) -> bool:
        """Type a cashu token into the portal's input field and submit."""
        # Step 1: Wait for portal to be ready
        log.info("Waiting for portal to be ready for token input...")
        start = time.time()
        while time.time() - start < timeout:
            xml = self.adb.ui_xml()
            sm = re.search(r'data-sm="(portal_ready|token_typing)"', xml)
            if sm:
                log.info(f"Portal ready for input (state: {sm.group(1)})")
                break
            time.sleep(3)
        else:
            log.warning("Portal did not reach portal_ready state")
            return False

        # Step 2: Find and tap the token input field
        # Portal is a React SPA in a WebView. Look for EditText or input nodes.
        xml = self.adb.ui_xml()
        input_match = re.search(
            r'<node[^>]*class="android.widget.EditText"[^>]*bounds="\[([^]]*)\]\[([^]]*)\]"',
            xml,
        )
        if not input_match:
            # Fallback: look for any editable text field
            input_match = re.search(
                r'<node[^>]*clickable="true"[^>]*text="[^"]*"[^>]*bounds="\[([^]]*)\]\[([^]]*)\]"',
                xml,
            )
        if not input_match:
            log.warning("Could not find token input field in portal")
            return False

        # Tap the input field
        bounds_str = f"[{input_match.group(1)}][{input_match.group(2)}]"
        self.adb.tap_bounds(bounds_str)
        time.sleep(1)
        log.info("Tapped token input field")

        # Step 3: Type the token
        # ADB input text doesn't handle some special chars well (%, spaces, etc.)
        # Cashu tokens are URL-safe base64 (A-Za-z0-9+/=) — +/= can be problematic
        # Use clipboard as reliable fallback
        try:
            # Try direct input first (works for most chars)
            escaped = token.replace(" ", "%s").replace("&", "\\&").replace("%", "\\%")
            self.adb.input_text(escaped)
            time.sleep(1)
        except Exception:
            log.info("Direct input failed, trying clipboard approach")
            # Use Android clipboard to paste
            self.adb.shell(f"am broadcast -a clipper.set -e text '{token}'")
            time.sleep(0.5)
            # Long press to show paste option
            self.adb.shell("input swipe 540 1200 540 1200 1000")
            time.sleep(1)
            # Look for and tap "Paste" button
            paste_xml = self.adb.ui_xml()
            paste_match = re.search(
                r'text="Paste"[^>]*bounds="\[([^]]*)\]\[([^]]*)\]"', paste_xml
            )
            if paste_match:
                self.adb.tap_bounds(f"[{paste_match.group(1)}][{paste_match.group(2)}]")
                time.sleep(1)

        log.info(f"Typed token ({len(token)} chars)")

        # Step 4: Find and tap submit button
        time.sleep(1)
        xml = self.adb.ui_xml()
        # Look for a button/clickable element — try common patterns
        submit_patterns = [
            r'text="Submit"[^>]*bounds="\[([^]]*)\]\[([^]]*)\]"',
            r'text="Pay"[^>]*bounds="\[([^]]*)\]\[([^]]*)\]"',
            r'text="Connect"[^>]*bounds="\[([^]]*)\]\[([^]]*)\]"',
            r'text="Go"[^>]*bounds="\[([^]]*)\]\[([^]]*)\]"',
        ]
        submitted = False
        for pattern in submit_patterns:
            btn_match = re.search(pattern, xml, re.IGNORECASE)
            if btn_match:
                self.adb.tap_bounds(f"[{btn_match.group(1)}][{btn_match.group(2)}]")
                log.info(f"Tapped submit button matching: {pattern[:30]}")
                submitted = True
                break

        if not submitted:
            # Try pressing Enter as fallback
            log.info("No submit button found, pressing Enter as fallback")
            self.adb.press_key("KEYCODE_ENTER")

        # Step 5: Wait for auth confirmation
        log.info("Waiting for authentication...")
        start = time.time()
        while time.time() - start < timeout:
            xml = self.adb.ui_xml()
            sm = re.search(r'data-sm="(authed|countdown|usage_dashboard)"', xml)
            if sm:
                log.info(
                    f"Portal reached authenticated state '{sm.group(1)}' after {int(time.time()-start)}s"
                )
                return True
            time.sleep(3)

        log.warning(f"Portal did not reach authenticated state within {timeout}s")
        return False

    def _reconnect_desktop(self) -> bool:
        log.info(f"Connected to {self.ssid} from desktop")
        self.router.ssh("echo '' > /tmp/tollgate-portal.log")

        domain = self.router.domain
        if domain:
            portal_url = f"http://{domain}:8080/"
            log.info(f"Opening portal at {portal_url}")
            self.adb.open_url(portal_url)
            time.sleep(5)

        log.info("Desktop connected to TollGate WiFi — portal accessible via browser or API")
        return True

    def reconnect_no_fallback(self) -> bool:
        if not self._connect_to_wifi():
            return False

        if _is_desktop_client(self.adb):
            return True

        if not self.adb.is_wifi_connected(self.ssid):
            log.error(f"Failed to connect to {self.ssid}")
            return False

        return self._open_portal_on_phone(
            r'data-sm="(portal_ready|token_typing)"',
            timeout=30,
        )
