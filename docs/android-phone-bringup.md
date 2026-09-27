# Android Phone Bring-up — labgrid resource on the bench host

Status: LIVE (2026-09-25). Phone: **moto g(7) power**, Android 15,
serial `ZY326DPC7R` (adb identity; vendor 18d1:4ee7).

## What's wired

| Piece | Where |
|---|---|
| Exporter | `labgrid-exporter-rig.service` (systemd, enabled) — rig venv, name `ai-legion-small-rig`, config `configs/labgrid/exporter-android.yaml`, coordinator `192.168.13.208:20408` |
| Resources | `AndroidADDDevice {serial: ZY326DPC7R}` (ResourceEntry fallback — wire-safe scalars) + `NetworkService {192.168.105.150}` (reserved phone static on router-alpha's VLAN; required by the driver bindings) |
| Place | `android-phone` on the coordinator, match `*/android-phone/*` |
| Client env | `configs/labgrid/android-phone.yaml` (RemotePlace + AndroidADBDriver) |
| Test | `tests/test_android_labgrid.py` — skips cleanly with no phone; asserts model |

Why `AndroidADDDevice` and NOT labgrid's udev-matched `AndroidUSBDevice`:
labgrid 25.0.1's exporter has no export class for it, and a udev match dict
cannot cross the coordinator wire (see docs/labgrid-rig-exporter-adoption.md
and the ACR1252 precedent). adb runs CLIENT-side; the resource is the
acquisition/exclusivity token. Presence = `adb devices`.

## Physical checklist (plug a phone in)

1. **Use USB port `pci-0000:07:00.4-usb-0:2`** (where the moto is pinned
   today; any port works electrically — adb identity follows the serial, not
   the port — but keeping one designated port makes udev/ID_PATH stable:
   current ID_PATH `pci-0000:07:00.4-usb-0:2`, ID_SERIAL
   `motorola_moto_g_7__power_ZY326DPC7R`).
2. Phone: Settings → About → tap Build number 7× → Developer options →
   enable **USB debugging**.
3. Plug in; the phone shows the **RSA authorization prompt** — tick "always
   allow from this computer" and Allow. (If the prompt was dismissed:
   `adb kill-server && adb devices` to re-trigger.)
4. Verify: `adb devices` → `ZY326DPC7R\tdevice`.
5. Re-pin if the PHONE changes: put the new serial in
   `configs/labgrid/exporter-android.yaml` → `sudo systemctl restart
   labgrid-exporter-rig`. (A port change needs nothing; a serial="" means
   "the one phone on this host".)
6. If the exporter name ever collides again: name comes from `-n
   ai-legion-small-rig` (NOT `--hostname`, which only sets the published
   resource-access hostname — a collision with the bolty exporter's default
   name fails with grpc ALREADY_EXISTS and a clean exit 0 that looks like
   nothing happened).

## Labgrid commands

```bash
export PATH=~/src/fips-lab/.venv/bin:$PATH
C=192.168.13.208:20408
labgrid-client -x $C resources | grep rig          # presence of the export
labgrid-client -x $C -p android-phone acquire      # exclusivity
labgrid-client -x $C -p android-phone show         # bound resources
labgrid-client -x $C -p android-phone release
# Console equivalent: adb shell (via the driver: run/run_check/check
# tests/test_android_labgrid.py)
```

Test run (from PRTA root, rig venv):

```bash
TOLLGATE_SSH_HOST= ROUTER_IP= TOLLGATE_VIRTUAL_LAB= \
LG_COORDINATOR=192.168.13.208:20408 \
~/venvs/rig-labgrid/bin/python -m pytest \
  --lg-env=configs/labgrid/android-phone.yaml --no-deploy \
  tests/test_android_labgrid.py -v
```

## Tollgate-lab fixes this bring-up surfaced (committed, unpushed)

The android driver pair had never actually run: `AndroidADDDevice` didn't
subclass `Resource` (unconstructible via factory) and `AndroidADBDriver`
left `CommandProtocol`'s `wait_for`/`poll_until_success` abstract. Both
fixed with tests (`c11661d`, `d5cea79`).

## Mapping PRTA's existing phone tooling onto the resource

`lib/clients/adb.py::ADBDevice` = plain adb (no appium): shell, screencap,
`uiautomator dump` (ui_xml), `am start` (open_url), wifi_mac/wifi_ip, and
screenshot_portal's portal-keyword scan over UI XML. The suite
(tests/phone/: auto, captive_portal_auto, token_input, data_metering, ~20
files) drives it via the `adb` fixture (PHONE_SERIAL/PIN envs).

`AndroidADBDriver` already covers shell/screenshot/open_url/install_apk.
Gap to close for the full portal-UX lane (phone joins router-alpha's SSID →
detects portal → pays): port `ui_xml`/portal-scan/wifi_mac/wifi_ip onto the
driver (thin additions), wifi join via `cmd wifi connect <ssid> wpa2 <psk>`
(Android 15), the SSID/PSK wiring from router-alpha's adopted config, and a
payment method (browser to the portal + Cashu token input, or the wallet
app). MAC allowlisting on the router is owned by the router fixtures.
