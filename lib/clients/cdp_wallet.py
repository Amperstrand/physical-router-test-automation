"""Chrome DevTools Protocol driver for the cashu.me wallet tab.

Why CDP instead of uiautomator2 for the wallet: Chrome's accessibility tree
lags SPA mounts on this device (u2 dumps show a long-blank page while the
DOM is live), and cashu.me enforces a single-tab guard ("Another tab is
already running") that multiple am-start invocations trip silently. CDP
talks to the real DOM, closes duplicate tabs, and drives inputs with the
native-setter + input-event pattern Vue/Quasar require.

Connection: adb forward tcp:9222 localabstract:chrome_devtools_remote
(Chrome's USB debugging must be on; it already is on this phone). The
websocket handshake must omit the Origin header (Chrome rejects
127.0.0.1 origins with 403 otherwise).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request

import websocket

log = logging.getLogger("tollgate.cdp_wallet")

CDP_PORT = 9222


class CdpTab:
    def __init__(self, url_substr: str, adb_serial: str = ""):
        import subprocess

        if adb_serial:
            subprocess.run(
                ["adb", "-s", adb_serial, "forward", f"tcp:{CDP_PORT}",
                 "localabstract:chrome_devtools_remote"],
                capture_output=True, timeout=10, check=False)
        else:
            subprocess.run(
                ["adb", "forward", f"tcp:{CDP_PORT}",
                 "localabstract:chrome_devtools_remote"],
                capture_output=True, timeout=10, check=False)
        self.url_substr = url_substr
        self._ws = None

    def targets(self) -> list[dict]:
        return json.load(urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json", timeout=5))

    def close_duplicate_tabs(self) -> int:
        """cashu.me's single-tab guard: keep exactly one matching tab."""
        matches = [t for t in self.targets()
                   if t.get("type") == "page" and self.url_substr in t.get("url", "")]
        closed = 0
        for t in matches[1:]:
            urllib.request.urlopen(
                f"http://127.0.0.1:{CDP_PORT}/json/close/{t['id']}", timeout=5)
            closed += 1
        if closed:
            log.info("closed %d duplicate wallet tab(s)", closed)
        return closed

    def connect(self, timeout: float = 20.0):
        """Attach to the (single) matching tab; waits for the page to exist.

        Page.bringToFront unfreezes the tab — Chrome suspends background
        tabs and Runtime.evaluate on a frozen tab hangs until timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            tgt = next((t for t in self.targets()
                        if t.get("type") == "page"
                        and self.url_substr in t.get("url", "")), None)
            if tgt:
                self._ws = websocket.create_connection(
                    tgt["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
                self._ws.send(json.dumps(
                    {"id": 0, "method": "Page.bringToFront"}))
                try:
                    while True:
                        m = json.loads(self._ws.recv())
                        if m.get("id") == 0:
                            break
                except Exception:
                    pass
                return tgt["url"]
            time.sleep(2)
        raise RuntimeError(f"no tab matching {self.url_substr!r}")

    def ev(self, expr: str, await_promise: bool = False, timeout: float = 20):
        assert self._ws is not None, "call connect() first"
        self._ws.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expr, "awaitPromise": await_promise,
                       "returnByValue": True}}))
        deadline = time.time() + timeout
        self._ws.settimeout(max(1, deadline - time.time()))
        while True:
            m = json.loads(self._ws.recv())
            if m.get("id") == 1:
                return m.get("result", {}).get("result", {}).get("value")

    def body_text(self, n: int = 200) -> str:
        return self.ev(f"document.body.innerText.slice(0,{n})") or ""

    # -- wallet-specific helpers ------------------------------------------

    def set_input(self, css: str, value: str) -> str:
        """Set a Vue/Quasar input the framework can see (native setter +
        input event); plain el.value assignment is invisible to v-model."""
        return self.ev(
            f"(() => {{ const inp = document.querySelector({css!r});"
            f" if (!inp) return null;"
            f" const set = Object.getOwnPropertyDescriptor("
            f"window.HTMLInputElement.prototype, 'value').set;"
            f" set.call(inp, {value!r});"
            f" inp.dispatchEvent(new Event('input', {{bubbles: true}}));"
            f" return inp.value; }})()")

    def click_button(self, text_substr: str) -> bool:
        return self.ev(
            f"(() => {{ const b = Array.from("
            f"document.querySelectorAll('button')).find(b => "
            f"b.innerText.includes({text_substr!r}));"
            f" if (!b || b.disabled) return false; b.click(); return true; }})()")

    def fetch_status(self, url: str, timeout_s: int = 8):
        return self.ev(
            f"fetch({url!r}).then(r => r.status)"
            f".catch(e => 'ERR:' + e.message)",
            await_promise=True, timeout=timeout_s + 5)

    def close(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
