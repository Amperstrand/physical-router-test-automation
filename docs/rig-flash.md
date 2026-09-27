# Rig Flash Runbook — AP3915i dual-router rig (alpha + beta)

Operational doc for `scripts/rig-flash.sh`: bringing both Extreme WS-AP3915i
(router-alpha, router-beta) to the bench-golden OpenWrt version, plus the
recovery lane if a unit loses flash-boot. Physical-router-test-automation
(PRTA), 2026-09-25.

## Version decision

| Lane | Version | Status |
|---|---|---|
| **Bench golden (this rig)** | **24.10.8** | User directive 2026-09-25 ("latest 24.x"). Image verified on downloads.openwrt.org: `openwrt-24.10.8-ipq40xx-generic-extreme-networks_ws-ap3915i-squashfs-sysupgrade.bin`, sha256 `30bf9601…dacca` (pinned in the script). |
| tollgate-os release pin | 24.10.4 | Upcoming tollgate release pins OpenWrt 24.10.4 across its whole device matrix (`tollgate-os` `publish-os.yml` + TMBG build-inputs). Keep in mind when matching CI builds — the rig intentionally runs one 24.10.x patch ahead per the user directive. |
| Rollback | 24.10.2 | Pre-flash version of both units. Image kept in `~/rig-flash-images/` (sha256 `38ca3856…f848`). |

Filename gotcha: the device directory is `extreme-networks_ws-ap3915i`
(hyphenated vendor), not `extreme_ws-ap3915i`.

### 25.12 forward-compat check (phase 0.5c — no flashing done)

25.12.x sysupgrade images for ipq40xx/ap3915i **exist** (25.12.0 through
25.12.5 verified on downloads.openwrt.org). The AP3915i can therefore be its
own 25.12 canary when that lane opens — org infra already runs 25.12
(the GS1900-8HP #1 lab switch is on 25.12.1, the ap-lan2 reference unit on
25.12.5). The D-Link COVR-X1860 (mt7621, in the official tollgate matrix)
remains an *optional* additional canary; it is **not** required and the COVRs
were not touched.

## As-built (live, 2026-09-25 PM — supersedes the pre-re-cabling plan)

The owner physically moved both APs from OpenWrt switch #1 (GS1900-8HP, root@192.168.13.2)
to the **stock** GS1900-8HP #2 (V2.90, web admin at **192.168.13.3**, sops
`fleet.bench_root_password`):

| Unit | Place | Stock switch port | Mgmt IP | Power control |
|---|---|---|---|---|
| router-alpha | ap-lan5 (b4:2d:56:24:ad:97) | **p5** | 192.168.105.51 | `StockWeb('192.168.13.3', pw).set_poe_state(5, …)` |
| router-beta | ap-lan3 (b4:2d:56:25:47:a2) | **p2** | 192.168.103.51 | `StockWeb('192.168.13.3', pw).set_poe_state(2, …)` |

Both units sit **flat on the 192.168.13.0/24 lab L2** (stock p1 = uplink).
Reach them directly from ai-legion-small — the host already carries secondary
IPs `192.168.103.2/24`, `192.168.105.2/24`, `192.168.108.2/24` on `wlp4s0`.
**No SSH jump via the OpenWrt switch anymore** (its lan3/lan5 are vacated,
PoE "Searching", carrier 0 — correct vacated state, do not "fix").

Pre-flash finding worth remembering: alpha's dropbear authenticated `using
"none"` — **root password was empty** on the flat lab L2. The flash overlay
now sets the fleet root password on first boot (`uci-defaults/99-rig-adopt`).

## Lane A — in-place flash (executed 2026-09-25)

```
scripts/rig-flash.sh in-place alpha [24.10.8]   # alpha FIRST, verify, then:
scripts/rig-flash.sh in-place beta  [24.10.8]
scripts/rig-flash.sh verify alpha|beta          # re-check anytime
```

Steps the script performs:

1. **Pin + verify** image sha256 locally (pinned hashes; refuses on mismatch).
2. **Preflight doors**: `ubus call system board`, `dd if=/dev/mtd0 | strings`
   confirms `bootcmd=run boot_openwrt; run boot_net` (TFTP fallback door),
   `/tmp` and RAM headroom. Door count per canon: alpha = SSH-key + TFTP
   fallback (2); beta = SSH-key + TFTP fallback + serial splice via ap-lan2
   listener `127.0.0.1:4003` (3).
3. **Backup** `/etc/config` + `/etc/dropbear` to `~/rig-flash-images/backup-<unit>-<ts>.tar.gz`.
4. **Overlay** (mandatory — BENCH-CANON "never reflash onto a shared L2"):
   - `etc/config/network` — minimal golden: br-lan (`lan` DSA port), static mgmt IP, no gateway.
   - `etc/config/dhcp` — DHCP server disabled on lan+wan (rogue-DHCP guard).
   - `etc/dropbear/authorized_keys` — union of the unit's current keys + ai-legion `id_ed25519`.
   - `etc/uci-defaults/99-rig-adopt` — hostname (`router-alpha`/`router-beta`),
     root password (fleet, via sops at build time — never committed), marker file.
5. **Transfer** `scp -O` (BusyBox dropbear has no sftp-server) and **re-verify
   sha256 on the device** before flashing.
6. `sysupgrade -n -f /tmp/overlay-<unit>.tar.gz /tmp/<image>` — clean config,
   golden state.
7. **Wait + verify** (up to 420 s): version == target, hostname, key auth,
   `dhcp.lan.ignore == 1`, `authorized_keys` ≥ 2 lines, `/etc/rig-adopted`
   marker.

## Lane B — from-scratch recovery (scripted, execute only when a unit needs it)

`scripts/rig-flash.sh from-scratch <unit> [--confirm]` — recovery-only; it
power-cycles the unit and temporarily exposes a TFTP server on the lab L2.

Path (encodes conwrt `recipes/switch-initiated-flash.md` +
`recipes/extreme-networks/ws-ap3915i/HARDWARE-DISCOVERY.md`):

1. **If the unit boots stock WiNG** (SSH with legacy crypto:
   `ssh -oHostKeyAlgorithms=+ssh-rsa -oKexAlgorithms=+diffie-hellman-group1-sha1 admin@<ip>`,
   web/admin creds in sops): set `bootcmd=run boot_net` via
   `rdwr_boot_cfg write_var bootcmd=run boot_net` (fall back to `flashcp` of a
   patched CFG1 when `rdwr_boot_cfg` is broken — flag-byte 0x05 issue, see
   HARDWARE-DISCOVERY). If the unit already has the fallback env (both rig
   units do), skip this step.
2. **TFTP bait**: the AP's U-Boot env hardcodes `serverip=192.168.1.2` and
   `ipaddr=192.168.1.1` (alpha) / `192.168.1.11` (beta). The script adds
   `192.168.1.2/24` to `wlp4s0` and serves the *initramfs* `.itb` as
   `vmlinux.gz.uImage.3912` via `dnsmasq --enable-tftp` from ai-legion-small
   (bounded lifetime, cleaned up on exit).
3. **PoE cycle** the unit's stock port via `StockWeb.set_poe_state()` — off 8 s
   (BCM59121 renegotiation delay), on. Coordinate with the `stock-poe` agent
   before toggling stock ports (it owns that switch's exporter work), and
   never touch the stock uplink p1 or NR7101 p7.
4. U-Boot TFTP-boots the initramfs → poll SSH at the env `ipaddr`.
5. From the initramfs: same overlay + `sysupgrade -n -f`.
6. Verify as in Lane A.

## Rollback

Keep `~/rig-flash-images/openwrt-24.10.2-…-sysupgrade.bin` (sha-pinned).
24.10.8 → 24.10.2 is a **downgrade**: sysupgrade refuses without `-F`. Manual
rollback (both doors intact):

```
scp -O ~/rig-flash-images/openwrt-24.10.2-*.bin root@<unit-ip>:/tmp/
ssh root@<unit-ip> 'sysupgrade -n -F /tmp/openwrt-24.10.2-*.bin'
# then re-adopt (overlay keys/password are gone with -n):
#   rebuild overlay: see scripts/rig-flash.sh build_overlay, or restore
#   ~/rig-flash-images/backup-<unit>-<ts>.tar.gz etc/config + etc/dropbear
```

## GS1900-8HP #1 (OpenWrt lab switch) upgrade — prepared + gated, NOT executed

`scripts/rig-flash.sh switch-upgrade [25.12.5] --serial-wired --confirm`

**Why**: port-map plan W2 — the 25.12.5 upgrade is the documented fix
candidate for the realtek-poe daemon↔MCU wedge class (2026-09-25 incident).
Best scheduled **after the phone E2E** and **before the lan4 W1 recovery**
(recovery = repeated PoE manages = the wedge trigger).

**Artifact decision** (verified live 2026-09-25): the switch runs 25.12.1
with `realtek-poe-1.3.1-r1` as an **apk package** (userspace, 65 KiB — the
Amperstrand fork build; the fork repo has no prebuilt image releases).
Therefore: flash the **stock** downloads.openwrt.org image
(`openwrt-25.12.5-realtek-rtl838x-zyxel_gs1900-8hp-a1-squashfs-sysupgrade.bin`,
sha-pinned in the script) with **keep-settings** (`sysupgrade`, NO `-n`) —
`/etc` AND the overlay survive, which preserves the fork package. Caveats
gated in-script: kernel-pinned kmods would not survive a kernel bump
(preflight records `apk info --depends realtek-poe`); post-verify asserts
`apk info realtek-poe` still present + a two-digest `poe info` liveness
check (frozen digest = wedge signature).

**Gates** (the script refuses without all of them):
1. `--confirm` — after the printed checklist: labgrid places released,
   `/tmp/amperstrand-bench` flock free, herdr announce to main + tollgate +
   stock-poe, bench power stable (the September "self-reboots" were bench
   power cuts), serial recovery kit located.
2. Live check: herdr agent `phone` must NOT be `working` — a switch reboot
   cold-cycles ap-lan2 and kills the E2E's serial-monitoring feed.

**Serial recovery (door #2) is ON-DEMAND** per owner 2026-09-25 — no
pre-wiring. If the upgrade strands the box: wire the internal UART header
(left side PCB, labeled `VCC/TX/RX/GND`, 3.3 V TTL, 115200 8N1; see
conwrt `recipes/zyxel/gs1900-8hp/notes.md` for the full serial+TFTP recovery
procedure) and roll back or re-flash from the console.

**Config preservation mechanics**: fresh pre-upgrade snapshot
(`etc-backup.tar.gz` + `uci show network` / `ip -4 addr` / `bridge vlan` /
`apk info`) lands in `~/rig-flash-images/switch-pre-<ts>/`; an offline
baseline from 2026-09-25 is in `~/rig-flash-images/switch-baseline-*/`.
Post-verify diffs SVIs (192.168.10N.1) and version. Runtime-only state is
wiped by ANY reboot (proven: after today's daemon restart the /32 pin +
1003 TFTP bait were already gone) → POST-STEPS: re-run
`scripts/gs1900-bench-arm.sh` (owner/Mac), verify ap-lan2 at .102.51 +
serial bridge, one manage/verify PoE cycle on the EMPTY lan7 test port.

**Rollback**: stock 25.12.1 image kept and sha-pinned in `~/rig-flash-images/`
(+ 25.12.5 initramfs-kernel.bin for a serial/TFTP boot). Downgrade via
serial console `sysupgrade -F`. Note a stock-image rollback keeps the fork
package via the same overlay-survival mechanism.

**Images staged** (~/rig-flash-images/, sha256-verified):
- 25.12.5 gs1900-8hp-a1 sysupgrade (6 554 382 B) + initramfs (5 283 506 B)
- 25.12.1 gs1900-8hp-a1 sysupgrade (6 816 526 B) — rollback

## Stock-switch PoE recovery runbook (earned 2026-09-25, p5 incident)

Artifacts: `~/rig-flash-images/switch-poe-backup-20260925T161500Z/`
(`poe.conf` + `poe-and-network.tar.gz` — PRE daemon-fix snapshot = rollback
artifact; a post-fix backup is re-taken at the upgrade-window start).

Failure classes observed on the stock GS1900 #2 web API that day:

1. **Read path lies two ways**: `cmd=773` serves partial pages under
   exporter+client session contention (one web session per user; exporter
   polls ~40 s), AND `StockWeb.poe_status()`'s row parser misses page
   variants → returns `None` for ports that the RAW page actually shows.
   **Truth = raw `<tr>` row parse of the cmd=773 HTML**, never the helper
   alone.
2. `set_poe_state` enable-verify (`mw>0` within 30 s) is too tight for a
   cold PD boot — it raises *after* the write landed. Treat its raise
   payload as data: `state` field is still truth.
3. After an abnormal disable sequence, enables can stop landing while
   disables always do (PSE channel / form-context wedge).

Recovery procedure (proven):

```
1. sudo systemctl stop labgrid-exporter-stock      # free the web session
2. raw-read cmd=773 rows (regex over <tr>...</tr>) # establish truth
3. post_cmd(774, {"port": "<N>", "sysSubmit": "Edit"})  # load edit form
4. set_poe_state(<N>, True); raw-verify state==Enable; mw follows in ~60 s
5. if enable still fails: HUMAN toggle in the web UI (full browser POST)
   — scripts stop there (never-reboot switch, no serial, factory-reset-only)
6. sudo systemctl start labgrid-exporter-stock
```

OpenWrt #1 `/etc/config/poe` restore (if ever needed): `scp -O` the backup
to `/etc/config/poe`, then `flock /tmp/amperstrand-bench -c
'/etc/init.d/poe restart'`, then verify `mcu_comm.stale == false` (new in
the poe-fix daemon build @bb9c792) + one lan7 empty-port manage control
with a ≥45 s poll budget (`poll_interval` 30000 ≈ 30 s reflection; drop to
2000 during the upgrade window).

## Coordination rules (this rig is shared)

- Announce to agents `main` and `tollgate` (herdr) before any switch config
  change; announce to `stock-poe` before stock-switch power toggles.
- OpenWrt switch #1 (root@192.168.13.2): lan1/lan8 are PROTECTED; per-DUT
  VLANs 1002-1008 stay as-is for lan2 (reference unit).
- Stock switch #2: **never reboot**; `cmd=775` response bodies lie — only
  `cmd=773` re-polls are truth (StockWeb already re-poll verifies).
- No commits from this work.
