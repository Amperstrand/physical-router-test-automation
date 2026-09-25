# tollgate-installer E2E Tests

End-to-end tests for the [tollgate-installer](https://github.com/OpenTollGate/tollgate-installer)
onboarding wizard against a fresh OpenWrt QEMU VM. The suite is fully
self-provisioning: it boots its own isolated router VM, builds the wizard
binary, and drives the wizard's REST API exactly like the browser UI does.

## What it covers

| Test | Verifies |
|------|----------|
| `test_scan_discovers_fresh_router` | `/api/scan` ARP discovery finds the fresh VM (`ssh_open`, OpenWrt 24.10 firmware probe) |
| `test_deploy_completes_all_steps` | `POST /api/deploy` (wan mode) reaches `done`; all 12 steps complete; tollgate-wrt installed from FreedomTechFeed |
| `test_router_branded_and_healthy` | Post-install SSH state: `tollgate-XXXX` hostname, tollgate-wrt package, `:2050`/`:2121` listening, kind 10021 health ad, `tollgate.lan` DNS, Lightning address in `identities.json` |

## Running

```bash
source ~/.tollgate-test-venv/bin/activate
pytest installer/ -v --timeout-method=signal
```

Full run: ~5 min (VM boot + go build + scan ~45s + deploy ~90s + verification).

## Prerequisites (auto-skipped when absent)

- Linux with `/dev/kvm`, `qemu-system-x86_64`, `qemu-img`, `sshpass`, passwordless `sudo`
- Baked base image `~/tollgate-virtual-lab/images/openwrt-base.qcow2` (fresh OpenWrt 24.10.1, SSH + root password, no TollGate)
- `credentials/virtual-lab-credentials.json` with that base image's root password
- Installer source at `~/src/tollgate-installer` plus `go`, or a prebuilt binary

## Environment overrides

| Var | Default | Purpose |
|-----|---------|---------|
| `TOLLGATE_INSTALLER_SRC` | `~/src/tollgate-installer` | Source checkout; the suite builds from a **clean clone** (committed state only — WIP in the checkout is never the test target) |
| `TOLLGATE_INSTALLER_BIN` | — | Use a prebuilt wizard binary directly (skips clone + go build) |

## Isolation

Own bridge `tg-inst-br` (`10.99.95.0/24`, host `.2`, VM `.1`) with NAT via
MASQUERADE — nothing on the poc lab (`10.99.99.x`), `10.99.88.x`,
`10.99.87.x`, or the physical LAN is touched. The wizard port is picked from
`8199` upward. Setup is idempotent: a leftover bridge/tap/VM from a crashed
run is cleaned up before each session and torn down after.
