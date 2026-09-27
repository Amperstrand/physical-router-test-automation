#!/usr/bin/env python3
"""Recorded phone-portal payment on the Cuttlefish emulator.

Drives the TollGate captive portal on the phone over the dry client
abstraction (CuttlefishClient; an ADBDevice on a physical phone exposes
the same methods): opens the portal via the NDS port-80 intercept, types
a cashu token, presses Purchase, and verifies the router authenticated
the phone's MAC and that internet flows. Screenrecords throughout.

Usage:
    python3 scripts/phone-portal-demo.py --token-file /tmp/phone-token.txt \
        --router 192.168.94.2 --out evidence/phone-demo
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.clients.cuttlefish import CuttlefishClient  # noqa: E402

INPUT_FIELD = (360, 633)     # fallback: token EditText center (720x1280)
PURCHASE = (360, 1090)       # fallback: Pay-button center


def portal_field(client, timeout=30):
    """(x, y) of the portal token EditText once the SPA a11y tree is up.

    The webview can restore a mid-page scroll position from a previous
    session; pull-to-top first so node bounds are deterministic."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        xml = client.ui_xml()
        boxes = [tuple(map(int, b)) for b in re.findall(
            r'<node[^>]*class="android.widget.EditText"[^>]*'
            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)]
        below = [b for b in boxes if b[1] > 400]
        if below:
            for _ in range(2):
                client.swipe(360, 500, 360, 1100, 200)
                time.sleep(0.5)
            xml = client.ui_xml()
            boxes = [tuple(map(int, b)) for b in re.findall(
                r'<node[^>]*class="android.widget.EditText"[^>]*'
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)]
            below = [b for b in boxes if b[1] > 400]
            if below:
                x1, y1, x2, y2 = below[0]
                return (x1 + x2) // 2, (y1 + y2) // 2
        time.sleep(2)
    return INPUT_FIELD


PAY_BUTTON = r"Pay \d+ sat"


def submit_via_devtools(phone, token, out, gateway_ip):
    """Drive the portal DOM over the WebView's CDP endpoint (adb forward
    on the Cuttlefish host, tunnelled here): fill the token input and
    click the Pay button — deterministic where input-tap UI driving is
    not."""
    import subprocess as sp
    fwd = sp.Popen(
        ["ssh", "-o", "ControlPath=none", "-o", "ExitOnForwardFailure=yes",
         "-L", "9223:127.0.0.1:9222", phone.host,
         "adb -s %s forward tcp:9222 localabstract:webview_devtools_remote_"
         "$(adb -s %s shell pidof org.chromium.webview_shell | tr -d '\\r'); "
         "sleep 60" % (phone.serial, phone.serial)],
        stdout=sp.DEVNULL, stderr=sp.DEVNULL)
    try:
        import urllib.request
        for _ in range(10):
            time.sleep(1)
            try:
                urllib.request.urlopen("http://127.0.0.1:9223/json",
                                       timeout=2).read(64)
                break
            except OSError:
                continue
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9223")
            page = None
            for ctx in browser.contexts:
                for p in ctx.pages:
                    if ":2051/" in p.url:
                        page = p
                        break
            if page is None:
                page = browser.contexts[0].pages[0]
                page.goto(f"http://{gateway_ip}/",
                          wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_url("**:2051/**", timeout=25000)
                except Exception:
                    pass  # asserted below with the actual URL
            assert ":2051/" in page.url, f"portal did not load: {page.url}"
            page.bring_to_front()
            try:
                page.get_by_text("Cashu").first.click(timeout=5000)
                time.sleep(1)
            except Exception:
                pass  # already active or single-tab build
            # whitelabel builds open on a size selector; 100 MB is the
            # 5-sat minimum the demo token pays for. The panel re-renders
            # asynchronously after the click — wait for the token input to
            # re-attach and verify the value actually stuck.
            try:
                page.get_by_role("button", name="100 MB").click(timeout=4000)
            except Exception:
                pass  # default build has no size selector
            field = None
            for attempt in range(3):
                try:
                    page.wait_for_selector(
                        "input[id=cashu-token], textarea", timeout=8000)
                except Exception:
                    print(f"attempt {attempt}: field selector timeout",
                          file=sys.stderr)
                time.sleep(2)
                # React controlled inputs drop playwright's fill(); set the
                # value through the native setter, reset the value tracker
                # so onChange observes the change, and bubble the input event
                try:
                    page.evaluate(
                        """token => {
                            const el = document.querySelector(
                                'input[id=cashu-token], textarea');
                            const setter = Object.getOwnPropertyDescriptor(
                                el.tagName === 'TEXTAREA'
                                    ? window.HTMLTextAreaElement.prototype
                                    : window.HTMLInputElement.prototype,
                                'value').set;
                            setter.call(el, token);
                            if (el._valueTracker) {
                                el._valueTracker.setValue('');
                            }
                            el.dispatchEvent(new Event('input',
                                                       {bubbles: true}));
                        }""", token)
                    time.sleep(1)
                    field = page.locator(
                        "input[id=cashu-token], textarea").first
                    val = field.input_value()
                    print(f"attempt {attempt}: field value "
                          f"{val[:16]!r}", file=sys.stderr)
                    if val.startswith("cashu"):
                        break
                except Exception as exc:
                    print(f"attempt {attempt}: {exc!r}"[:200], file=sys.stderr)
                    continue
            else:
                raise RuntimeError("token never landed in the portal field")
            page.screenshot(path=str(out / "02-token-typed.png"))
            try:
                clicked = False
                # whitelabel: "Purchase Internet Access" button after size
                # selection; default build: "Pay N sat(s) to get X" (the
                # text is split across spans, so match on a text fragment)
                for loc in (page.get_by_role("button",
                                             name="Purchase Internet Access"),
                            page.get_by_text("sat to get")):
                    try:
                        loc.last.click(timeout=15000)
                        clicked = True
                        break
                    except Exception:
                        continue
                if not clicked:
                    raise RuntimeError("no Pay/Purchase control found")
            except Exception:
                page.screenshot(path=str(out / "click-timeout.png"))
                texts = page.evaluate(
                    "() => document.body.innerText.slice(0, 400)")
                print(f"click timeout; url={page.url} body={texts!r}",
                      file=sys.stderr)
                raise
            time.sleep(1)
            page.screenshot(path=str(out / "03-after-purchase.png"))
    finally:
        fwd.terminate()


def node_center(client, pattern, fallback):
    """Center bounds of the first ui_xml node whose text/content-desc
    matches, else the fallback coordinate. For the token field the first
    EditText below the browser chrome (y > 400) is used."""
    xml = client.ui_xml()
    m = re.search(
        r'<node[^>]*((?:text|content-desc)="[^"]*' + pattern + r'[^"]*")[^>]*'
        r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
    if not m:
        boxes = [tuple(map(int, b)) for b in re.findall(
            r'<node[^>]*class="android.widget.EditText"[^>]*'
            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)]
        boxes = [b for b in boxes if b[1] > 400] or boxes
        if boxes:
            x1, y1, x2, y2 = boxes[0]
            return (x1 + x2) // 2, (y1 + y2) // 2
        return fallback
    x1, y1, x2, y2 = map(int, m.groups()[1:])
    return (x1 + x2) // 2, (y1 + y2) // 2


def ssh_router(host, cmd, timeout=15):
    r = subprocess.run(
        ["sshpass", "-p", "tollgate", "ssh", "-o", "StrictHostKeyChecking=no",
         "-o", "ProxyJump=ai-legion", f"root@{host}", cmd],
        capture_output=True, text=True, timeout=timeout, check=False)
    return r.stdout.strip()


def type_token(client, token, chunk=200):
    for i in range(0, len(token), chunk):
        client.text(token[i:i + chunk])
        time.sleep(0.4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--router", default="192.168.94.2")
    ap.add_argument("--gateway-ip", default="192.168.99.129",
                    help="WiFi-side gateway IP to trigger the intercept")
    ap.add_argument("--out", default="evidence/phone-demo")
    ap.add_argument("--ssid-prefix", default="TollGate",
                    help="WiFi SSID brand prefix to expect (TollGate | Net4sats)")
    args = ap.parse_args()

    token = Path(args.token_file).read_text().strip()
    if not token.startswith("cashu"):
        print(f"token file {args.token_file} does not hold a cashu token — "
              "mint one first", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    phone = CuttlefishClient()
    mac = phone.wifi_mac()
    print(f"phone wifi mac: {mac}")
    assert phone.is_wifi_connected(args.ssid_prefix), \
        f"phone not on {args.ssid_prefix} WiFi"

    # a prior run's mark workaround (below) survives restarts — drop it so
    # the phone starts genuinely preauthenticated
    ssh_router(args.router,
               f"iptables -t mangle -D ndsOUT -m mac --mac-source {mac} "
               f"-j MARK --set-mark 0x20000/0x30000 2>/dev/null; true")

    print("router log capture: starting")
    log = subprocess.Popen(
        ["sshpass", "-p", "tollgate", "ssh", "-o", "StrictHostKeyChecking=no",
         "-o", "ProxyJump=ai-legion", f"root@{args.router}", "logread -f"],
        stdout=open(out / "router.log", "w"), stderr=subprocess.DEVNULL)

    phone.screen_record_start("/sdcard/phone-demo.mp4")
    try:
        phone.force_stop("org.chromium.webview_shell")
        time.sleep(1)
        print("opening portal (port-80 intercept)")
        phone.open_url(f"http://{args.gateway_ip}/")
        time.sleep(12)
        phone.screenshot(str(out / "01-portal.png"))

        print("filling token + pressing Purchase via webview devtools")
        submit_via_devtools(phone, token, out, args.gateway_ip)

        authed = False
        deadline = time.time() + 45
        while not authed and time.time() < deadline:
            state = ssh_router(args.router, "ndsctl json")
            authed = f'"{mac}"' in state and "Authenticated" in state
            time.sleep(3)
        print(f"router authenticated: {authed}")

        net = False
        if authed:
            # NDS 5.0.2 auth-mark bug (see PRTA AGENTS.md "auth-mark bug"):
            # ndsctl auth sets 0x30000 but ndsNET accepts exact 0x20000, and
            # the mangle marker thread dies across restarts — authenticated
            # clients cannot open new connections. Lab workaround: set the
            # client mark explicitly.
            ssh_router(args.router,
                       f"iptables -t mangle -I ndsOUT 1 -m mac "
                       f"--mac-source {mac} -j MARK --set-mark 0x20000/0x30000")
            time.sleep(2)
            deadline = time.time() + 30
            while time.time() < deadline:
                net = "bytes from" in phone.shell(
                    "ping -c 1 -W 3 1.1.1.1")
                if net:
                    break
                time.sleep(3)
        print(f"phone internet: {net}")
        phone.screenshot(str(out / "04-final.png"))
    finally:
        phone.screen_record_stop(str(out / "phone-demo.mp4"))
        log.terminate()

    ok = authed and net
    print("DEMO", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
