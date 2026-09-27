# Virtual WiFi Issues — Android Emulator + TollGate

## Issue 1: virtio-wifi-tap — bridge to mac80211_hwsim virtual AP
**Priority:** HIGH | **Labels:** enhancement, wifi, android-emulator

Use `-wifi-tap` to bridge the Android emulator to a virtual AP on the host.

**Findings from research:**
- Emulator v37.1.11 supports `-wifi-tap <interface>` for Virtio WiFi
- `mac80211_hwsim` creates virtual radios (wlan0 from phy#1, NOT hwsim0)
- Previous hostapd failure was due to using `hwsim0` (management interface) instead of `wlan0` (the actual radio)
- Need to: set wlan0 to AP mode, run hostapd on it, create TAP, restart emulator

**Steps:**
1. `sudo modprobe mac80211_hwsim radios=2` (one for AP, one for client)
2. `sudo iw phy phy1 interface add wlan-ap type ap` (create AP interface)
3. `sudo hostapd -B /etc/hostapd/tollgate.conf` (SSID=TollGate on wlan-ap)
4. `sudo ip tuntap add tap0 mode tap && sudo ip link set tap0 up`
5. Bridge tap0 to wlan-ap
6. Kill emulator, restart with: `emulator -avd easypark -wifi-tap tap0 -no-snapshot`
7. Android should see "TollGate" in WiFi settings

---

## Issue 2: emulator wifi add command — upgrade or find the flag
**Priority:** HIGH | **Labels:** enhancement, wifi

The emulator binary contains `wifi add <ssid> [password]` console commands but
they're not exposed in the current version's console.

**Findings:**
- `strings qemu-system-x86_64-headless | grep wifi` shows:
  - `'wifi add <ssid> [password]' will add a new SSID named <ssid> that will be visible`
  - `'wifi block <ssid>' will block network access on the given SSID`
  - `'wifi unblock <ssid>' will unblock network access on the given SSID`
- But `adb emu wifi add TollGate` returns "unknown command"
- Feature documented as available from emulator 36.5+ (we're on 37.1.11)

**Steps:**
1. Try the emulator canary channel: `sdkmanager --channel=1 emulator`
2. Check if the feature requires a specific system image (API 35+)
3. Or build emulator from source with the feature enabled

---

## Issue 3: root binary-patch WiFi HAL to rename AndroidWifi
**Priority:** MEDIUM | **Labels:** enhancement, wifi, root

We have root (`su 0`) on the emulator. The SSID "AndroidWifi" is hardcoded
in the Virtio WiFi HAL binary inside the Android system image.

**Findings:**
- `su 0` works (uid=0 root confirmed)
- SSID not in any text config file — it's compiled into the HAL
- Both strings are 11 chars ("AndroidWifi" vs "TollGate" is 8 — need padding or different approach)

**Steps:**
1. Find the HAL binary: `find /vendor /system -name "*wifi*" -type f`
2. Extract it: `adb pull /vendor/lib64/hw/android.hardware.wifi@1.0-default.so`
3. Binary-patch "AndroidWifi" → "TollGate\0\0\0" (pad with nulls)
4. Push back: `adb push patched.so /vendor/lib64/hw/`
5. Restart the WiFi service

---

## Issue 4: redroid — Docker-based Android with virtual WiFi
**Priority:** MEDIUM | **Labels:** enhancement, wifi, docker

redroid (remote Android, https://github.com/remote-android/redroid-doc) runs
Android in a Docker container and has active development on virtual WiFi.

**Findings from redroid issue #791:**
- They're working on virtual WiFi using network namespaces
- Can create `wlan0` and `wlan1` as virtual devices
- Would allow custom SSIDs without real WiFi hardware

**Steps:**
1. Deploy redroid on ai-legion: `docker run -d redroid/redroid:14.0.0-latest`
2. Configure virtual WiFi per their documentation
3. Set SSID to "TollGate"
4. Connect to the QEMU virtual lab's network
5. The captive portal would work naturally

---

## Issue 5: Genymotion with bridged virtual network
**Priority:** LOW | **Labels:** enhancement, wifi, genymotion

Genymotion runs Android in a proper VM and can bridge to the host network.
Set up Genymotion on ai-legion with a bridge to the QEMU virtual lab.

**Steps:**
1. Install Genymotion Desktop on ai-legion
2. Create an Android 14 VM
3. Configure bridged networking to the virtual lab's bridge interface
4. The phone would see "TollGate" from the OpenWrt router's AP
5. Full captive portal experience

---

## Recommended execution order:
1. **Issue 2** (emulator update) — lowest effort, highest probability
2. **Issue 1** (virtio-wifi-tap) — medium effort, real WiFi interface
3. **Issue 3** (binary patch) — medium effort, guaranteed to work
4. **Issue 4** (redroid) — medium effort, most realistic
5. **Issue 5** (Genymotion) — highest effort, most polished


---

## EXECUTION RESULTS (2026-09-15)

### Issue 1 (virtio-wifi-tap): ❌ DOES NOT WORK AS EXPECTED
- hostapd on mac80211_hwsim **successfully** broadcasts SSID "TollGate" (AP-ENABLED)
- Emulator accepts the `-wifi-tap` flag and boots normally
- BUT: the Virtio WiFi device does NOT scan for external APs
- It always shows "AndroidWifi" (the built-in simulated AP)
- Root cause: `-wifi-tap` is designed for emulator-to-emulator WiFi forwarding
  (`-wifi-server-port`/`-wifi-client-port`), not for bridging to host APs
- The Virtio WiFi operates at QEMU's abstraction level, which is different
  from the 802.11 frames that hostapd on hwsim produces

### Issue 2 (emulator wifi add): NOT YET TRIED
- The `wifi add` command IS in the emulator binary strings
- Not exposed in the console of v37.1.11
- May need a canary/beta emulator or a specific system image
- **NEXT STEP: try `sdkmanager --channel=1 emulator` to get the canary**

### Issue 3 (root binary patch): MOST PROMISING SHORT-TERM
- `su 0` gives full root access
- The SSID is in the Virtio WiFi HAL binary inside the system image
- Binary-patching "AndroidWifi" → "TollGate\0\0\0" (pad with nulls)
- This is the fastest guaranteed path to showing "TollGate" in WiFi settings

### New finding: emulator-to-emulator WiFi
The `-wifi-server-port` and `-wifi-client-port` options connect two emulator
instances via WiFi. One could act as a "router" emulator (running OpenWrt
in QEMU with WiFi) and the other as the "phone" emulator. This is the
intended use case for Virtio WiFi and is worth exploring.

### Recommended next steps (in order):
1. **Binary-patch the WiFi HAL** (Issue 3) — fastest, guaranteed
2. **Try canary emulator** (Issue 2) — the feature is in the binary
3. **Emulator-to-emulator WiFi** — most architecturally correct
4. **redroid in Docker** (Issue 4) — good long-term solution

---

## EXECUTION RESULTS — SESSION 2 (2026-09-15, afternoon)

### Issue 2 (emulator wifi alternatives): ❌ ALL DEAD ENDS
- `-wifi-socket` — silent, data plane only, no visible AP rename
- `-wifi-server-port`/`-wifi-client-port` (emulator-to-emulator) — still shows "AndroidWifi"
- `settings put global wifi_simulated_ap_name TollGate` — no effect on API 34
- `adb emu wifi add` — still "unknown command" in console of v37.1.11
- Conclusion: emulator Virtio WiFi cannot show a custom SSID without binary patching

### Issue 3 (binary patch): 🚫 DECLINED BY USER
- User: "i dont want to do the binary patching i only want to do it if it is real or will be useful to us in the future"
- It would be a visual-only hack against a vendor image; not useful beyond the demo

### Issue 4 (redroid + hwsim): ✅ EXECUTED — L2 PROVEN, L3/UI BLOCKED
**Setup that worked (on ai-legion):**
1. `modprobe mac80211_hwsim radios=2` → wlan0 (phy5, host), wlan1 (phy6)
2. hostapd on host wlan0 broadcasting SSID "TollGate" (AP-ENABLED)
3. `docker run -d --name redroid-tollgate redroid/redroid:14.0.0-latest` (Android 14)
4. Moved wlan1 into container netns: `iw phy phy6 set netns <pid>`
5. Inside container (`docker exec`): `iw scan` finds "TollGate" at -30 dBm
6. Inside container: `iw connect TollGate` → **L2 association succeeds**

**Proven evidence:**
- `iw dev wlan1 link` inside container: `Connected to 02:00:00:00:00:00, SSID: TollGate, freq: 2437, signal: -30 dBm, RX: 537101 bytes (11892 packets)`
- Management frames + data frames flow through the hwsim virtual medium

**Blockers (redroid limitations, not fixable without custom Android build):**
- ❌ L3 data path: ping/HTTP through hwsim medium fails (frames counted at L2 but not routed)
- ❌ WiFi Settings UI shows "off" — redroid has no WiFi HAL/framework integration
- ❌ Container browser can't reach host HTTP server (Docker bridge 172.17.0.1:8090 → ERR_ADDRESS_UNREACHABLE)
- ❌ file:// URLs won't open in browser (Error type 3 — neither `com.android.webview/.WebViewActivity` nor `org.chromium.webview_shell/.WebViewShellActivity` resolves)
- Note: ADB over TCP to redroid shows "offline" — use `docker exec` instead
- Note: `iw scan` takes 5-10s inside container — avoid in timeout-sensitive SSH commands; use `iw link` (instant)

### Final outcome: demo composed from hybrid evidence
Since no single environment renders the full story, the user story film combines:
1. **WiFi evidence card** (animated `iw link` terminal output) — proves TollGate association at kernel level
2. **Playwright portal + router logs** (split-screen, laptop) — proves the captive portal UX
3. **Close card** — summarises the 4-step happy path

Deliverable: `demo/footage/final/virtual-wifi-story.mp4` (87s, 5.5MB, narration QA PASS)

### Verdict per approach
| Approach | SSID visible | L2 connect | L3 data | Settings UI | Verdict |
|---|---|---|---|---|---|
| Emulator default | AndroidWifi | n/a | ✅ | ✅ | wrong SSID |
| Emulator -wifi-tap/-wifi-socket/ports | AndroidWifi | n/a | ✅ | ✅ | wrong SSID |
| Emulator wifi_simulated_ap_name | AndroidWifi | n/a | ✅ | ✅ | no effect |
| Binary patch HAL | TollGate (visual) | n/a | ✅ | ✅ | declined by user |
| **redroid + hwsim** | **TollGate** | **✅ (iw)** | ❌ | ❌ | **best real WiFi proof; L3/UI blocked** |

**If full in-Android TollGate WiFi is ever needed:** build a redroid image with a
mac80211_hwsim-backed WiFi HAL (or use a Cuttlefish virtual device, which supports
`-wifi_tap` natively in newer versions). That is real engineering value beyond this demo.

---

## EXECUTION RESULTS — SESSION 3 (2026-09-16, Cuttlefish): ✅ FULL STACK WORKS

### Verdict: Cuttlefish delivers everything the emulator and redroid could not

**Architecture (all on ai-legion, zero real WiFi):**
```
Android VM (Cuttlefish aosp_cf_x86_64_only_phone, build 16102939, aosp-android-latest-release)
  └─ real Android WiFi stack (wificond + wpa_supplicant)
      ↕ virtio-wifi ↔ wmediumd shared medium
OpenWrt AP VM (run_cvd-managed, crosvm, mac80211_hwsim radios)
  ├─ SSID: TollGate (uci-set, was VirtWifi)
  ├─ dnsmasq (DHCP + probe-DNS hijack)
  └─ SSH root@192.168.94.2 (host tap cvd-wifiap-01)
ai-legion host
  └─ captive-portal control plane (:80) — walled-garden redirect,
     Cashu token verification against https://signut.cashu.exchange (signet mint)
```

**Proven working, end to end:**
1. Android Settings scans and finds **TollGate** (real scan results via hwsim medium)
2. Connect: supplicant COMPLETED, DHCP lease 192.168.99.99, 802.11ac
3. WiFi Settings UI shows "TollGate — Connected / No internet access" + lock-screen notification
4. Native captive-portal detection: `NetworkMonitor isPortal()=true RedirectUrl=/portal-mobile.html`
5. Native **"Sign in to Wi-Fi network TollGate"** notification → tap → CaptivePortalLogin opens the TollGate portal (URL bar shows intercepted www.google.com)
6. Portal payment: token pasted ON THE PHONE → router verifies proofs live against the signet Cashu mint → `{"ok":true,"paid":21,"toll":4}`
7. Double-spend: same token second attempt → 402 rejected
8. Gate opens: /generate_204 now answers 204 → Android revalidates
9. Two portal skins tested e2e: **TollGate** and **Nets4Sats** (both paid 21 real signet sats)
10. Full phone screenrecord of the flow + composed documentary film with narration

**Key configuration that made it work (the recipe):**
- `adb shell settings put global captive_portal_http_url http://192.168.94.1/generate_204` (+ https same) — bypasses DNS entirely for probes
- Host route: `ip route add 192.168.99.0/25 via 192.168.94.2 dev cvd-wifiap-01`
- Host firewall: `ufw allow in on cvd-wifiap-01 to any port 80` (default DROP was silently eating SYNs — the longest debug of the session)
- Walled garden: redirect ALL HTTP paths to the portal; after payment /generate_204 returns real 204
- Signet test ecash: `POST /v1/admin/grants` (admin key from `.operator-secrets/admin-keys.json`) creates a born-paid NUT-04 quote; mint proofs with cashu-ts `wallet.mintProofsBolt11(amount, quoteId)`

**Bugs/quirks hit (filed as GitHub issues):**
- ci.android.com artifacts GC'd on aosp-master; release branch retains them; signed URLs embedded as `\u0026` JSON escapes in viewer HTML
- dnsmasq on the CF OpenWrt: `address=/#/` catch-all stops answering after restarts (NXDOMAIN) — worked around by making everything IP-literal
- screenrecord mp4 unfinalized if pulled before process exit (moov missing); source file has trailing corruption — trim with `-t 174`
- CaptivePortalLogin can't be started manually (`Unexpected null CaptivePortal` — needs Parcelable extra); notification tap is the only path, and STALE notifications don't fire — bounce WiFi for a fresh one
- The WebView JS `fetch` in CaptivePortalLogin reaches the portal via the default (ethernet) network, not the captive WiFi — payment still verified correctly (same server) but noteworthy
- adb consumes stdin: remote scripts via SSH heredocs die at the first adb call — always `< /dev/null`

**Artifacts:**
- Film: `demo/footage/final/tollgate-captive-flow.mp4` (115s, phone + router terminal, narrated)
- Raw phone footage + screenshots: `demo/footage/evidence/captive-flow/`
- Rig: `demo/tools/` (tg-movie.sh, captive-server.py, portal skins, signet-grant-mint.mjs)

---

## SESSION 3B (2026-09-16 later): REAL net4sats portal + full internet gate

Upgraded from the mock portal to the **real** `net4sats/net4sats-captive-portal-site` React app,
driven by a net4sats-protocol emulator on :2121 (details event kind 10021 / whoami / signed
kind-21000 payment events), verified against the signet mint. Router-firewall gate model:
pre-payment only the portal is reachable (nft forward_wifi0 restricted), payment opens it.

### Proven in final take (v3)
- Fresh phone boot → WiFi Settings → **Net4Sats** SSID joined visibly via UI taps
- Native sign-in notif → CaptivePortalLogin: **"Sign in to Net4Sats" at net4sats.cash**
  (SSID rename + DNS hijack + captive_portal_http_url)
- Real portal: token validated locally ("Pay 21 sat to get 52.50 minutes" — correct
  allocation math from the advertised price tags), nostr payment event POSTed
- Router: proofs checked @signut → `payment_accepted` → **fw4 gate open 63ms later**
- Android validates (clean WiFi icon) → **Wikipedia fully loads** through
  guest → virtual WiFi → AP (gate) → host NAT → internet (~22ms)
- Synchronized audit timeline: router event log (jsonl, epoch ms) + phone timeline
  rendered as timestamped terminal panel; REC T+MM:SS badge on the composed film

### Host networking chain (the three stacked blockers — see issue #121 comment)
ufw INPUT DROP → `inet vps_killswitch` policy drop (priority before ufw!) → missing
nft MASQUERADE for the AP WAN IP. All fixed; symmetrical accept rules for the tap.

### Open items (filed)
- `OpenTollGate/tollgate#18` — SSID naming + checksum spec (WIFI-01 reserved)
- `net4sats/net4sats-captive-portal-site#1` — "Access purchased52.50 minutes" i18n spacing
- `#124` — CaptivePortalLogin WebView fetch exits via default network (payment reached
  the router via the ethernet path, not the tolled WiFi — visible in server logs)

### Known cosmetic gaps in v3 film
- `show_taps`/`clock_seconds` settings were applied too early after boot (SystemUI not
  up) — no tap dots, status-bar clock without seconds. Timestamps are carried by the
  REC badge + terminal clock instead. Fix for next take: set after `boot_completed`
  + 10s or restart SystemUI.
- REC badge overlaps nothing now (bottom-center), but ffmpeg on the laptop lacks
  drawtext — overlays are pre-rendered PNG frames (see compose scripts).

---

## SESSION 3C (2026-09-16 evening): BYTES MODE + chapter-locked director

### Bytes-based allocation (user story: pay for MB, watch it run out)
- Router advertises `metric=bytes`, `step_size=2621440` (2.5 MiB), 4 sat/step.
  8-sat token → 5 MiB allocation.
- Enforcement is REAL nftables accounting on the AP:
  - The gate rules must live in the **parent `inet fw4 forward` chain** — the
    `ct state established` shortcut at its top makes per-chain quota rules see
    only connection handshakes (~20 KB per session, invisible video traffic).
  - Quota matched only `ip saddr` (uploads). Downloads bypass — use **dual
    direction counters** (`n4s-up`/`n4s-down` comment-tagged) with a 1 s poller
    that closes the gate (`reject`) when used ≥ quota.
  - nft quota renders `5 mbytes` (not bytes) in listings; rule deletion needs
    `delete rule … handle N` (not bare N); cleanup must match ALL rule comments.
- Phone-side: `svc data disable` is required — Android otherwise streams via the
  virtual cellular network and the quota stays idle (root cause of two "silent"
  takes). AOSP WebView lacks H.264 — the metered test video is WebM/VP9
  (test-videos.co.uk BBB 10 MB): plays, bursts ~8.6 MiB against the 5 MiB quota,
  poller fires `allocation_exhausted` → `gate_closed_by_quota` → stream stalls,
  wikipedia retry fails. All in the event log.

### Chapter-locked director (`demo/tools/director.py`)
Answers the "phone and logs must be in sync when we cut" requirement:
- Cuts the phone recording at **exact event boundaries** (from the run timeline +
  router event jsonl, one shared clock).
- Each chapter renders a terminal card containing **only that chapter's log
  lines** with router wall-clock stamps + a chapter header (CH n — title —
  time window).
- Narration is sequential **by construction**: each chapter is padded
  (freeze-frame) to at least narration length + gap → overlapping voices are
  impossible.
- Rebuilt final: `demo/footage/final/net4sats-bytes-flow.mp4` (127 s, 8 chapters,
  boot → join → captive → pay → gate → internet → streaming → exhausted).

### Bugs squashed this session (worth remembering)
- `tg-*.sh` deployment: `pkill -f` patterns match the deploying shell itself →
  use `[n]et4sats-…` bracket trick.
- director.py: `line[11:]` off-by-one on `REC_START=` parsing → 1e9-second
  offsets; hstack needs equal heights (vstack header onto terminal first).
- sed swap silently matched nothing (wrong base file) → Sintel page returned;
  always `grep -c` after generated-file transforms.

---

## LESSON LEARNED — shipping a film with overlapping narration (LL-001)

**What happened:** `net4sats-captive-flow-v3.mp4` shipped with narration scenes
on fixed hand-picked offsets. Scene 1 (15.6 s of audio in a 13 s slot) and
scene 4 (13.5 s in a 10 s slot) overlapped the next scene's voice — audible
double-narration. The mix was produced without any measurement of audio
durations against slot sizes; the earlier "gap QA" print existed only in an
older script and was not run for this film.

**Root cause:** narration placement was asserted by intent ("these offsets
look right"), never verified by data. Audio durations were known (ffprobe)
but not compared against slots; nothing in the build could fail.

**Fix shipped now:**
1. `demo/tools/voice_guard.py` — a build gate with two modes:
   - `plan` — BEFORE mixing: every scene's `lead + audio_duration + 0.25s gap`
     must fit its slot, and slots must not overlap. Exit 1 on violation.
     (Validated: run against the old v3 plan → FAIL with exact overflow numbers.)
   - `audit` — AFTER mixing: extracts speech blocks from the finished film via
     `silencedetect` and fails if any speech crosses a scene/chapter boundary.
2. `director.py` now runs the audit automatically after every build — a
   chapter film with bleeding narration cannot complete the build.
3. `make -f demo/Makefile.demo qa-voice FILM=… BOUNDARIES=…` for ad-hoc checks.
4. Fixed the v3 film: narration re-mixed onto guard-approved sequential
   offsets (3.0 / 20.5 / 34.8 / 51.2 / 66.6 / 81.2 s) — audit PASS.

**Design rule going forward:** narration slots are DERIVED from measured audio
durations (`slot_len = lead + audio + gap`, sequentially stacked), never
hand-picked constants. If a scene's video is shorter than its slot, the video
is freeze-frame extended (`tpad stop_mode=clone`) — pausing the picture, never
overlapping the voice. Chapter-locked films (director.py) get this by
construction; fixed-offset films MUST pass `voice_guard plan` before mixing.

**Boundary detection note:** `audit` proves no voice crosses a scene boundary
(= no overlap between scenes) and that each slot starts with silence. It
cannot detect two voices summed INSIDE one block — which is why the `plan`
check (pre-mix arithmetic) is the primary gate and per-scene single-source
mixing is the invariant.
