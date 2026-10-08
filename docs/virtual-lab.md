# Virtual Lab — local QEMU TollGate test lab

Local KVM/QEMU test lab: an OpenWrt x86_64 VM (router under test) plus a
Debian 12 client VM, on a dedicated bridge. Runs the pytest API suite, the
client payment flow, and reseller scenarios without physical hardware.
Repeat start cycles take ~2.5 min; the serial console is fallback-only and
is never touched on an already-provisioned overlay. Operational details and
pitfalls: `LOCAL-VM-TESTING.md`.

## Current topology

```text
Lab host (KVM required; 10.99.99.2 on the lab bridge)
├── tg-poc-br (bridge, 10.99.99.2/24)
│   ├── tg-poc-tap  → OpenWrt VM eth0 (br-lan, 10.99.99.1)
│   └── tg-poc-tap2 → Debian client VM ens3 (10.99.99.100)
├── OpenWrt VM (KVM, x86_64, 512MB)
│   ├── Disk: overlays/tollgate-poc.qcow2 → images/openwrt-base.qcow2
│   ├── Serial console: run/serial.sock (fallback provisioning only)
│   ├── QEMU monitor: run/monitor.sock (ACPI powerdown on stop-poc)
│   └── SSH: root@10.99.99.1 (password: tollgate / lab credentials file)
└── Debian client VM (KVM, 1GB, debian-12-generic)
    ├── Disk: overlays/debian-client.qcow2 → images/debian-12-generic-amd64.qcow2
    │   + NoCloud seed attached (images/seed.iso)
    ├── Serial console: run/serial-client.sock (fallback provisioning only)
    ├── QEMU monitor: run/monitor-client.sock
    ├── SMBIOS ds=nocloud → skips cloud-init datasource probing
    ├── SSH: root@10.99.99.100 (password: tollgate / lab key)
    └── static 10.99.99.100/24 via 10.99.99.1, DNS → host (10.99.99.2)
```

The host also runs the local CDK V2 FakeWallet mint (10.99.99.2:8383) during
`run-local-tests.sh` and NATs the 10.99.99.0/24 subnet for internet access.

## Quick start

```bash
# 1. Download OpenWrt base + Debian generic base, build the NoCloud seed
python3 scripts/virtual-lab.py prepare-image --host localhost
python3 scripts/virtual-lab.py prepare-debian --host localhost

# 2. Start the lab (OpenWrt + Debian; cloud-init provisions the client first boot)
python3 scripts/virtual-lab.py start-poc --host localhost

# 3. Run the test suite (starts the mint, configures TollGate, runs pytest)
./scripts/run-local-tests.sh tests/api/test_payment_regression.py

# 4. Stop (ACPI powerdown — clean disks keep the next boot fast)
python3 scripts/virtual-lab.py stop-poc --host localhost
```

Use `--host <hostname>` instead of `localhost` when the lab runs on a remote
Ubuntu machine.

## DUT state across stop/start (and `--fresh`)

A `stop-poc`/`start-poc` cycle **reuses the DUT overlay** (`overlays/
tollgate-poc.qcow2`) by design: installed packages, config and internal
qcow2 snapshots survive the restart — which also means a corrupted DUT
comes back identical (#26: opkg orphan-removal had zeroed nodogsplash/
curl/socat/jq and the "fresh" boot still reported the package installed).
`start-poc` announces the reuse on every boot. For a pristine DUT:

```bash
python3 scripts/virtual-lab.py start-poc --host localhost --fresh
```

`--fresh` purges the overlay before boot (the manual `rm -f
overlays/*.qcow2` between stop and start is no longer needed), refuses to
run against a live VM (`stop-poc` first), and prints how many internal
snapshots it discards — they live inside the overlay file. The Debian
client overlay is untouched (re-provisioning it costs a full cloud-init
run). `doctor` now also checks a reachable DUT for zero-byte binaries in
`/usr/bin` — the early corruption detector from #26 — and fails with the
`--fresh` remediation; an unreachable DUT is a skip, not a failure.
`FRESH_DUT=1 ./scripts/extensive-test.sh` passes the flag through on
bring-up.

## Client provisioning: seed first, serial as fallback

The Debian client base is `debian-12-generic` — the image WITH cloud-init.
`prepare-debian` builds a NoCloud seed (`images/seed.iso`: user-data,
meta-data, network-config v2) and `start-poc` attaches it. On the first boot
of a fresh overlay cloud-init installs openssh-server, sets root access,
configures the static IP, and enables a `netplan-generate-boot.service`
oneshot. After that, every reboot is: boot (~16s) → sshd → done. No serial
console involved.

Serial console provisioning remains the fallback if the SSH probe (start-poc
Step 4b, up to 300s) fails. It now persists the network config as netplan
too, so even a fallback-provisioned client survives reboots.

### Pitfalls this design avoids (each was a real failure)

- `debian-12-nocloud` images ship **without cloud-init** ("no cloud
  integration" is not the NoCloud datasource) — a seed attached to a nocloud
  image is dead weight, and serial becomes the only channel.
- The client's static IP must be **persisted as netplan**, overwriting the
  image's match-all `90-default.yaml`: a higher-numbered sibling file loses
  to the match-all (netplan emits equal prefixes; networkd takes the
  lexicographically-first match).
- Debian's netplan.io ships **no boot-time `netplan generate`**:
  `/run/systemd/network` is tmpfs, so ens3 comes up unmanaged and
  `systemd-networkd-wait-online` burns 120s before sshd. The seed installs an
  **unconfined oneshot** (`netplan-generate-boot.service`) that regenerates
  before networkd — an `ExecStartPre` drop-in inside networkd's own unit
  crash-loops it (the unit is sandboxed: no `CAP_DAC_OVERRIDE`).
- DNS during first-boot provisioning points at the **host** (10.99.99.2), not
  the freshly provisioned OpenWrt — its dnsmasq is not dependable during
  provisioning windows.
- `ds=nocloud` (SMBIOS) skips cloud-init's multi-datasource probing, which
  otherwise costs ~2 minutes per boot.

### Ephemeral client mode

```bash
python3 scripts/virtual-lab.py start-poc --host localhost --ephemeral-client
```

Runs the client with QEMU `-snapshot`: writes are discarded on stop-poc, so
every cycle starts from the same pristine provisioned state (no client-state
drift between test runs). Requires an already-provisioned overlay — in this
mode provisioning cannot persist, so first-time provisioning must happen in
the default (persistent) mode. Note `provision_debian` extras
(playwright/chromium) also will not persist while ephemeral.

## Mint health

`run-local-tests.sh` starts the local CDK V2 FakeWallet mint and **probes the
real quote→PAID path** before every suite: a wedged cdk-mintd answers
`/v1/info` but never settles quotes, which hangs every payment test past the
timeout. An unhealthy mint is restarted automatically. The pytest invocation
uses `--timeout-method=signal` so a hung test dies with a stack dump instead
of surviving as an unkilleable thread.

## What this validates

- API tests over SSH + HTTP (the `tests/api/` suite)
- Payment flow: token mint → `pay_direct` through the Debian client → backend
  swap → MAC authorization → gate open
- Mint quote lifecycle against a real cdk-mintd FakeWallet
- Serial-fallback provisioning (on fresh overlays) and cloud-init provisioning
- Reseller-mode scenarios (`run-reseller-scenarios`)

## What still requires physical hardware

Android/ADB phone tests, WiFi radio behavior, LuCI browser tests against real
radios, and destructive sysupgrade flows.

## Deploying TollGate builds into the lab

`run-local-tests.sh` runs pytest with `--no-deploy`; deploy the build under
test first:

```bash
sshpass -p <password> scp -O <tollgate-wrt>.ipk root@10.99.99.1:/tmp/tg.ipk
sshpass -p <password> ssh root@10.99.99.1 \
  'opkg install --force-downgrade --force-overwrite /tmp/tg.ipk'
```

Cross-version reinstalls need those force flags (opkg treats a changed
version prefix as a downgrade). Clear `/etc/tollgate/wallet.db` when
switching mints.
