"""uiautomator2-backed phone driver for the rig (Android 15, LineageOS).

Bridges the android-tooling-brief pattern (serial -> u2.connect) into PRTA:
the serial comes from PHONE_SERIAL (the env var PRTA's conftest adb fixture
already uses), so labgrid-style target resolution collapses to one env var
on this single-phone bench.

Device facts pinned on-device (ZY326DPC7R, sdk 35 / Android 15):
- `cmd wifi connect-network/forget-network/status` are non-privileged and
  work from adb shell (AOSP android15 WifiShellCommand + verified live).
- `dumpsys connectivity` NetworkAgentInfo blocks print
  `nc{[ Transports: WIFI Capabilities: ...&CAPTIVE_PORTAL...&VALIDATED...`
  with the SSID embedded — parser below is pinned to that exact format
  (snapshot: /tmp/opencode/dumpsys-conn.txt on the bench host).
- Chrome (com.android.chrome) is the default browser; web inputs surface as
  android.widget.EditText in the a11y tree and u2 set_text round-trips
  400+ char tokens exactly (adb `input text` truncates near 200).
- /system/bin/curl exists on this ROM (extra HTTP-level assert available).
- Screen: `settings put global stay_on_while_plugged_in 7` holds the screen
  while USB-powered (verify mStayOn=true); secure keyguard with PIN —
  PIN comes from PHONE_PIN env (rig secret convention, never hardcoded).
"""

from __future__ import annotations

import logging
import re
import time

log = logging.getLogger("tollgate.u2phone")

_DATA_SM = re.compile(r'data-sm="?([^"\s]+)"?')
_AUTHED_STATES = ("authed", "countdown", "usage_dashboard")


def connect_u2(serial: str):
    import uiautomator2 as u2

    d = u2.connect(serial)
    d.settings["wait_timeout"] = 20.0
    return d


class U2Phone:
    """UI + shell driver for one phone via uiautomator2 + adb shell."""

    def __init__(self, d, pin: str = ""):
        self.d = d
        self.pin = pin

    def shell(self, cmd: str) -> str:
        """d.shell() returns a ShellResponse in u2 3.x — normalize to str."""
        r = self.d.shell(cmd)
        return r.output if hasattr(r, "output") else str(r)

    # -- power / lock ----------------------------------------------------

    def ensure_awake(self) -> None:
        self.shell("settings put global stay_on_while_plugged_in 7")
        self.shell("input keyevent KEYCODE_WAKEUP")

    def is_locked(self) -> bool:
        out = self.shell("dumpsys trust | grep '(current)' | head -1")
        m = re.search(r"deviceLocked=([01])", out)
        return bool(m) and m.group(1) == "1"

    def ensure_unlocked(self) -> bool:
        """Wake + swipe up; enter PHONE_PIN on the bouncer if configured.

        The swipe coordinates are valid for this 720x1520 panel (the PRTA
        default 540,2000 start point is off-screen on it).
        """
        self.ensure_awake()
        if not self.is_locked():
            return True
        self.d.swipe(0.5, 0.85, 0.4, 0.2, 0.3)
        time.sleep(1.5)
        if not self.is_locked():
            return True
        if not self.pin:
            log.warning("keyguard locked and no PHONE_PIN configured")
            return False
        self.shell(f"input text {self.pin}")
        time.sleep(0.8)
        self.shell("input keyevent 66")
        time.sleep(2)
        return not self.is_locked()

    # -- wifi ------------------------------------------------------------

    def wifi_connect(self, ssid: str, security: str = "open", psk: str = "") -> str:
        cmd = f"cmd wifi connect-network {ssid} {security}"
        if psk:
            cmd += f" {psk}"
        return self.shell(cmd).strip()

    def wifi_forget(self, ssid: str) -> str:
        out = self.shell("cmd wifi list-networks")
        for line in out.splitlines():
            if ssid in line:
                netid = line.split()[0]
                return self.shell(f"cmd wifi forget-network {netid}").strip()
        return ""

    def wifi_connected(self, ssid: str) -> bool:
        return ssid in self.shell("dumpsys wifi | grep 'mWifiInfo'")

    def wifi_validated(self, ssid: str) -> bool:
        """System verdict: the SSID's WIFI network has VALIDATED and no
        CAPTIVE_PORTAL capability. Parser pinned to this device's
        dumpsys connectivity format (NetworkAgentInfo/nc{[ ... } blocks)."""
        out = self.shell("dumpsys connectivity")
        for block in out.split("NetworkAgentInfo"):
            if "Transports: WIFI" not in block:
                continue
            if f'SSID: "{ssid}"' not in block:
                continue
            caps = block.split("nc{", 1)[1] if "nc{" in block else block
            # the capability list ends at LinkUpBandwidth; everything after
            # is score policies (VALIDATED/IS_VALIDATED bits) and other
            # networks' sections that must not leak into the tokens
            caps_list = caps.split("LinkUpBandwidth")[0]
            tokens = caps_list.replace(",", "&").split("&")
            return "VALIDATED" in tokens and "CAPTIVE_PORTAL" not in tokens
        return False

    def wait_wifi_validated(self, ssid: str, timeout: int = 60,
                            interval: float = 3) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.wifi_validated(ssid):
                return True
            time.sleep(interval)
        return False

    # -- connectivity asserts --------------------------------------------

    def ping_ok(self, host: str = "1.1.1.1") -> bool:
        # IP literal on purpose: a portal DNS hijack cannot lie about it.
        out = self.shell(f"ping -c 1 -W 3 {host}")
        return "1 received" in out or "1 packets received" in out

    def curl_http_code(self, url: str, timeout: int = 8) -> str:
        out = self.shell(f"curl -s -o /dev/null -w '%{{http_code}}' -m {timeout} '{url}'")
        return out.strip()

    # -- browser / portal -------------------------------------------------

    def dismiss_translate(self) -> None:
        """Swipe away Chrome's 'Translate this page?' bottom sheet.

        The sheet is invisible to the accessibility tree (the form behind
        it still dumps normally), so detection is impossible — instead one
        guarded swipe-down on the bottom quarter, then re-verify the web
        input is still reachable (type_token re-taps the field anyway, so
        a stray page scroll from the swipe is self-healing)."""
        self.d.swipe(0.5, 0.82, 0.5, 0.97, 0.25)
        time.sleep(0.8)
        self.d.swipe(0.5, 0.82, 0.5, 0.97, 0.25)
        time.sleep(0.5)

    def wait_web_input(self, timeout: int = 15) -> bool:
        return self.d(className="android.widget.EditText").wait(timeout=timeout)

    def open_url(self, url: str) -> None:
        self.shell(f"am start -a android.intent.action.VIEW -d '{url}'")

    def portal_state(self) -> str:
        """Current portal data-sm state from the accessibility tree.

        The portal SPA publishes its state machine as an aria-label-style
        marker (same mechanism PRTA's ui_xml regex relies on)."""
        m = _DATA_SM.search(self.d.dump_hierarchy())
        return m.group(1) if m else ""

    def wait_portal_state(self, states, timeout: int = 60, interval: float = 3) -> str:
        wanted = tuple(states)
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            last = self.portal_state()
            if last in wanted:
                return last
            time.sleep(interval)
        return last

    def tap_button(self, *texts: str) -> bool:
        for text in texts:
            el = self.d(text=text)
            if el.wait(timeout=3):
                el.click()
                return True
        return False

    def type_token(self, token: str) -> bool:
        """Type a full-length Cashu token into the portal's web input.

        u2 set_text round-trips 400+ chars exactly where adb input text
        silently truncates near 200. The Chrome omnibox also surfaces as
        an EditText at the top of the tree — pick the input whose bounds
        sit below the toolbar instead of trusting instance order."""
        import re as _re
        h = self.d.dump_hierarchy()
        cands = []
        for m in _re.finditer(
                r'<node[^>]*class="android.widget.EditText"[^>]*?'
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', h):
            x1, y1, x2, y2 = map(int, m.groups())
            if y1 > 200:  # below the omnibox/toolbar band
                cands.append(((x1 + x2) // 2, (y1 + y2) // 2))
        if not cands:
            return False
        x, y = cands[0]
        self.d.click(x, y)
        time.sleep(0.5)
        ok = self.d(focused=True).set_text(token)
        time.sleep(1.0)
        return bool(ok)
