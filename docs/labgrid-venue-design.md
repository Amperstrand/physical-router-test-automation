# Labgrid physical venue — design (Phase 3 of poe-venue-roadmap)

Status: DESIGN (2026-09-24, post-spike). Implementation follows only after the
QEMU baseline gate (S2) is green. Owner: PRTA lib/ + conwrt bench.

## S2 spike outcome (2026-09-24): RED — reported, gated

The QEMU cashu-portal baseline cannot go green "unmodified" today. Root
cause, proven live on ai-legion:

- The VM's deployed portal (Sept-21 build) only accepts tokens whose proofs
  carry **short (16-hex) keyset ids** and whose mint serves **old-scheme
  keysets**. The local CDK fakewallet (0.16 AND 0.18 — same mnemonic, same
  keyset) declares **new-scheme 32-hex ids** (`01…`). Proof-id form is
  rejected in every variation: full id → CU102 (parse throws), no id →
  CU109 ("keyset rotated"), short id → CU102.
- The Go `mint-token` (gonuts v0.11.2, the Sep-18-validated wallet) also
  refuses this mint: "Derived id '0016…' but got '01df…'" — the Sep-18
  "V3+V4 pass e2e" validation ran against REMOTE testnut mints, not the
  local fakewallet.
- Fixed along the way (real, kept): lib/cashu.py V3 serializer encoded `C`
  as hex — spec requires base64 of the 33-byte compressed point (28/28
  unit tests still pass). Plus 7 QEMU-lab infra repairs (NAT, ufw, bridge
  DNS, on-VM jq, venv path, playwright, mint lifecycle).

Submit-path forensics (2026-09-24, portal bundle): the portal passes proof
`C` to the backend **verbatim** at submit (`incompleteProofs: proofs.map(o
=> ({secret, C: o.C, amount}))` — no re-encoding). The portal decoder
demands base64 `C`; the deployed backend's swap demands hex. With the
Sept-21 portal build, portal payment is therefore impossible against the
current backend REGARDLESS of mint choice — options (a) old-keyset mint
and (c) remote mint cannot fix it. The option space collapses to:

- **(b)** deploy a matched portal+backend pair to the VM (current builds), or
- **(d)** keep portal-payment e2e out of this arc (venue = PoE+SSH+fixture
  layer, S1/S3-green — the choice made for this arc).

Phase 3 on the SUT is NOT dialect-blocked: a fresh deploy ships a matched
pair, and `lib/deploy.py` is verified apk-era-ready (`.apk` artifact
naming, `apk add --allow-untrusted`, era-aware test deps and format
selection). Its remaining gates: ap-lan2 released by the parallel session
(held since 13:33 at diagnosis time) + the staged nft mint rule below.

## Goal

Make the conwrt labgrid bench (GS1900 PoE switch + places `ap-lan2..8` on the
shared coordinator) a first-class PRTA venue, so the SAME scenario suite runs
unchanged against `place=ap-lan2` — additive only: QEMU and SHC venues stay
byte-identical paths.

## What the spike proved (evidence base, 2026-09-24)

- `TOLLGATE_POE_SMOKE=1` PoE smoke is green on ap-lan2 through the direct
  controller: admin off/on, full cold cycle (uptime reset + firmware identity
  + zero port leakage). Boot-race lesson codified: PoE `Delivering` proves the
  port, not userspace — always `_wait_ssh` before snapshotting a DUT that a
  preceding test may have power-interrupted.
- SUT truth (supersedes older notes): ap-lan2 runs **OpenWrt 25.12.5, apk
  era** (`/usr/bin/apk`, no opkg), /overlay 19.7M free, /tmp 246.7M free.
  Adopted: static `192.168.102.<N>.51`, 4 SSH keys + password; cold-cycle
  survival proven. `lib/deploy.py` is ALREADY era-aware
  (`detect_package_manager`, `detect_arch`) and already uses `scp -O`.
- SSH path: SUT dropbear (25.12.x build) **refuses ssh-rsa pubkeys** —
  ed25519 keys only. Mac reachability to the VLAN-1002 subnet is via the
  switch as jump host; `Router(jump_host=...)` supports `-J` natively.
  The Mac's legacy `~/.ssh/id_ed25519` is actually an RSA key (misnamed) —
  the venue uses a dedicated ed25519 key referenced from the gitignored
  inventory.
- The env seam is complete: conftest's `router` fixture builds `Router` from
  `TOLLGATE_SSH_HOST / TOLLGATE_SSH_KEY / TOLLGATE_SSH_JUMP_HOST` (+ client
  IP/MAC). A venue that exports these env vars needs ZERO scenario changes.
- Locking model (owner directive 2026-09-22): per-DUT work = labgrid place
  acquire + RouterLock; suite self-serialization = `prta-poe-bench` flock;
  switch-wide mutations = global `amperstrand-bench` flock (never needed by
  scenarios).

## Architecture: thin venue adapter, subprocess labgrid-client

```
conftest router fixture
  └─ TOLLGATE_VENUE=labgrid branch (new, additive)
       ├─ lib/lab_inventory.py      (extend: place + keyfile fields)
       ├─ lib/labgrid_venue.py      (new: acquire/release/power wrapper)
       └─ exports TOLLGATE_SSH_HOST/_KEY/_JUMP_HOST + CLIENT_IP/MAC
            └─ existing Router(...) path — untouched downstream
```

### Why subprocess `labgrid-client`, not RemotePlace / pytest-labgrid

The scenarios need exactly three labgrid operations: **acquire (lock),
power, release**. The address comes from the inventory (NetworkService export
mirrors it). RemotePlace would pull the driver stack and pytest plugin
coupling for no scenario benefit, and would bypass PRTA's protected-port
guard, which lives on the inventory side. Subprocess client is trivially
mockable in unit tests (inject the runner), needs no coordinator in CI, and
the power path keeps verified-manage semantics anyway — the exporter's
`conwrt_poe` backend post-verifies every manage underneath the place's
NetworkPowerPort.

Dependency: `labgrid` pip package in the PRTA venv (Mac and ai-legion both).
Unit tests mock the subprocess — no hardware, no coordinator.

### `lib/labgrid_venue.py` (new module)

```
class LabgridBenchError(RuntimeError): ...

class LabgridBench:
    def __init__(self, coordinator: str, place: str, runner=subprocess.run)
    def acquire(self)          # labgrid-client -x <coord> -p <place> acquire
    def release(self)          # release; idempotent
    def power(self, state)     # power on/off via place NetworkPowerPort
    def assert_on(self)        # power on + wait_ssh discipline (see below)
```

Rules baked in:

- **Refuse before acquire** if the mapped inventory router is `protected:`
  or has no `place:` mapping (belt under a labgrid path that has no PRTA
  guard of its own).
- **Never power-blind**: `assert_on` pairs power-on with an SSH readiness
  wait (the boot-race lesson). Surface verified-manage errors loudly, never
  retry blind.
- `release()` in a `finally` — an orphaned place lock blocks every other
  session (happened on the bench: ap-lan6 held 14h by a closed session).

### Inventory extension (`inventory.local.yaml`, gitignored; example updated)

```yaml
coordinator:
  address: "<host:port>"        # already exists

routers:
  ap-lan2:
    place: "ap-lan2"            # NEW: labgrid place name ("" = no place)
    keyfile: "~/.ssh/id_ed25519_bench"   # NEW: ed25519 only — SUT refuses RSA
    address: "192.168.102.51"
    jump_host: "root@<switch>"  # NEW: ProxyJump for pytest hosts not on the
                                # DUT VLAN (Mac); empty when on-subnet
    ...existing fields...
```

`lib/lab_inventory.py`: add `place`, `keyfile`, `jump_host` to RouterEntry
(defaults `""`). Loader keeps its no-values-in-errors policy.

### Conftest hook (additive branch only)

```python
if os.environ.get("TOLLGATE_VENUE") == "labgrid":
    # inventory → entry → LabgridBench.acquire() → export env vars →
    # yield Router → finally: bench.release()
```

Selection: `TOLLGATE_LABGRID_PLACE` (default `ap-lan2`). QEMU/SHC never set
`TOLLGATE_VENUE` — their paths are untouched. The existing
`prta-poe-bench` flock + RouterLock fixtures keep working unchanged around
it.

## Deploy + payment prerequisites on the SUT (25.12.5, apk)

- Packages: `detect_package_manager` → apk path; local packages install with
  `apk add --allow-untrusted --force-non-repository /tmp/*.apk` (25.x rule).
  Transfers via existing `scp -O`. Kernel modules must match `uname -r`
  exactly — tollgate userspace needs none.
- **NEVER firstboot** on the SUT (brick-class per conwrt rules; the unit is
  adopted and valuable). Config changes follow uci readback discipline; each
  scenario documents what it changes on the unit and restores it.
- **Mint reachability — CONFIRMED BLOCKED, fix staged (2026-09-24)**: the
  SUT's route to an ai-legion-hosted mint (`192.168.102.1` gateway → switch
  → mgmt LAN) is rejected by the switch's fw4 forward with admin-prohibited
  (SUT sees `Operation not permitted`). Verified read-only from the SUT.
  The staged fix — apply ONLY when no labgrid place is acquired (switch-wide
  mutation class, per the locking table) and prefer folding into
  `/etc/bench-arm.sh` so it re-arms at boot:
  `nft insert rule inet fw4 forward iifname "switch.1002" ip daddr <mint-host> tcp dport 8383 accept`
  (runtime-only as written; survives nothing — that is the safety property).
  NOTE 2026-09-24 13:33: ap-lan2 was acquired by a parallel session
  (ai-legion/ubuntu) while this fix was staged — do not apply while held.

## Sequencing (target: today)

1. `lib/labgrid_venue.py` + inventory fields + conftest branch + **mocked
   unit tests** (venue mapping, refusal on protected/missing place, env var
   export, release-in-finally). No hardware in unit tests — PRTA rule.
2. `test_poe_power_cycle.py` green through the venue on ap-lan2 (direct
   controller path stays as bring-up fallback).
3. `test_captive_portal_cashu_payment` on ap-lan2: deploy via apk path →
   configure mint → portal → pay → access granted → teardown restores
   adoption state.
4. QEMU regression green (gate: finish S2 first — QEMU backend↔mint root
   cause still open).

## Post-rewiring re-verification checklist (bench is being rewired NOW)

- Port map may have MOVED: re-run `labgrid-client places`, `ubus call poe
  info`, and verify per-port MACs against the inventory before any acquire.
- If DUT ports changed: exporter.yaml + `add-match` updates on ai-legion,
  inventory `poe_port`/`vlan` updates, then one cold-cycle proof per moved
  unit.
- Switch boot re-arms bench failsafes automatically (`/etc/bench-arm.sh`)
  — verify `systemctl --user is-active conwrt-exporter` on ai-legion after
  power return.
- ap-lan2 adoption: verify BOTH auth doors (key + password) after power-up
  before touching anything else.
- If ai-legion was power-cycled: re-arm the QEMU lab runtime pieces (NAT
  masquerade rule, bridge dnsmasq, mint) — ~2 min, documented in the S2
  notes.

## Later (not today)

- Serial assertions via ap-lan2's NetworkSerialPort (forwarder on ai-legion
  port 4002; needs bridge restart — conwrt inventory note).
- QEMU VM enrolled as a labgrid place (issue #76) → all three venues become
  place-parametric; one `--lg-place` selects QEMU vs physical.
- Port the protected-port + frozen-daemon guards into the conwrt exporter
  backend so the coordinator path is self-guarding (conwrt-side proposal).
