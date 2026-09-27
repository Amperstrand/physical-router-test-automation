# Adopting the GS1900-8HP rig into the labgrid coordinator (sketch)

Status: design sketch only (Phase D). The rig is proven GREEN as a LOCAL env
(`configs/labgrid/rig-alpha.yaml` + `tests/test_rig_smoke.py`, 2026-09-25).
This is how the existing `ap-lan2..ap-lan8` places on the shared coordinator
(192.168.13.221:20408) would wrap the same capability.

## Current state

- Coordinator `192.168.13.221:20408` runs on ai-legion-small; places
  `ap-lan2`…`ap-lan8` exist (created 2026-09-22) with matches
  `*/ap-lanN/*` (ap-lan2 additionally pins `NetworkPowerPort` +
  `NetworkService`), but no exporter currently provides matching resources.
- Power control works via tollgate-lab's `ZyxelPoePort` resource +
  `ZyxelPoEDriver` (SSH to root@192.168.13.2 → `ubus call poe
  set_port_config` / `poe info`), deliberately NOT the stock `UbusPowerDriver`
  (no unauthenticated ubus HTTP ACLs on the switch).
- DUT addressing: one VLAN per port, DUTs at `192.168.10N.51`, reachable only
  through the switch — hence the `tools: ssh:` ProxyJump wrapper
  (`configs/labgrid/ssh-via-switch.sh`).

## Sketch

1. **Exporter** (ai-legion-small, run from the rig venv so custom classes are
   importable):
   ```yaml
   # exporter config: groups named after the existing places
   coordinator: "192.168.13.221:20408"
   hostname: ai-legion-small-rig
   resources:
     ap-lan2:
       ZyxelPoePort:
         host: "192.168.13.2"
         port: "lan2"
       NetworkService:
         address: "192.168.102.51"
         username: "root"
     ap-lan5:
       ZyxelPoePort:
         host: "192.168.13.2"
         port: "lan5"
       NetworkService:
         address: "192.168.105.51"
         username: "root"
     # … lan3/lan4/lan6 once credentials/identity are recovered (see
     # configs/labgrid/port-map.md open items); lan7 empty; lan8 protected —
     # never export a ZyxelPoePort for lan1/lan8.
   ```
2. **Client env** (`environment-rig.yaml`, coordinator flavor): targets use
   `RemotePlace: {name: ap-lanN}` + `ZyxelPoEDriver` + `SSHDriver`, and must
   keep `tools: ssh: <wrapper>` — with RemotePlace, SSH still originates from
   the machine running pytest, so the ProxyJump wrapper travels with the env.
   The client venv needs `tollgate_lab.drivers.zyxel_poe` imported (conftest
   one-liner) so the YAML class names resolve.
3. **Places**: existing `*/ap-lanN/*` matches already bind to these groups.
   ap-lan2's extra `*/ap-lan2/NetworkPowerPort` match was written for the
   stock-ubus plan and will simply stay unmatched; delete it when adopting.

## Protobuf constraints (coordinator wire)

Learned from bolty-rs/fips-lab exporter work (`labgrid-bench-sharing.md`,
`tools/hil/labgrid-exporter.yaml` comments) and labgrid source:

- Exported resources cross the coordinator as **flat protobuf**: string /
  int / bool scalars and flat string maps only. Nested dicts, lists, or
  arbitrary Python types in resource attrs silently break or raise
  (MapValue is flat). `ZyxelPoePort` (host/port/username — all scalars) is
  wire-safe by construction.
- Resource classes not in the exporter's builtin `exports[]` map ride the
  **ResourceEntry fallback**: plain passthrough of the attrs, NO
  exporter-side behavior. That is exactly what we want here — the PoE SSH
  work happens client-side in the driver; nothing needs to run on the
  exporter host. (Contrast: USBSerialPort→SerialPortExport spawns ser2net;
  writing `NetworkSerialPort` directly in an exporter yaml rides the same
  fallback and gets no ser2net — the documented fips-lab gotcha.)
- Drivers never cross the wire: only resources do. The client env must list
  `ZyxelPoEDriver`/`SSHDriver` itself; the exporter config lists resources
  only.
- `NetworkService` exports natively (builtin), including through proxies when
  exported with an ifname; for this rig it stays client-originated, so no
  proxy plumbing is needed.

## Open before adoption

- lan2/lan3 credentials, lan4/lan6 identity (help request queued in
  /tmp/help.md, 2026-09-25).
- Decide exporter supervision (systemd unit alongside the existing
  bolty-rs/microfips exporters) and whether the rig exporter config lives in
  tollgate-lab `docs/` or fips-lab `exporters/`.
