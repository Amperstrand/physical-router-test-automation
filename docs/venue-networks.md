# Venue networks — DUT routing, E2E topology, and evidence collection

> How we define, bring up, and observe the networks our tests run in —
> one pattern for virtual, cloud, and **physical** DUTs. This is the
> architecture the extensive test builds on; `scripts/extensive-test.sh`
> is its first consumer.

## The core insight: never route DUT bays to the house LAN

DUT bays are isolated VLANs **by design** (rogue-DHCP discipline: a
fresh/default router runs DHCP + RA and must never compete with the
network's DHCP authorities). Trying to route `192.168.104/105.x` into
the house LAN fights that discipline and loses.

Instead: **attach each DUT bay to a bench-host "test internet"**. The
bench host (ai-legion) already runs exactly this topology for the
virtual lab — a bridge with NAT, a controlled mint, a Debian client VM,
and taps. Extend it so a *physical* DUT's LAN port bridges into its own
per-DUT test network on the host:

```
                    ┌─ bench host (ai-legion) ─────────────────────────┐
                    │                                                   │
 house/ISP ──NAT──► │  tg-dut-<name>-br   10.99.1x.0/24   (isolated)   │
                    │   ├─ tap ◄── DUT LAN port (bench bay VLAN)        │
                    │   ├─ Debian client VM (the paying client)         │
                    │   ├─ fakewallet mint / m5 hardware mint (opt)     │
                    │   └─ host tap = syslog + tcpdump + evidence       │
                    └───────────────────────────────────────────────────┘
   DUT WAN ──► (a) same NAT, or (b) house WiFi STA, or (c) second DUT
```

The NAT host is the firewall boundary, so a rogue DHCP inside a DUT bay
is contained by construction. Deterministic mint control (start/stop
the fakewallet, unplug nothing) makes **degraded-mode tests physical**.
The Debian client gives real E2E (portal → payment → internet) with
payment attribution to its MAC — the proven cloud-lab pattern.

## The four planes (what every DUT seat defines)

| Plane | Carries | Mechanism today |
|---|---|---|
| **Mgmt** | SSH/console/power, exclusivity | labgrid place (ap-lan ports: `NetworkPowerPort` + `NetworkSerialPort` + `NetworkService`), `scripts/vm-keyinject.py` pattern for fresh devices |
| **Data (client)** | the real user | Debian client VM on the DUT's bridge (or the phone via `android-test` for radio-real flows) |
| **Uplink** | DUT WAN "internet" | bench NAT (deterministic) / house WiFi (realistic) / second DUT (P9 chains) |
| **Observability** | evidence | flight recorder (below) + Nostr notices the product publishes itself |

## Defining a network: venue.yaml per seat

Declarative, diffable, versioned — consumed by the runner (and by
`TopologyManager`, which already implements bridges/VMs):

```yaml
# config/venues/nr7101.yaml
name: nr7101
dut:
  mgmt: {labgrid_place: nr7101-router, ssh: 192.168.12.124}
  arch: mipsel_24kc
client:
  type: debian-vm            # debian-vm | phone | wifi-radio
  bridge: tg-dut-nr7101-br
  subnet: 10.99.31.0/24
uplink:
  type: bench-nat            # bench-nat | house-wifi | dut-chain
mint:
  type: fakewallet           # fakewallet | hardware-stick | remote
  url: http://10.99.31.2:8383
taps: [router-logread, nds-console, backend, client-http, nostr]
```

`extensive-test.sh --venue nr7101` then: acquires the place → ensures
the bridge/NAT/client/mint per spec → deploys the ipk (with the
**NDS health gate** — a dead portal can never install "successfully",
tbmg#109/#110) → runs the tiers → collects evidence → tears the bridge
down. Every state change ends with the registry discipline (conwrt-lab
commit for bench-port/VLAN changes).

## Evidence collection: the flight recorder

One module, started at run begin, stopped at end — every tap lands in
`results/<run>/flight/` with a manifest:

| Tap | Mode | Mechanism |
|---|---|---|
| Router syslog | realtime | `ssh dut 'logread -f'` follower → tee (mgmt plane; survives nothing if the DUT dies — that's what the host-side taps are for) |
| Backend stdout | realtime | same follower on the service log |
| NDS console | realtime | labgrid `SerialPort` → tee (serial sees boot/panic that syslog never will) |
| Host-side packet view | realtime | `tcpdump -i tg-dut-<n>-br` on the bench host — sees DHCP rogue behavior, DNS, the *client's* experience |
| Client browser trace | realtime | Playwright trace/video (proven container pattern) |
| Post-hoc | after | `logread` dump + `/etc/tollgate/` config + `ndsctl status` + junit — the existing `runlogs.py`/artifacts flow |
| Product's own evidence | live | Nostr notices (kind 30078 etc.) — subscribe during the run |

Realtime taps are crash-tolerant by construction (host-side), so even a
DUT that wedges mid-test leaves a complete story.

## Rollout order (each step is independently useful)

1. **Flight recorder** on the existing virtual venue (pure software,
   no bench changes) — every extensive run gets full evidence.
2. **NR7101 as the first physical seat** (mgmt plane only — it's
   reachable; needs its password + place-match fix). E2E client =
   phone or a VM once its bench port is bridged.
3. **326D** — same, plus the WiFi-client plane (phone/laptop) and the
   hardware-mint payment test (issue #12 closes here).
4. **One bay VLAN bridged to a bench test network** (ALPHA or BRAVO) —
   the conwrt-bench + registry change that unlocks the full four-plane
   pattern for bench DUTs, including sysupgrade tests.
5. **Two-DUT chains** (P9) — a second bridge between DUTs (the
   cloud-lab `tg-upstream-br` pattern, physicalized).
