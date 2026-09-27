# Flashing OpenWrt + TollGate onto a Cudy WR3000 **v1** — the two-stage lane

**Lane:** `scripts/cudy-flash.py` (logic in `lib/cudy_flash.py`, tests in `tests/unit/test_cudy_flash.py`)
**Status:** stages **1 and 2 are VERIFIED ON HARDWARE (2026-09-27)** on a real Cudy
WR3000 v1. The stage-1 vendor upload endpoint/selector and the stage-2 sysupgrade path
below are pinned from that run. The flash-capacity preflight is measured on that run;
the **volatile (tmpfs) install is implemented and unit-tested but NOT hardware-verified**.
See [What is verified vs not](#what-is-verified-vs-not).

The operator's requirement was: *"the physical router testing kit should have the
ability to flash OpenWrt and TollGate on these Cudy routers."*

## Why two stages

CudyOS (the vendor firmware) **signs its images** and ships no signature-disable
build, so its web UI refuses a stock OpenWrt sysupgrade. The OpenWrt Table of
Hardware page's "OEM easy installation" section gives a way through with **no
case opening and no UART**:

1. the vendor UI *does* accept **Cudy's own signed OpenWrt**, a *transitional*
   image — CudyOS dies, a real OpenWrt remains;
2. once a real OpenWrt is running, the ordinary mainline `sysupgrade` path is
   available — flash the release we actually want;
3. install TollGate through the kit's existing install path (or volatile, below).

The alternative route (serial UART 115200 8N1 3.3V → U-Boot `0` at power-on →
TFTP `cudy3000.bin` → `tftpboot 0x46000000 …; bootm` → sysupgrade, upstream
commit `c9cb6411c1a7`) is the *recovery* route and needs the case open. This lane
is the no-case route.

## Stage 1 — the vendor upload (VERIFIED 2026-09-27)

The vendor firmware UI is **not** a plain LuCI page. Its shape, pinned from the run:

| what | value |
|---|---|
| log in | `POST /cgi-bin/luci/` with `luci_username=admin` + the lab UI secret |
| the door | `/cgi-bin/luci/admin/panel` → the **Firmware modal** |
| **the fragment trap** | a bare `GET /cgi-bin/luci/admin/system/upgrade` returns a ~2.6 KB fragment where `window.upload_file` is **undefined**. The **modal** is what loads the upgrade JS — do not treat the fragment as the page. |
| the file input | `input[name=cbid.upgrade.1.firmware]` (`accept=".bin"`), served inside the modal |
| step 1 (upload) | attach the image → the page's own `onchange` POSTs it to `POST /cgi-bin/luci/admin/system/upgrade` → **HTTP 200** |
| step 2 (`Proceed`) | a **`Proceed`** button then appears → clicking it POSTs again → **HTTP 302** |
| step 3 (reboot) | the 302 drives `/cgi-bin/luci/admin/system/reboot?upgrade=` then `/reboot/apply?upgrade=true` |

The lab UI secret is supplied by environment only
(`CUDY_PASSWORD` / `TOLLGATE_LUCI_PASSWORD`); there is no literal in this repo's code.

The lane (`oem-upload`) reproduces this **two-step** exchange and treats *upload 200 →
Proceed 302* as the pass (`cf.classify_stage1_sequence`). It uses a **no-redirect** HTTP
opener so the 302 is observable rather than chased. `--dump-page` still logs the real
panel HTML + the pinned selectors and uploads nothing.

**Result of stage 1 (observed):** the box rebooted onto `192.168.1.1` = OpenWrt
**SNAPSHOT r22906-c9cb6411c1**, board `cudy,wr3000-v1`, `aarch64_cortex-a53`, reachable
by **`ssh root` with an EMPTY password** (Wi-Fi interfaces DISABLED). No case opening,
no UART.

## Stage 2 — mainline sysupgrade (VERIFIED 2026-09-27)

These builds ship **no `sftp-server`**: `scp`/`sftp` FAIL. The transport is an ssh
stdin redirect, and the sha256 is re-checked **on the device** before the flash:

```sh
# stage the image (ssh stdin redirect — no sftp-server on these builds)
sshpass -e ssh root@192.168.1.1 'cat > /tmp/sysup.bin' < openwrt-25.12.5-mediatek-filogic-cudy_wr3000-v1-squashfs-sysupgrade.bin
# verify ON DEVICE against the pinned hash
ssh root@192.168.1.1 'sha256sum /tmp/sysup.bin | cut -d" " -f1'
# -T checks the image, then -n flashes it (config NOT kept)
ssh root@192.168.1.1 'sysupgrade -T /tmp/sysup.bin'
ssh root@192.168.1.1 'sysupgrade -n /tmp/sysup.bin'
```

**Result of stage 2 (observed):** the box came back on `192.168.1.1` as OpenWrt
**25.12.5 r33051-f5dae5ece4**, board `cudy,wr3000-v1`. (The lab root password `test123`
was then set — the offline installer refuses an empty password.)

## Hardware facts (verified)

| fact | value |
|---|---|
| model | Cudy **WR3000 v1 (R31)** — label reads `WR3000 V1.0` |
| SoC / target | MediaTek **MT7981B**, `mediatek/filogic`, `aarch64_cortex-a53` |
| flash / RAM | 16 MB SPI-NOR / 256 MB |
| factory LAN | `192.168.10.1`, CudyOS (LuCI-derived vendor skin) |
| factory open ports | 53 / 80 / 443 only — **no SSH, no telnet** |
| after flashing | `192.168.1.1`, **root ssh unauthenticated by default** (see trap 7), Wi-Fi **DISABLED** |
| Go/Rust arch token | `aarch64_cortex-a53` |

## Image provenance and hashes (both verified)

| stage | file | bytes | sha256 | build |
|---|---|---|---|---|
| 1 (transitional, Cudy-signed) | `openwrt-mediatek-filogic-cudy_wr3000-v1-sysupgrade.bin` | 9964331 | `8ee579d1b970488ee06f47964ac27ef88bc627b563cea00b88f1cb3a2917ec64` | OpenWrt **SNAPSHOT r22906-c9cb6411c1**, board `cudy_wr3000-v1`, `supported_devices: ["cudy,wr3000-v1","R31"]` |
| 2 (mainline) | `openwrt-25.12.5-mediatek-filogic-cudy_wr3000-v1-squashfs-sysupgrade.bin` | 9699606 | `be876cf5335ab757874cd19f806b01f2271d8c20a1a0e68f1680019346c4408a` | OpenWrt **25.12.5**, same target/board token |

Provenance, stage 1 (the vendored blob is **not** re-hosted here):

* page: <https://www.cudy.com/blogs/faq/openwrt-software-download>
* Drive folder `1BKVarlwlNxf7uJUtRhuMGUqeCa5KpMnj`, Drive file id
  `1AWpLk9bElfBverPHdpXOJrP30WcFf9OC`
* download name **`WR3000+V1  without recovery TFTP.zip`** (note the double space);
  it contains a nested zip with exactly ONE file — the `.bin` above.
  The Cudy folder ALSO offers a **"recovery TFTP"** variant: that one is for the
  serial/U-Boot route and is **not** what this lane uploads.

Provenance, stage 2:

* <https://downloads.openwrt.org/releases/25.12.5/targets/mediatek/filogic/>
* the pinned sha256 was checked **against the upstream `sha256sums`** on 2026-09-27
  (`be876cf5…`, exact match), on the host AND on the device.
* the lane **derives** the filename from the board token
  (`cf.mainline_image_filename()`), so it can never quietly stage — or claim to
  have staged — the MT3000 image `lib/fresh_flash.py` pins for the *other* bench
  box (`gl-mt3000`); `verify_mainline_image()` refuses that filename by name.

Both images also accept a **`<image>.sha256` sidecar** when one is present. A
sidecar that disagrees with the pinned hash is a refusal, never a silent pick.

## The flash-capacity wall (MEASURED 2026-09-27) — the headline for a 16 MB box

The TollGate payload **cannot be installed on flash** on this board:

| fact | bytes |
|---|---|
| mtd5 `firmware` (16 MB NOR) | 15,794,176 (0xf10000) |
| mtd6 kernel | ~4.2 MB |
| mtd7 rootfs | ~10.8 MB |
| mtd8 `rootfs_data` (jffs2 overlay) | ~5.9 MB, of which **only ~4.6 MB free** |
| tollgate-wrt payload, **uncompressed** | **21 MB** |
| — `usr/bin/tollgate-wrt` | 12,361,280 |
| — `usr/bin/tollgate` | 7,373,632 |
| — `etc/` 924 K, `www/` 216 K, `lib/` ~1.2 MB | ~2.3 MB |
| the `.apk`/`.ipk` package, **compressed** | 8.5 MB |

The compressed package cannot be unpacked into 4.6 MB free: `apk` dies mid-extract with

```
failed to extract usr/bin/tollgate-wrt: No space left on device
```

A custom ImageBuilder image fails the same arithmetic (base squashfs ~6.5 MB + kernel
3.2 MB + ~8.5 MB compressed payload > 15.1 MB firmware area).

**The lane refuses this before it starts**, with the arithmetic rather than `apk`'s ENOSPC:

```sh
scripts/cudy-flash.py capacity            # read-only; probes the box if reachable
scripts/cudy-flash.py capacity --probe    # df -k /overlay + df -k /tmp over ssh
```

`cf.check_install_capacity` names the payload size and the free space, and
`install-tollgate` (and `capacity`) refuse with exit bit **128** before touching the box.

## The volatile (tmpfs) install — IMPLEMENTED, NOT YET HARDWARE-VERIFIED

The operator's chosen answer to the wall is a **volatile** install: ~117 MB of tmpfs is
free, so the **two big binaries live in `/tmp`** (RAM) and are **symlinked from `/usr/bin`**,
while the **small parts** (`etc/`, `www/`, `lib/` ~1.2 MB) go on flash.

```sh
# prints the plan and uploads nothing unless --package + --yes-i-mean-it are given
TOLLGATE_ENABLE_SYSUPGRADE_FLASHING=true \
  scripts/cudy-flash.py install-tollgate --volatile --package <tollgate-wrt-*.apk>
```

The plan (`cf.volatile_install_plan`) is **idempotent** (`mkdir -p`, `ln -sf`, `cp -a`):

```sh
mkdir -p /tmp/tollgate-wrt-volatile
tar -xzf /tmp/<pkg>.apk -C /tmp/tollgate-wrt-volatile
ln -sf /tmp/tollgate-wrt-volatile/usr/bin/tollgate-wrt /usr/bin/tollgate-wrt
ln -sf /tmp/tollgate-wrt-volatile/usr/bin/tollgate     /usr/bin/tollgate
cp -a /tmp/tollgate-wrt-volatile/etc/. /etc/ 2>/dev/null || true
cp -a /tmp/tollgate-wrt-volatile/www/. /www/ 2>/dev/null || true
cp -a /tmp/tollgate-wrt-volatile/lib/. /lib/ 2>/dev/null || true
```

**It will NOT pretend to be persistent.** The plan's `persistent` is always `False`, the
description prints `persistent=False`, and `cf.volatile_persistence_violations(True)`
refuses any claim that it survives a reboot:

> VOLATILE (tmpfs) install: … **THE INSTALL IS LOST ON REBOOT** — `/tmp` is tmpfs, it does
> not survive a power cycle or a `sysupgrade`.

This is a *bench/workbench* install mode (bring the module up to test it), not a
deployment mode.

## Two kit bugs found by the hardware run, and their fixes

### (1) Fresh-box keepalive seed — **fixed, VERIFIED ON HARDWARE**

`scripts/offline/templates/99z-mgmt-keepalive` used to call
`uci add_list nodogsplash.@nodogsplash[0].trustedmac=…` straight away. On a **freshly
flashed** router there is no `/etc/config/nodogsplash` yet, so every `uci` call answered
`uci: Entry not found`, the seed still exited 0, the box trusted **nothing**, and the
installer (correctly) refused with exit **5**. The verified fix creates the file **and**
the anonymous section first:

```sh
[ -f "$CONF_DIR/nodogsplash" ] || : > "$CONF_DIR/nodogsplash"
if ! uci -q get nodogsplash.@nodogsplash[0] >/dev/null 2>&1; then
    uci add nodogsplash nodogsplash
    uci set nodogsplash.@nodogsplash[0].gatewayinterface='br-lan'
fi
```

Regression-tested in the no-hardware harness: the `uci` double now models **real section
semantics** (a write into a missing section fails), and **T21**
(`tests/offline-install/run-tests.sh`) runs the shipped seed on a fresh fixture router
(must go live) with the pre-fix seed text as a control (must refuse with the lockout code).

### (2) WAN-less dependency closure — **already landed upstream (PR #178)**

`scripts/offline/install-router.sh` used to hand `apk` only the **direct** deps
(`nodogsplash jq libmicrohttpd-no-ssl libpthread`), so on a fresh, WAN-less box `apk`
tried to resolve `iptables-mod-conntrack-extra` / `iptables-mod-ipopt` /
`iptables-mod-nat-extra` / `iptables-nft` from **unreachable** repositories and failed
`unable to select packages: … (no such package)`. The verified fix — pass the **whole
staged bundle closure** as file arguments, with `tollgate-wrt` **last** — is already in
this branch via upstream **PR #178** (`84fcb96`), with the fresh-box regression control
**T20** in the same harness. The no-brick ordering is unchanged: dependencies → keepalive
live → nodogsplash → tollgate-wrt last.

## Usage — every subcommand

```sh
# 0. read-only, offline (changes nothing; names every gate that is closed)
scripts/cudy-flash.py check
scripts/cudy-flash.py check --model "WR3000 V1.0"
scripts/cudy-flash.py check --page-html /tmp/captured-firmware-page.html   # classify a capture
# exit bits: 8 = destructive switch off, 1 = image problem, 2 = model refused, 4 = page unclassified

# 0b. flash-capacity preflight (read-only; add --probe to df the box)
scripts/cudy-flash.py capacity
scripts/cudy-flash.py capacity --probe
# exit bit 128 = the payload does not fit the chosen mode

# 1. STAGE 1 — vendor-UI upload of the Cudy-signed transitional image
scripts/cudy-flash.py oem-upload --dump-page --model "WR3000 V1.0"   # log the panel + pinned selectors, upload nothing
TOLLGATE_ENABLE_SYSUPGRADE_FLASHING=true CUDY_PASSWORD=… \
  scripts/cudy-flash.py oem-upload --model "WR3000 V1.0" --yes-i-mean-it

# 2. STAGE 2 — mainline sysupgrade (host NIC re-addressed onto 192.168.1.0/24)
TOLLGATE_ENABLE_SYSUPGRADE_FLASHING=true \
  scripts/cudy-flash.py sysupgrade --yes-i-mean-it --readdress enx00e04c683d2d
#    --dry-run stages + verifies on device and stops before the flash

# 3. STAGE 3 — handover into the kit's existing install path (or volatile)
TOLLGATE_ENABLE_SYSUPGRADE_FLASHING=true TOLLGATE_ROUTER_PASSWORD=… \
  scripts/cudy-flash.py install-tollgate                 # capacity preflight, sets pw, enables Wi-Fi, prints the path
TOLLGATE_ENABLE_SYSUPGRADE_FLASHING=true TOLLGATE_ROUTER_PASSWORD=… \
  scripts/cudy-flash.py install-tollgate --run-install-path
TOLLGATE_ENABLE_SYSUPGRADE_FLASHING=true \
  scripts/cudy-flash.py install-tollgate --volatile --package <tollgate-wrt-*.apk> --yes-i-mean-it

# 4. read-only post-install ladder
scripts/cudy-flash.py verify --api http://192.168.1.1:2121/
```

Environment: `TOLLGATE_ENABLE_SYSUPGRADE_FLASHING` (the destructive switch),
`CUDY_PASSWORD` / `TOLLGATE_LUCI_PASSWORD` (vendor UI password — **env only, no literal in
this repo's new code**), `CUDY_URL`, `CUDY_MODEL`, `CUDY_TRANSITIONAL_IMAGE`,
`CUDY_MAINLINE_IMAGE`, `CUDY_DUMP_DIR`, `CUDY_FW_ACTION` / `CUDY_FW_FILE_FIELD` (override the
pinned endpoint/field), `CUDY_TOLLGATE_PACKAGE` (the `.apk` for `--volatile`),
`TOLLGATE_SSH_PASSWORD` / `TOLLGATE_LUCI_PASSWORD` (ssh), `TOLLGATE_ROUTER_PASSWORD`
(the lab password to SET on the fresh box), `TOLLGATE_CUDY_TAKE_BENCH_LOCK` /
`TOLLGATE_CUDY_LOCK` (lock policy).

## What it REFUSES (each of these is a unit-tested refusal)

* a **WR3000 2.0** box — the OpenWrt TOH page says the 2.0 CPU is *not supported*;
  a v1 transitional image will not boot there. The refusal cannot be overridden.
* an **unidentifiable** label (fail closed; `--assume-wr3000-v1` clears *unknown*
  only, never a 2.0);
* a stage-1 image that is not exactly `openwrt-mediatek-filogic-cudy_wr3000-v1-sysupgrade.bin`
  (right name, exact size, pinned sha256);
* a stage-2 image that is not the board-derived filename / size / sha256 — including
  the MT3000 filename, named explicitly;
* a `<image>.sha256` sidecar that disagrees with the pinned hash;
* **any** destructive step without `TOLLGATE_ENABLE_SYSUPGRADE_FLASHING=true`, and
  without an explicit `--yes-i-mean-it` (stray cron / mistyped re-run protection);
* the stage-2 `sysupgrade -n` when the **wallet probe did not answer** (unknown is
  not "empty"), and when the wallet holds ecash (drain first:
  `tollgate wallet drain cashu --yes`);
* the vendor upload unless the page is **positively classified as the CudyOS vendor
  UI** — a **bootloader/recovery** page refuses (different, riskier lane) and an
  **already-running OpenWrt** page refuses with "stage 1 is already done";
* an upload whose two-step answer is not *upload 200 → Proceed 302* (unknown is
  **never** a pass), and an upload refused by Cudy's signature check;
* the **install** when the payload does not fit the free overlay (exit **128**), with
  the payload-vs-free arithmetic and the ENOSPC consequence named — never `apk`'s error;
* `install-tollgate` without `TOLLGATE_ROUTER_PASSWORD` (a fresh image has an
  EMPTY root password and the lab password must be **set** as part of the handover).

## What it VERIFIES

* image name + exact size + sha256, locally **and again on the device** after
  staging (`sha256sum /tmp/<image> | cut -d' ' -f1`), comparing byte-for-byte with
  the local hash before any `sysupgrade`;
* after stage 1: board token is the Cudy one **and** the build is the SNAPSHOT
  transitional one (a release string there means stage 1 did not land);
* after stage 2: board token + `25.12.5` + arch, **no** SNAPSHOT, and **no**
  TollGate state (`tollgate-wrt` package absent, no
  `/etc/tollgate/{config,identities,install}.json`, no stale setup marker);
* Wi-Fi: `ubus call network.wireless status` must list interfaces (zero means the
  `disabled 1` trap);
* post-install: the module API must answer `kind:10021` **with** `price_per_step`
  tags (missing tags == degraded mode, never evidence).

## Reused machinery (not re-invented)

| concern | owner | how this lane uses it |
|---|---|---|
| destructive switch, `FlashRefused`/`ImageInvalid` | `lib/fresh_flash.py` | imports the constants/exceptions; `flash_enable_gate()` |
| wallet gate (fail closed) | `lib/fresh_flash.py` | `WALLET_BALANCE_COMMAND`, `parse_probed_wallet_state`, `flash_guard`, `flash_preconditions` |
| sysupgrade / board-identity command builders | `lib/fresh_flash.py` | `sysupgrade_command`, `remote_image_path`, `board_identity_command`, `post_flash_readdress_command`, `no_tollgate_state_violations` |
| TollGate artifact + install path | `lib/install_paths.py`, `scripts/install-path-e2e.py`, `scripts/fresh-flash.py` | `install-tollgate` sets up the box then hands over to `make install-path-e2e` / `--run-install-path` |
| bench single-owner lock | `lib/bench_lock.py` (`~/.hermes/state/bench-mt3000.lock`) | **explicitly not taken by default** |

**Lock policy (explicit, not silent):** the Cudy is a *separate physical box* on a
different L2 from the MT3000 bench, so taking the MT3000 flock would serialise two
unrelated boxes. The lane takes **no** lock by default; when the Cudy shares the
bench host's wire with another owner, set `TOLLGATE_CUDY_TAKE_BENCH_LOCK=true` and
it serialises on its **own** lock (`~/.hermes/state/bench-cudy-wr3000.lock`).

## Traps (each one cost someone real time)

1. **v1 vs 2.0.** A new "WR3000 **2.0**" exists and is **not** OpenWrt-supported.
   Read the label (`WR3000 V1.0`, optionally `R31`). The lane refuses a 2.0 *and*
   refuses an unreadable label.
2. **The firmware modal, not the fragment.** A bare GET of
   `/cgi-bin/luci/admin/system/upgrade` is a ~2.6 KB fragment where `window.upload_file`
   is undefined. The **modal** on `/cgi-bin/luci/admin/panel` is what loads the JS; the
   upload is a **two-step** (file POST 200 → `Proceed` POST 302).
3. **Wi-Fi is off after the transitional flash.** Stock OpenWrt ships both AP
   `wifi-iface` sections `option disabled 1`: the radios are up, `iw dev` is
   empty, `"interfaces": []`, and every scan strategy fails. Enable the
   **ifaces**, not just the radios, before claiming any portal result.
4. **The address moves.** `192.168.10.1` (CudyOS) → `192.168.1.1` (fresh OpenWrt).
   Re-address the host NIC (`cf.readdress_command`, default `192.168.1.200/24`).
   On a shared bench the new LAN may **collide** with another box's `192.168.1.1`;
   pin the route to the Cudy's NIC (`ip route replace 192.168.1.1/32 dev <nic> table 100`
   + `ip rule add to 192.168.1.1/32 lookup 100 priority 50`) and remove the rule after.
5. **No `sftp-server`.** `scp`/`sftp` FAIL on these builds. Stage with an ssh
   stdin redirect (`ssh root@box 'cat > /tmp/<image>' < <local image>`) and
   re-verify the sha256 on the device.
6. **Cudy signature protection.** The vendor UI refuses *stock* OpenWrt. That is
   the expected refusal, and the reason stage 1 exists — do not read it as "the
   route is broken".
7. **A fresh image has an EMPTY root password.** `ssh root@192.168.1.1` works with no
   password; the lab password must be **set** (`passwd root`; `chpasswd` does not exist
   on OpenWrt) before the install path runs, which refuses an empty password.
8. **The 16 MB flash wall.** The 21 MB uncompressed payload does not fit the 4.6 MB
   free overlay. Use `--volatile` (tmpfs; **LOST ON REBOOT**), never a flash install.
9. **"recovery TFTP" naming.** The Cudy Drive folder has more than one variant.
   Use `WR3000+V1  without recovery TFTP.zip`; the recovery-TFTP archive belongs
   to the UART/U-Boot route.
10. **CudyOS is not OpenWrt.** Most stock LuCI paths do **not** exist
   (`/admin/status/overview` answers "No page is registered"); the vendor menu is
   `/admin/wizard`, `/admin/setup`, `/admin/panel`, `/admin/tools`. Do not apply
   uci runbooks to CudyOS.
11. **The label's Wi-Fi key is NOT the UI password.** The UI password is a lab
   secret supplied by env only.
12. **Segment hygiene.** The bench wire has had more than one DHCP server on it
   before; the address a host holds there is only stable until the next lease
   event. Pin it, or remove competing servers.

## What is verified vs not

**Verified ON HARDWARE (2026-09-27, Cudy WR3000 v1):**

* **stage 1** — the vendor-UI upload (`panel` modal → `cbid.upgrade.1.firmware` →
  `POST /cgi-bin/luci/admin/system/upgrade` 200 → `Proceed` 302 → reboot applies),
  landing on SNAPSHOT r22906-c9cb6411c1 / board `cudy,wr3000-v1` / empty root password;
* **stage 2** — the ssh-stdin staging, on-device sha256 match, `sysupgrade -T` then
  `sysupgrade -n`, landing on 25.12.5 r33051-f5dae5ece4 / board `cudy,wr3000-v1`;
* the **flash-capacity wall** (the measured layout and the ENOSPC mid-extract);
* the **kit bug (1)** keepalive-seed fix (the fresh box then trusts the MAC).
  *(Kit bug (2), the WAN-less closure, was fixed and landed upstream in PR #178.)*

**Implemented + unit-tested, NOT run on hardware:**

* the **volatile (tmpfs) install** (`install-tollgate --volatile`) — plan, idempotent
  commands, and the non-persistence refusals are unit-tested; no run has installed it
  on a box. Treat it as "bring it up for a bench test", and verify the module answers
  `kind:10021` by hand the first time;
* the **capacity preflight's on-device probes** (`df -k /overlay`, `df -k /tmp` over ssh)
  — the arithmetic is pinned from the run, but the probe wiring is not exercised.

**Still unverified / out of scope:**

* the **login POST shape** end to end (`luci_username=admin` + `password` to
  `/cgi-bin/luci/`) — the standard LuCI form, but not exercised for the *upload flow* by
  the lane itself;
* whether the Wi-Fi `wifi-iface` section names on THIS board are
  `default_radio0`/`default_radio1` (the enable sequence was proven on the MT3000, not
  on the Cudy) — pin them from the box before trusting the exact `uci set` lines;
* the **serial UART / U-Boot TFTP recovery** route (the kit's `uboot-recover.py` does not
  yet carry a `cudy-wr3000` profile);
* the **WR3000 2.0** in any form — no lane exists or should exist for it;
* a **per-version image matrix**: the lane pins one transitional build and one mainline
  release; a new OpenWrt release means re-pinning the hash and size.

## Verifying the lane itself (no router)

```sh
python3 -m pytest tests/unit/test_cudy_flash.py -q
python3 -m py_compile scripts/cudy-flash.py lib/cudy_flash.py
python3 scripts/cudy-flash.py check --model "WR3000 2.0"    # must refuse, naming the gates
python3 scripts/cudy-flash.py capacity                      # must refuse: 21 MB vs 4.6 MB
python3 scripts/cudy-flash.py --help

# the two kit-bug regressions, offline (no router)
bash tests/offline-install/run-tests.sh --only T20   # WAN-less dependency closure
bash tests/offline-install/run-tests.sh --only T21   # fresh-box keepalive seed
```
