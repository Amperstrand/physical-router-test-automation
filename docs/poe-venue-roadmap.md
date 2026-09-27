# PoE Venue Roadmap — physical lab on the GS1900 bench switch

State snapshot: 2026-09-22. Owner: PRTA (test scenarios) + conwrt (bench
rig) + tollgate-lab (shared primitives). Read together with
`conwrt/data/sessions/2026-09-22-SESSION-CLOSEOUT.md` (bench portrait)
and `conwrt/docs/BENCH-SWITCH-PATTERN.md` (rig).

## Bench portrait (as of 2026-09-22)

| Port | Unit | Firmware | Power-cycle policy |
|---|---|---|---|
| lan1 | ERX uplink trunk (VLANs 1002–1008) | 25.12.4 | **PROTECTED** (not a DUT) |
| lan2 | AP3915i UNIT2 review | 25.12.5-era, self-booting | OK |
| lan3 | AP3915i recovered | 24.10.2, TFTP-dependent | **PROTECTED** until #61 |
| lan4 | AP3915i reference | 24.10.2, self-booting | OK |
| lan5 | AP3915i fallback-dependent | — | **PROTECTED — ONE-WAY TRIP** until TFTP lifeline re-armed |
| lan6–8 | empty (lan6 dark/serial-gated #62) | — | n/a |

Enforcement: `configs/labgrid/inventory.local.yaml` `protected_ports` +
per-router `protected: true` → `PoeControllerConfig.protected_ports` →
`PoePowerController._manage()` refuses before any ubus call. lan1/lan3/lan5
are currently protected. **Ask conwrt to not export a power place for
lan5** — the labgrid path bypasses our controller.

## Updating the inventory

The inventory (`configs/labgrid/inventory.local.yaml`, gitignored) is
PRTA's local copy of bench truth. Update procedure — sources in order:

1. Newest `conwrt/data/sessions/*CLOSEOUT*` / `*bench-state*` doc
   (port map, unit lineage, hazards).
2. Live switch, read-only: `ssh root@<switch> 'ubus call poe info'` —
   validates which ports actually Deliver.
3. Address discovery after power events (DHCP leases / ARP), when the
   bench is free.
4. Edit the file; validate:
   `python3 -c "import sys; sys.path.insert(0,'.'); from lib.lab_inventory import load_inventory; i=load_inventory(); print(sorted(i.routers), sorted(i.protected_ports))"`
5. Committed schema/example carries placeholders only — MACs, serials,
   passwords, per-unit IPs never enter git.

Rule: never power-toggle a port to "discover" a device while another
session works; never infer the map from a single source.

## Phase 1 — direct SSH power control (DONE, pending live re-verify)

- `tollgate_lab/hardware/poe.py` — verified-manage controller: action-form
  ubus (`poe manage {"port","action"}` + `set_port_config` fallback),
  state verification by polling, min-off-time 8s, budget projection,
  frozen-daemon detection (`PoeUnresponsiveError`), protected-port
  refusal (`PoeProtectedPortError`). 27 unit tests.
- PRTA entry: `TOLLGATE_POE_SMOKE=1 pytest tests/scenarios/test_poe_power_cycle.py --no-deploy`
  (gate router via `TOLLGATE_POE_GATE`, default `ap-lan2`).
- Recovery orchestration: `scripts/recovery/switch_tftp_recovery.py`
  (dry-run default; `--execute` gated; re-inserts the runtime-only nft
  rule its serve-step needs after switch reboots).

## Phase 2 — labgrid coordinator client (LIVE 2026-09-22)

All OpenWrt DUTs on the GS1900 are enrolled: exporter resources
`ai-legion/ap-lan2..8/NetworkPowerPort` (conwrt_poe backend) and places
`ap-lan2..8` on the shared coordinator (address in inventory). PRTA's
scope is the `ap-lan*` places ONLY — the coordinator hosts other
projects' places (fips atoms, charger HIL, bolty records) that PRTA
never touches.

Locking granularity (owner directive 2026-09-22 — per-resource, not
global):

| Operation | Lock |
|---|---|
| Per-DUT work (power on one port, SSH, tests) | labgrid place acquire + RouterLock — no global lock |
| PRTA's own PoE scenarios vs each other | `prta-poe-bench` flock (venue-local) |
| Switch-wide mutations (VLAN/network changes, reboots, poe-daemon restart, exporter restarts) | global `amperstrand-bench` flock, only when no place is acquired |

Remaining PRTA steps:

1. `pip install labgrid` client (done in tollgate-test-venv 2026-09-22).
2. Client smoke: `labgrid-client -x <coordinator> -p ap-lan2 show` (done —
   place matched, resource avail). First acquire/power-cycle only after
   operator sign-off on ap-lan2 allocation.
3. Tests adopt `--lg-env=configs/labgrid/physical-poe-lab.yaml` with
   RemotePlace; keep the direct PoePowerController path as bring-up
   fallback (it carries protected-port + frozen-daemon guards the
   coordinator path doesn't have yet — porting proposal sits with conwrt).
4. labgrid YAML gotcha (from conwrt): comments must use `##`, never `#`
   (Jinja templates).

## Release testing on the PoE venue (target: next tollgate release)

The AP3915i fleet upgrades the venue vs the shelved NR7101: ARM
Cortex-A7 (fully covered by tmbg CI matrix) AND dual-band WiFi — the
portal/phone tier becomes possible on PoE-controlled hardware, plus
25.12.x = APK packaging path on lan2's UNIT.

Allocation is pending the operator's WiFi-mesh plan (lan4 = gateway
candidate) — coordinate before squatting. Proposed split when agreed:

- `ap-lan2` (25.12.5, self-booting): tollgate release SUT — deploy RC,
  API tier, token formats, cold-boot persistence, power-churn soak.
- `ap-lan4` (24.10.2 reference): operator mesh gateway; PRTA uses only
  with explicit handoff.
- Portal tier (Playwright/phone): whichever unit the operator assigns;
  fallback remains GL-MT3000 alpha.
- Publish gating unchanged: physical venue never publishes; public
  evidence via a parallel SHC cloud run of the same artifact.

## Open items

1. Verify `manage` responsiveness on the rebuilt switch (toggle an EMPTY
   port, never lan3/lan5) — no one has power-managed since the redeploy.
2. conwrt: drop/annotate the `ap-lan5` power place (one-way trip).
3. Addresses: DUTs sit on old statics until `conwrt configure` adopts the
   bench addressing (VLAN 100N, 192.168.10N.50-150) — fill inventory as
   they land.
4. Switch daemon health monitoring: the frozen-detector should run as a
   cheap preflight in every PoE scenario, not just on failure paths.

## Phase 3 — labgrid venue (LIVE 2026-09-24)

The physical lab is now a first-class venue: scenarios run unchanged against
a labgrid place with `TOLLGATE_VENUE=labgrid` (select DUT with
`TOLLGATE_LABGRID_PLACE`, default `ap-lan2`).

- **Design + S2 findings**: `docs/labgrid-venue-design.md` (thin adapter,
  subprocess labgrid-client, guarded place binding, env-export into the
  existing `router` fixture — QEMU/SHC paths untouched).
- **Proven on ap-lan2** (2026-09-24): `tests/scenarios/test_labgrid_venue.py`
  green end-to-end — place acquire (per-DUT lock), SSH via inventory
  key/jump, cold cycle through the place's NetworkPowerPort (conwrt_poe
  verified-manage underneath; driver-host needs `conwrt_poe.py` installed
  inside the venv's labgrid package), uptime-reset + firmware-identity
  proof, clean release. Direct PoE smoke remains the bring-up fallback.
- **Token dialect note**: deployed backend parses proof `C` as hex; the
  Sept-21 portal build wants base64 — `lib/cashu.py` `_encode_c()` defaults
  to hex (backend dialect) with `TOLLGATE_TOKEN_C_BASE64=1` for portal-only
  flows. QEMU API regression green on the hex default (12/12).
- **Hardware prerequisites per DUT** (inventory, gitignored): `place`,
  `keyfile` (ed25519 — 25.x dropbear refuses RSA pubkeys), `jump_host`
  (pytest hosts off the DUT VLAN), `address`.
