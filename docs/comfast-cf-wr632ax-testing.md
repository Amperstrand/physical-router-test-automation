# COMFAST CF-WR632AX — OpenWrt 25.12.5 + TollGate image: flash & test

**Lane:** `scripts/comfast-cf-wr632ax/build-image.sh` (build) — this page is the tester's page
**Artifact:** `openwrt-25.12.5-mediatek-filogic-comfast_cf-wr632ax-squashfs-sysupgrade-tollgate-0.6.0-alpha4-pre19.bin`
**sha256:** `3172e688bf84692480f912d762acb3ceae5feaad79b935dfb0f0be128328f8ac`
**Size:** 17,961,249 bytes (≈ 17.1 MiB; the profile's image limit is 64 MiB)
**Status:** the image is **BUILT, HASH-VERIFIED AND STATICALLY INSPECTED — NOT BOOTED.** No
CF-WR632AX is in hand. Read [What is verified vs not](#what-is-verified-vs-not) before you
promise anyone anything.

## What this image is

A stock-OpenWrt **sysupgrade** image for the COMFAST CF-WR632AX (MediaTek MT7981 class,
WiFi-6 travel router, 128 MiB SPI NAND, target `mediatek/filogic`, arch
`aarch64_cortex-a53`), built with the official OpenWrt **25.12.5** ImageBuilder using the
**`comfast_cf-wr632ax` (stock layout)** profile, with the TollGate module
**`tollgate-wrt` `0.6.0_alpha4_pre19`** (FreedomTechFeed/packages) baked in.

Two mechanism notes, because they are the parts that can silently not work:

| what | how it is done | why |
|---|---|---|
| nodogsplash + jq + libmicrohttpd-no-ssl | named in ImageBuilder `PACKAGES=`; the ImageBuilder resolves nodogsplash's dependency closure (iptables/xtables/kmod set) from the 25.12.5 feeds | they are in the official feeds |
| `tollgate-wrt` | **not** in `PACKAGES=` — it is not in the official feeds and would fail to resolve. The ADB `.apk` is shipped at `root/tollgate-wrt.apk` via `FILES=` and installed **offline on first boot** by `etc/uci-defaults/99-tollgate-firstboot` (`apk add --no-network --allow-untrusted --force-non-repository /root/tollgate-wrt.apk`, one transaction), which then removes the staged apk and itself | offline install, no WAN needed at first boot |

Because the module is installed on first boot and not at image-build time, it appears in **no
package manifest** — that is expected, and the build script says so rather than pretending.

**OpenWrt >= 25.12.5 is a hard requirement for this device** (a memory-speed stability issue
on the OpenWrt U-Boot layout existed in 25.12.0–25.12.4; fixed in 25.12.5, upstream PRs
#22929 / #23416). This image is 25.12.5.

**Layout choice:** the stock `comfast_cf-wr632ax` layout is used because it does not require
replacing the vendor bootloader — the safer default for a tester. The device also has a
`comfast_cf-wr632ax-ubootmod` profile (OpenWrt U-Boot layout); that layout is an option, not
the default here, and older releases carried the memory-speed caveat on it.

## Flash it (stock layout, sysupgrade)

You need: the CF-WR632AX, a computer with an Ethernet port, the image file.

1. **Address your computer** on the router's LAN: set the wired interface to a static
   `192.168.1.254/24` (netmask `255.255.255.0`), no gateway required.
2. **Cable into the 1 GbE LAN port** of the CF-WR632AX (not the WAN port).
3. **Browse to <http://192.168.1.1>** — this is LuCI (on this image LuCI is on `:8080`,
   i.e. <http://192.168.1.1:8080>; the plain `:80` listener belongs to the captive portal).
4. Log in (fresh OpenWrt: user `root`, empty password) and go to
   **System → Backup / Flash Firmware**, keep "Keep settings" **OFF** for the first flash, and
   upload the `.bin`.
   CLI equivalent over SSH: `sysupgrade -n /tmp/openwrt-...-tollgate-....bin`
   (note the `-n` — it wipes the config, which is what you want for a clean first boot).
5. The device reboots on its own (~1–2 min). Its LAN address stays `192.168.1.1`.

**Device still on stock COMFAST firmware?** You cannot sysupgrade straight from the vendor
firmware over LuCI. Use COMFAST's/OpenWrt's OEM recovery-installer path first: the recovery
web UI reachable from a host at a static address after powering the device into recovery mode
(the vendor's recovery tool/`uhttpd` recovery page for this board), upload the plain OpenWrt
25.12.5 factory/initramfs image, and only then flash this TollGate image from OpenWrt. The
plain-OpenWrt reference images for this exact release are:
`openwrt-25.12.5-mediatek-filogic-comfast_cf-wr632ax-squashfs-sysupgrade.bin`
(sha256 `0ef234de428b7deed5c2d8bb813f5bb8ee1af4c556587b33a9d99d6996ce9b11`) and
`...-initramfs-kernel.bin` (sha256 `6c25c8c676689e2c63efff832d2166f52d9b069937fc5bc938150bd22b961d53`),
from <https://downloads.openwrt.org/releases/25.12.5/targets/mediatek/filogic/>.

## What to look for after the first boot

Read the first-boot log first — it is on tmpfs, so it disappears on reboot:

```sh
ssh root@192.168.1.1 'cat /tmp/tollgate-firstboot.log'
ssh root@192.168.1.1 'apk list --installed | grep tollgate-wrt'
```

| check | expected | how |
|---|---|---|
| LAN up + DHCP server | your computer gets a `192.168.1.x` lease; gateway `192.168.1.1` | `ip -4 addr` / `ip route` |
| SSH | reachable on `192.168.1.1:22` | `ssh root@192.168.1.1` |
| LuCI | `http://192.168.1.1:8080` answers | browser |
| TollGate API | `http://192.168.1.1:2121/` returns `{"kind":10021,...}` | `curl -s -m 6 http://192.168.1.1:2121/ \| head -c 400` |
| TollGate session state | `http://192.168.1.1:2121/balance` answers, `session_active:false` when idle | `curl -s -m 6 http://192.168.1.1:2121/balance` |
| captive portal splash | `http://192.168.1.1:2051/splash.html` answers `200` | browser / `curl -sI` |
| pre-auth redirect | a client the box has never seen, hitting `http://connectivitycheck.gstatic.com/generate_204`, gets **307** to the splash | use a *fresh MAC*, not your own machine (see below) |
| admin board | `:8080` is LuCI; `:8090` must be **closed** to a pre-auth client | `curl -s -m 4 -o /dev/null -w '%{http_code}\n' http://192.168.1.1:8090/` from a pre-auth client → `000` |
| services | `nodogsplash` and `tollgate-wrt` are running and enabled | `ssh root@192.168.1.1 '/etc/init.d/tollgate-wrt status; /etc/init.d/nodogsplash status'` |

**Measure the pre-auth behaviour from a MAC the box has never seen.** If your own machine's MAC
ends up in `nodogsplash.@nodogsplash[0].trustedmac` (the lab keepalive armour does this on
purpose), your machine is *exempt* from the captive portal by design: no interception, no OS
sign-in prompt, free internet — and it looks exactly like "the captive portal is broken".
Check it explicitly: `ssh root@192.168.1.1 'uci get nodogsplash.@nodogsplash[0].trustedmac'`
and `ndsctl clients`.

**About the `:8090` admin board on a fresh flash.** The module fails *closed*: while root's
password hash in `/etc/shadow` is empty — the state a fresh OpenWrt ships in — it deliberately
does **not** serve the admin board, because rpcd would accept *any* password. So `:8090`
answering `000` on a fresh image is the documented, correct behaviour, not a bug. If you want
the board: `ssh root@192.168.1.1 passwd`, set a password, then reboot before expecting `:8090`.

## Where the image is (hand-over)

The image is **not** on any public host — no working upload path existed at build time, and no
substitute host was improvised:

| host | outcome |
|---|---|
| `drive.cashu.email` (the Blossom host the feed's uploads live on) | **blocked**: 17.1 MiB needs payment (19 sats), and the mint the client tries is not accepted — `400 Untrusted mint`. The server's accepted mints do not auto-pay, and there is no funded Lightning capacity. |
| `blossom2.orangesync.tech` (our own Blossom) | **blocked**: `413 File too large. Maximum allowed size is 10485760 bytes` — a 10 MiB cap, the image is 17.1 MiB. |

So the image exists **only on the build host**:

```
~/artifacts/comfast-cf-wr632ax/openwrt-25.12.5-mediatek-filogic-comfast_cf-wr632ax-squashfs-sysupgrade-tollgate-0.6.0-alpha4-pre19.bin
sha256 3172e688bf84692480f912d762acb3ceae5feaad79b935dfb0f0be128328f8ac
17961249 bytes
```

A **metadata-only** bundle (manifest, sha256, this page, the build script, the first-boot script —
no image) was uploaded to the free tier so a tester can check what they should have received:

```
https://drive.cashu.email/082adfe79ef41ade41d4c432f8487308501931cca7a89d3fb40fe3841f765dc3.gz
sha256 082adfe79ef41ade41d4c432f8487308501931cca7a89d3fb40fe3841f765dc3   11493 bytes
```

⚠️ That URL is the **tar.gz of metadata**, 7-day free-tier retention. It is **NOT the image** —
do not flash it. Rebuild the image with the script, or hand over the file from the build host and
verify its sha256 against `IMAGE-SHA256.txt` inside the bundle.

## How to report results

Reply with, per check above: the raw command, its raw output, and PASS/FAIL/INCONCLUSIVE.
Always include:

- which image file you flashed (name) and its sha256 (`sha256sum` on the file you used);
- `cat /proc/cpuinfo | head -30`, `ubus call system board`, and
  `cat /tmp/tollgate-firstboot.log` from the booted device;
- a `logread` excerpt if anything looks wrong.

Say "INCONCLUSIVE" rather than PASS when a check could not be run at all. Do not report a
payment/portal result you did not actually observe.

## Build it yourself (reproducible)

```sh
scripts/comfast-cf-wr632ax/build-image.sh
```

The script downloads the ImageBuilder, **verifies it against the release `sha256sums`**,
downloads the tollgate `.apk` from `FreedomTechFeed/packages@v0.6.0-alpha4-pre19` and verifies
it against that release's `SHA256SUMS`, stages `files/` (the apk + the first-boot script),
builds the image, copies it to `~/artifacts/comfast-cf-wr632ax/`, checks the manifest for the
expected packages, and prints the sha256 + size. It is `set -euo pipefail`, idempotent (reuses
a hash-correct download), and needs no secrets and no device.

Env overrides: `OPENWRT_RELEASE` (default `25.12.5`), `TOLLGATE_RELEASE` (default
`v0.6.0-alpha4-pre19`), `WORK_DIR`, `OUT_DIR`.

## What is verified vs not

**Ran during this build** (on the build host, no device):

- ImageBuilder downloaded and its sha256 checked against the release `sha256sums`
  (`7fb6cf626582ebcbfb46974da48c1eae577213f38879eaf6b1d982041e843461`) — matched.
- The tollgate `.apk` sha256 checked against the release `SHA256SUMS`
  (`a62ef5e60b219f4bcebf8980098cf2a3054e90c842aa9e239f2cb079cbe38ff1`), and the artifact
  inspected with apk-tools **3.0.5** (the version OpenWrt 25.12.5 ships):
  `apk verify` → OK, `apk adbdump` → `name: tollgate-wrt`, `version: 0.6.0_alpha4_pre19-r1`,
  `arch: aarch64_cortex-a53`, `depends: jq, libc, nodogsplash` — exactly this device's arch, so
  nothing is cross-built.
- The sysupgrade image was produced for `PROFILE=comfast_cf-wr632ax`; the ImageBuilder's own
  `sha256sums` and `profiles.json` for it agree with the recorded digest and size,
  and `profiles.json` reports `file_size_limits.image = 67108864` (64 MiB) — the image is far
  under it.
- The image's package manifest was inspected: `base-files 1711~f5dae5ece4`,
  `dropbear 2025.89-r3`, `jq 1.8.1-r2`, `libmicrohttpd-no-ssl 1.0.2-r1`, `nodogsplash 5.0.2-r2`
  all present.
- The sysupgrade container was unpacked and its squashfs rootfs unsquashed, then inspected:
  - `/root/tollgate-wrt.apk` is present and its sha256 **inside the image** is
    `a62ef5e60b219f4bcebf8980098cf2a3054e90c842aa9e239f2cb079cbe38ff1` — byte-identical to the
    verified release asset;
  - `/etc/uci-defaults/99-tollgate-firstboot` is present and byte-identical to the script in
    this repo;
  - `/etc/init.d/nodogsplash`, `/usr/bin/ndsctl` and `/etc/config/nodogsplash` are present
    (nodogsplash's own uci-defaults script `40_nodogsplash` is in `/etc/uci-defaults/`);
  - `/etc/config/dhcp` carries `config dhcp lan` with `start 100 / limit 150` — the DHCP server
    is on for LAN. The LAN address itself (`192.168.1.1`) is not a file in the image on 25.x:
    it is generated at first boot by `/etc/board.d/02_network`, which this image ships and which
    matches the `comfast,cf-wr632ax` board. **That this resolves to `192.168.1.1` is therefore a
    build-host inference, not an observation** — the first thing a tester should confirm.
- `bash -n` and `sh -n` / `busybox ash -n` pass on the build script and the first-boot script.

**NOT verified — no CF-WR632AX is in hand:**

- **Nothing has been booted on hardware.** Not the first boot, not the offline apk install, not
  the portal, not the payment path, not the `:8090` pre-auth closure on this device.
- The image has not been flashed at all, and has not been tested from the OEM recovery path.
- The TollGate module's own service startup, kind:10021 API, splash and admin surfaces are
  **unproven on this device**; they are certainly proven on the lab GL-MT3000, which is a
  different board.
- No captive-portal, splash-redirect, payment, or firewall-closure measurement exists for this
  image.

If you flash it, you are the first hardware data point — please report per the section above.
