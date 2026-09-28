# COMFAST CF-WR632AX — OpenWrt 25.12.5 + TollGate image: flash & test

**Lane:** `scripts/comfast-cf-wr632ax/build-image.sh` (build) — this page is the tester's page
**Artifact:** `openwrt-25.12.5-mediatek-filogic-comfast_cf-wr632ax-squashfs-sysupgrade-tollgate-0.6.0-alpha4-pre19.bin`
**sha256:** `908d64c575c08c2623c12710811e9f4cb7c77fbb9deaae9cc2345897dc06a594`
**size:** 18,401,569 bytes (17.5 MiB)
**Status: BUILT, HASH-VERIFIED AND STATICALLY INSPECTED — NOT BOOTED.** No CF-WR632AX is in
hand. Do not read this page as a hardware verification of anything. See
[What is verified vs not](#what-is-verified-vs-not).

## What this image is

A stock-OpenWrt **sysupgrade** image for the COMFAST CF-WR632AX (MediaTek MT7981 class, WiFi-6
compact travel router, 128 MiB SPI NAND, target `mediatek/filogic`, arch
`aarch64_cortex-a53`), built with the official OpenWrt **25.12.5** ImageBuilder using the
**`comfast_cf-wr632ax` (stock layout)** profile, with the TollGate module
**`tollgate-wrt` `0.6.0_alpha4_pre19`** (from `FreedomTechFeed/packages`) baked in.

| what | how it is done | why |
|---|---|---|
| `nodogsplash`, `jq`, `libmicrohttpd-no-ssl`, `luci` | named in the ImageBuilder `PACKAGES=` line; the ImageBuilder resolves nodogsplash's dependency closure (iptables/xtables/kmod set) from the 25.12.5 feeds | they are in the official feeds |
| `tollgate-wrt` | **not** in `PACKAGES=` — it is not in the official feeds and would fail to resolve. The ADB `.apk` is shipped at `root/tollgate-wrt.apk` via `FILES=` and installed **offline on first boot** by `etc/uci-defaults/99-tollgate-firstboot`, which then removes the staged apk and itself | offline install, no WAN needed at first boot |

Because the module is installed on first boot rather than at image-build time, `tollgate-wrt`
appears in **no** package manifest. That is expected, and the build script asserts its presence
in the image rootfs and in the built squashfs instead of pretending the manifest covers it.

**OpenWrt >= 25.12.5 is a hard requirement for this device.** The OpenWrt U-Boot layout carried
a memory-speed stability issue in 25.12.0–25.12.4 (upstream PRs #22929 / #23416); it is fixed in
25.12.5. This image *is* 25.12.5.

**Layout choice.** The stock `comfast_cf-wr632ax` layout is used because it does **not** require
replacing the vendor bootloader — the safer default for a tester. The device also has a
`comfast_cf-wr632ax-ubootmod` (OpenWrt U-Boot) profile; it is not the default here, and the
memory-speed caveat above applied to it in earlier releases.

## Flash it

You need: the CF-WR632AX, a computer with an Ethernet port, and the image file.

### Path A — device still on stock COMFAST firmware (the documented "Ordinary" path)

The OpenWrt device page for this board says, for the stock layout:

> 1. Install the `openwrt-[version]-mediatek-filogic-comfast_cf-wr632ax-squashfs-sysupgrade.bin`
>    image using the stock WebUI update page.
> 2. Press and hold the reset button after reboot to wipe the stock config.

So: put the plain OpenWrt 25.12.5 stock-layout sysupgrade image on the vendor WebUI's update
page first, let the box come up on OpenWrt, and then sysupgrade **this** file from OpenWrt
(step 2 below). Wiping the stock config after the vendor→OpenWrt flash is part of the documented
procedure, not an optional step.

### Path B — device already running OpenWrt (or after Path A)

1. **Address your computer** on the router's LAN: set the wired interface to a static
   **`192.168.1.254/24`** (netmask `255.255.255.0`). No gateway is needed.
2. **Cable into the 1 GbE LAN port** of the CF-WR632AX (documented as the LAN port, not WAN).
3. **Browse to `http://192.168.1.1`** and log in (fresh OpenWrt: user `root`, empty password).
4. Upload this image in **System → Backup / Flash Firmware** with **"Keep settings" OFF** for a
   clean first boot. SSH equivalent:

   ```sh
   scp openwrt-25.12.5-mediatek-filogic-comfast_cf-wr632ax-squashfs-sysupgrade-tollgate-0.6.0-alpha4-pre19.bin root@192.168.1.1:/tmp/
   ssh root@192.168.1.1 'sysupgrade -n /tmp/openwrt-25.12.5-mediatek-filogic-comfast_cf-wr632ax-squashfs-sysupgrade-tollgate-0.6.0-alpha4-pre19.bin'
   ```

   The `-n` wipes config, which is what you want for a clean first boot.
5. The device reboots on its own (~1–2 min) and keeps the LAN address `192.168.1.1`.

### Path C — a device that will not take a direct flash (the all-in-UBI installer)

The OpenWrt device page documents an alternative for the **OpenWrt all-in-UBI layout**, which
switches the flash layout. Its installer lives in the third-party project
`andros-ua/owrt-ubi-installer` (branch `cf-wr632ax`), **not** in `downloads.openwrt.org`:

1. The router must already be running the **latest generic OpenWrt firmware**.
2. Get the installer: build it from <https://github.com/andros-ua/owrt-ubi-installer/tree/cf-wr632ax>
   or take a prebuilt one from <https://github.com/andros-ua/owrt-ubi-installer/releases>.
3. Assign `192.168.1.254/255.255.255.0` to your computer's Ethernet port.
4. Connect Ethernet to the **1 GbE LAN port**.
5. Open `http://192.168.1.1`.
6. Flash the **`openwrt*-ubi-initramfs-recovery-installer.itb`** with sysupgrade. **The installer
   is one-shot per device.** Wait for it to finish (the green status LED).

After Path C the device is on the all-in-UBI layout — a *different* layout from this image's
stock layout, so this sysupgrade file is the wrong file for a device that has been moved there.
Treat Path C as the recovery/fallback route only, and say in your report which path you used.

Note there is also a `openwrt-25.12.5-mediatek-filogic-comfast_cf-wr632ax-ubootmod-initramfs-recovery.itb`
in the OpenWrt release (sha256 `ff269983b6fc56b9044b055d244ad8d2e4bed0e2b5ff923525f583c293e589e4`);
that belongs to the **U-Boot-mod** layout, which is *not* what this image targets. Do not mix the two.

## What to look for after the first boot

Read the first-boot log **first** — it lives on tmpfs and disappears on reboot:

```sh
ssh root@192.168.1.1 'cat /tmp/tollgate-firstboot.log'
ssh root@192.168.1.1 'apk list --installed | grep tollgate-wrt'
```

| check | expected | how |
|---|---|---|
| LAN up + DHCP server | your computer gets a `192.168.1.x` lease, gateway `192.168.1.1` | `ip -4 addr` / `ip route` |
| SSH | reachable on `192.168.1.1:22` | `ssh root@192.168.1.1` |
| the module got installed | `apk list --installed` lists `tollgate-wrt-0.6.0_alpha4_pre19` | `ssh root@192.168.1.1 'apk info -v \| grep tollgate'` |
| TollGate advert | `http://192.168.1.1:2121/` returns `{"kind":10021,…}` | `curl -s -m 6 http://192.168.1.1:2121/ \| head -c 400` |
| TollGate session state | `http://192.168.1.1:2121/balance` answers, `session_active:false` when idle | `curl -s -m 6 http://192.168.1.1:2121/balance` |
| captive-portal splash | `http://192.168.1.1:2051/splash.html` answers `200` | browser / `curl -sI` |
| pre-auth redirect | a client the box has never seen, hitting `http://connectivitycheck.gstatic.com/generate_204`, gets **307** to the splash | from a **fresh MAC** — see below |
| admin surface | `:8080` answers; `:8090` must be **closed** to a pre-auth client | `curl -s -m 4 -o /dev/null -w '%{http_code}\n' http://192.168.1.1:8090/` → `000` |
| services | `nodogsplash` and `tollgate-wrt` running and enabled | `ssh root@192.168.1.1 '/etc/init.d/tollgate-wrt status; /etc/init.d/nodogsplash status'` |

Three things to know while reading those results:

- **Measure pre-auth behaviour from a MAC the box has never seen.** If your own machine's MAC
  lands in `nodogsplash.@nodogsplash[0].trustedmac` (the lab keepalive armour writes it on
  purpose), your machine is *exempt* from the captive portal by design: no interception, no OS
  sign-in prompt, free internet — which looks exactly like "the captive portal is broken". Check
  `ssh root@192.168.1.1 'uci get nodogsplash.@nodogsplash[0].trustedmac'` and `ndsctl clients`
  before concluding anything.
- **`/etc/config/nodogsplash` does not exist until nodogsplash's own uci-defaults have run.** The
  first-boot script creates the section before writing to it for exactly this reason; if the log
  shows a failure there, that ordering is the thing to look at.
- **A fresh image may legitimately serve `000` on `:8090`.** The module fails *closed*: while
  root's password hash in `/etc/shadow` is empty (the state a fresh OpenWrt ships in) it
  deliberately does not serve the admin board, because `rpcd` would accept *any* password. To get
  the board: `ssh root@192.168.1.1 passwd`, set a password, reboot, then look again.
- **Where LuCI lives is not verified on this image.** The project's convention on TollGate images
  is LuCI on **`:8080`** (uhttpd's main instance moved off `:80` so the captive portal owns `:80`).
  Check **both** `http://192.168.1.1` and `http://192.168.1.1:8080` and report which answered.

## How to report results

For each check above send: the raw command, its raw output, and **PASS / FAIL / INCONCLUSIVE**.
Always include:

- which image file you flashed (name) and `sha256sum` of the file you actually used;
- `cat /tmp/tollgate-firstboot.log` and `ubus call system board` from the booted device;
- which flash path you used (A/B/C) and whether the wired link came up at 1 GbE;
- a `logread` excerpt if anything looks wrong.

Say **INCONCLUSIVE** rather than PASS when a check could not be run at all. Do not report a
portal, payment, or firewall result you did not actually observe.

## Build it yourself (reproducible)

```sh
scripts/comfast-cf-wr632ax/build-image.sh
```

It builds on a **remote build host over SSH** (default `debian@23.182.128.219`, override with
`BUILD_HOST`) because the ImageBuilder tarball is ~513 MB and expands to ~1.6 GB — on a
workstation whose `/tmp` is a small tmpfs that fails with *Disk quota exceeded*. Only the finished
~18 MB image is copied back.

The script:

1. fetches the OpenWrt release `sha256sums` and pins the ImageBuilder's sha256;
2. fetches the module release's own `SHA256SUMS` asset from `FreedomTechFeed/packages` and pins
   the `.apk` sha256;
3. ships `remote-build.sh` plus a `files/` tarball to the build host, which downloads the
   ImageBuilder, **verifies it against those sums**, downloads the `.apk`, **cross-checks it
   against the release `SHA256SUMS` asset**, extracts, stages `files/`, builds, and then proves the
   baked-in apk inside the assembled rootfs is byte-identical to the verified apk;
4. copies the image and its `.manifest` back and re-verifies the sha256 locally.

Env overrides: `BUILD_HOST`, `SSH`, `SCP`, `REMOTE_DIR`, `OPENWRT_RELEASE` (default `25.12.5`),
`TOLLGATE_RELEASE` (default `v0.6.0-alpha4-pre19`), `TOLLGATE_ASSET`, `TOLLGATE_REPO`,
`WORK_DIR`, `OUT_DIR`. No secrets, no device access, no flashing.

### Build-host prerequisite that bites (measured)

The ImageBuilder's own prereq check needs **GNU awk specifically**. Debian ships `mawk` as
`/usr/bin/awk`, and the failure surfaces as a bare

```
Checking 'awk'... failed.
Build dependency: Please install GNU 'awk'
```

with no hint that the fix is `sudo apt-get install -y gawk`. `remote-build.sh` checks this up
front and names the fix. Other prerequisites: `make gcc g++ wget curl tar zstd sha256sum file`.

### A second measured trap: the 25.12 output layout

This ImageBuilder release writes to `bin/targets/mediatek/filogic/` **flat**, not
`bin/targets/mediatek/filogic/<profile>/` as older releases did. `remote-build.sh` accepts either.
(A build that finished perfectly was first read as "no output dir" because of this.)

## Where the artifact is published

**The image itself is NOT published** — no upload path accepted an 18.4 MB file. Stated plainly
rather than papered over:

- `https://drive.cashu.email` (the preferred host): the server requires Cashu payment for files
  over 1 MiB. Its 402 `creqA` CBOR names exactly three accepted mints —
  `https://rugs.cashu.exchange`, `https://rugs01.cashu.exchange`,
  `https://mint.minibits.cash/Bitcoin` — and **none of them auto-pays** its Lightning quote
  (all three sat `UNPAID` after 30 s of polling). So the upload cannot be completed from here
  without real Lightning capacity. (`testnut.cashu.exchange` mints fine but the server rejects it:
  `400 Untrusted mint`. The server's own `llms.txt` still lists testnut as accepted — it is stale.)
- `https://blossom2.orangesync.tech` (our self-hosted Blossom, on vps3): reachable and our key
  authenticates, but the server refuses with `413 File too large. Maximum allowed size is
  10485760 bytes`. 18.4 MB > 10 MiB.
- `https://blossom1.orangesync.tech`: unreachable (connection fails).

The image therefore lives **local only**, at:

```
~/artifacts/comfast-cf-wr632ax/openwrt-25.12.5-mediatek-filogic-comfast_cf-wr632ax-squashfs-sysupgrade-tollgate-0.6.0-alpha4-pre19.bin
sha256 908d64c575c08c2623c12710811e9f4cb7c77fbb9deaae9cc2345897dc06a594
size   18401569 bytes
```

To hand it to a tester, transfer it out of band and have them check `sha256sum` against the value
above before flashing.

### A metadata bundle WAS published (free tier) — it is not the image

The 16 KB side-car bundle published to the free tier is **not flashable**. Its only job is to let a
tester confirm that whatever file they were handed is the right bytes:

```
URL    https://drive.cashu.email/53bc115763d2eaf2b6f6f6722848253e0acd4e179cf87aeb7c2837b6533ef45f.gz
sha256 53bc115763d2eaf2b6f6f6722848253e0acd4e179cf87aeb7c2837b6533ef45f
size   16165 bytes
expiry 2026-10-05T00:29:25Z (7-day free-tier retention — re-upload if it lapses)
```

It contains `IMAGE-SHA256.txt` (the image's sha256/size), the image's `.manifest`, this page, and
the build + first-boot scripts. Retrieval was verified round-trip: a fresh `curl` of that URL
returns 16,165 bytes whose sha256 equals the value above. **Do not flash it.**

## What is verified vs not

**Verified by this build** (on the build host and on this workstation; **no device involved**):

- Both pins were fetched from their authoritative sources and checked:
  ImageBuilder sha256 `7fb6cf626582ebcbfb46974da48c1eae577213f38879eaf6b1d982041e843461`
  against the 25.12.5 release `sha256sums`; module apk sha256
  `a62ef5e60b219f4bcebf8980098cf2a3054e90c842aa9e239f2cb079cbe38ff1` against
  `FreedomTechFeed/packages@v0.6.0-alpha4-pre19`'s `SHA256SUMS`.
- The image was produced for `PROFILE=comfast_cf-wr632ax`; the build log's first line reads
  `Building images for mediatek - COMFAST CF-WR632AX`.
- The image's own sha256 was computed **on the build host** and again **on the copy** — they match:
  `908d64c575c08c2623c12710811e9f4cb7c77fbb9deaae9cc2345897dc06a594`, 18,401,569 bytes.
- The package manifest inside the image contains 181 packages, including `nodogsplash 5.0.2-r2`,
  `jq 1.8.1-r2`, `libmicrohttpd-no-ssl 1.0.2-r1`, `luci 26.268.33051~dd3d1cb` (13 `luci-*`
  packages), `base-files`, `dnsmasq`, `dropbear`, `firewall4`.
- The baked files are present in the **assembled rootfs** and in the **built squashfs**:
  `root/tollgate-wrt.apk` (mode 644, sha256 = the verified apk) and
  `etc/uci-defaults/99-tollgate-firstboot` (mode 755). The apk inside the image is byte-identical
  to the verified download.
- The module apk is `arch: aarch64_cortex-a53` — this device's arch. Nothing is cross-built.
- `bash -n` and `sh -n` pass on the build scripts and the first-boot script.
- vps3 was cleaned afterwards: `~/tg-build` (2.2 GB) deleted, free space back to the
  49 GB it started at; only logs kept in `~/tg-build-logs`.

**NOT verified — no CF-WR632AX is in hand:**

- **Nothing has been booted on hardware.** Not the first boot, not the offline apk install, not
  the service start, not the portal, not any payment.
- The image has never been flashed, and no flash path (A/B/C) has been exercised.
- The captive-portal surfaces — `:2121` advert, `:2051` splash, `:8080` admin, `:8090`
  closed-to-pre-auth — are **unproven on this device**. They are proven on the lab GL-MT3000,
  which is a different board with different Wi-Fi hardware.
- The `apk add --no-network --allow-untrusted --force-non-repository` closure completing in ONE
  transaction on first boot has not been observed on this device.
- LuCI's actual address on this image (`:80` vs `:8080`) is not measured.
- Nothing about Wi-Fi, the MT7981 radio, or 1 GbE link behaviour is measured.
- No captive-portal, splash-redirect, payment, or firewall-closure measurement exists for this image.

If you flash it you are the first hardware data point — please report per the section above.
