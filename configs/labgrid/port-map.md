# Rig Port Map — rev 5, FROZEN 2026-09-25 ~14:40Z (agent provision; single source of truth)

Frozen per conductor DoD. Evidence base: direct SSH probes from
ai-legion-small (wlp4s0 secondaries 102.2/103.2/105.2/108.2), raw cmd=773
HTML rows (NOT the StockWeb parser — see caveats), stock SSH-CLI MAC table,
OpenWrt ubus/FDB, serial console (splice RX), WiFi scan, PoE bounce
correlation. Changes after this freeze go in as rev 6 with a new evidence
pass.

## Stock GS1900-8HP #2 — THE RIG SWITCH (V2.90, web admin 192.168.13.3, sops fleet pw)

| Port | Occupant (evidence) | Status @ freeze |
|---|---|---|
| **p1** | lab-LAN uplink | **KILL-SWITCH — never touch** (no serial; factory-reset-only recovery) |
| **p2** | **ap-lan2 OBSERVER** (dc:b8:08:6c:ea:7f; FDB + SSH @ .102.51 flat) | 5.1 W, healthy; splice SOURCE (its ttyMSM0 → dark unit's console); survived 3 accidental reboots 09-25 (provision p2-misattribution incident — pre-FDB-fix) |
| **p3** | dark unit's 2nd link per FDB (b4:2d:56:25:79:b1 seen here; unit is power-proven on OpenWrt lan2 → dual-cable or stale entry) | 3.4 W class3 |
| **p5** | **router-alpha** (b4:2d:56:24:ad:97; SSH-proven @ .105.51) | **PoE-INPUT FAILURE verdict (2026-09-25 ~15:0xZ)**: cycle executed (provision disable + owner enable — provision's enable leg hit the lying-error-page class), re-poll verified **class0/0 mW across a 60 s offered-power window = THIRD independent no-negotiation result**. Sibling fields sane (Low/802.3at — earlier repair held). Remaining discriminator before declaring DUT hardware: **cable move to empty stock p3/p4 or lab injector**. Golden 24.10.8 on disk; unit dark until discriminator |
| **p7** | NR7101 (78:c5:7d:13:91:9c) | latest pass reads 0 mW (was 5.3 W morning) — flagged, NOT a rig DUT, untouched |
| p4/p6/p8 | — | free |

Web-API caveats (all live-earned 09-25, runbook in PRTA docs/rig-flash.md):
cmd=773 partial pages under exporter+client session contention; StockWeb
row-parser misses page variants (raw `<tr>` parse = truth); set_poe_state
enable-verify too tight for cold PD boot; enables can stop landing after
abnormal disable sequences → human web-UI toggle is the escalation.

## OpenWrt GS1900-8HP #1 (192.168.13.2, 25.12.1 r32768 + poe-fix daemon @bb9c792)

| Port | Occupant (evidence) | Status @ freeze |
|---|---|---|
| **lan1** | uplink + 1002-1008 trunk | **PROTECTED** |
| **lan2** | **DARK UNIT b4:2d:56:25:79:b1** (serial identity + bounce-power correlation) | 2.6-3.6 W; chronic boot-staller: PBL→SBL1→U-Boot (NAND/ESS/eth0 init seen) then SILENT pre-kernel since ≥09-24; splice RX watches it (TX dead — cable suspect); W1 recovery = interactive U-Boot once TX fixed |
| lan3/4/5/6/7 | empty | carrier 0 |
| **lan8** | UNKNOWN: 4.6 W, carrier 1, L2-silent for hours, no FDB | **prime candidate for the missing lan3 DUT** (b4:2d:56:25:47:a2 — RS-unresponsive on flat, no beacons, no FDB anywhere); owner cable-trace pending; **NEVER TOGGLE (mission rule)** |

- Daemon fix (poe-fix lane, 14:22Z): UART-framing resync + LED-map OOB
  write; verified live at 14:37Z — `mcu_comm: age 17 s, stale:false,
  384 ok / 0 bad / 0 rejects`.
- 12:26Z mystery reboot: attribution hang→watchdog-reset, trigger unproven
  → ~/conwrt-bench/docs/POE-WEDGE-ROOT-CAUSE.md.
- Wedge model (corrected 09-25): flock'd daemon restart clears liveness +
  efficacy; manage reflection takes 15-30 s (`poll_interval` 30000);
  rc=0 is never evidence.
- 25.12.5 upgrade: staged + gated (`scripts/rig-flash.sh switch-upgrade
  --confirm` + phone-agent gate); window = T+15 min after alpha recovery;
  in-window: fresh post-fix poe backup, poll_interval→2000, keep-settings
  preserves the daemon fix via overlay.

## Missing unit

**lan3 DUT / router-beta (b4:2d:56:25:47:a2)** — golden 24.10.8 flashed
09-25 morning, then went unlocatable after the owner's rewire: no v4/v6/
NDP/ARP anywhere (103/105/13.x swept + 192.168.1.1), no FDB on either
switch, no WiFi beacons, RS-unresponsive on the flat L2. Recovery = one
owner action: trace the OpenWrt lan8 cable; if it is the DUT, move to stock
p4/p6 → golden keys SSH → normalize (target 105.x doctrine) → proofs.

## labgrid (canonical coordinator 192.168.13.208:20408)

- Stock power: `StockZyxelPoePort` via `ai-legion-small-stock` exporter
  (PAUSED during the p5 incident — restart after the owner toggle) +
  client registration `~/venvs/rig-labgrid/.../tollgate_lab_stock_poe.pth`
  (labgrid 25.0.1 has no entry-point hook). Proven: `power get` "on" +
  place-driven cycle on ap-lan5 (09-25).
- PRTA envs: rig-alpha.yaml (direct .105.51), rig-beta.yaml (holder;
  DUT pending).

## PROTECTED (never toggle, never reboot)

OpenWrt #1: **lan1, lan8**, the switch. Stock #2: **p1**, the switch.
