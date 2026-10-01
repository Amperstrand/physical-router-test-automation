# Bench environments — which routers are ours, where they live, what runs where

Written 2026-10-01 after a live session confused three different MT3000
populations and nearly built infrastructure for the wrong one. If a doc,
report, or session mentions a router, FIRST place it in one of the three
environments below. When in doubt: the SSID suffix is the discriminator on
TollGate APs (`TollGate-XXXX`), and `scripts/bench/device-identity.sh` is
the fail-closed pin for anything destructive.

## Environment 1 — labgrid PoE bench (the farm)

**Where:** ai-legion-small (physical DUT host per the host-role law); the
labgrid coordinator is the canonical one on ai-legion.

**Devices:** 2 PoE routers, 2× x1860, a fleet of WS3915i — all powered and
**power-controlled via PoE** (remote power cycling is the farm's defining
capability).

**What runs here:** farm-scheduled device testing — anything that needs
power control, unattended re-runs, or shared allocation. This is the only
environment where devices are always-on shared resources.

**Access:** labgrid places via the coordinator. Nothing here depends on a
laptop or a human bridge.

## Environment 2 — laptop bench (mt3000-bench, the live/dev rig)

**Where:** the field laptop's desk. The laptop is dual-homed: ethernet
`192.168.1.2` on the router LAN + lab wifi.

**Devices:** the **GL-MT3000 "TollGate-326D" @ 192.168.1.1** (OpenWrt
24.10, aarch64_cortex-a53 — MediaTek MT7981, same model family as Env 3's
units but a DIFFERENT device), plus the Cudy WR3000 hazard documented in
[bench-device-identity.md](bench-device-identity.md) (two boxes answering
on `192.168.1.1` — identity pinning is mandatory before destructive steps).

**What runs here:** the commissioned live API/SSH tier (PRTA with
`TOLLGATE_SSH_HOST=192.168.1.1`), portal/captive probes, and the on-device
fakewallet-mint work. AI/SSH only unless a commission says otherwise.

**Access:** ONLY through the laptop — ai-legion has no route to
192.168.1.x (verified repeatedly). Today that means either running on the
laptop (see `/tmp/laptophandoff` convention on ai-legion) or a reverse
tunnel the laptop opens. **Enrollment in overlays (NetBird etc.) is NOT
sanctioned for this environment** — owner decision 2026-10-01; if remote
access is ever wanted, propose it, don't build it.

## Environment 3 — external NetBird MT3000s (NOT OURS)

**What:** two GL.iNet MT3000 (arm64, OpenWrt 24.10.4) on a NetBird overlay
(`100.90.41.166` / `100.90.216.248`, AP SSIDs `TollGate-1690` and
`TollGate-D1C6`, aliases router-alpha/beta).

**Status: OUT OF SCOPE.** Do not test on them, do not enroll anything of
ours with them, do not cite them as reachable infrastructure. They appear
in older WiFi/upstream reports (`upstream-wifi-test-report.md`,
`router-b-incident-2026-04-30.md`, `TEST_SETUP.md`) — when a session finds
"MT3000 + arm64 + NetBird" in a doc, THAT is this environment, never
Environment 2's router.

## The confusion this doc exists to kill

| If a session sees... | It is... | Verdict |
|---|---|---|
| `TollGate-326D` / `192.168.1.1` | Env 2 (laptop bench) | Ours; test via laptop bridge |
| `TollGate-1690` / `TollGate-D1C6` / `100.90.x` | Env 3 (external NetBird) | Out of scope |
| WS3915i / x1860 / PoE control | Env 1 (labgrid farm) | Ours; use the coordinator |
| "MT3000 (arm64)" with no other identity | AMBIGUOUS — must be pinned before use | Identity check mandatory |
