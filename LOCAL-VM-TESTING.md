# Local QEMU VM Testing

Run TollGate E2E tests against a local OpenWrt 24.10.1 QEMU VM — no physical
router or cloud account needed.

## Prerequisites

```bash
# Ubuntu host packages
sudo apt install qemu-system-x86 qemu-utils sshpass curl

# Python venv with PRTA dependencies
./scripts/setup-python.sh
source ~/.tollgate-test-venv/bin/activate

# CDK mint binary (fakewallet backend, no Lightning needed)
ls /opt/cdk-mintd/cdk-mintd  # should exist if baked
```

## Quick Start

```bash
cd ~/src/physical-router-test-automation

# 1. Start OpenWrt + Debian client VMs
python3 scripts/virtual-lab.py start-poc --host localhost

# 2. Run tests (starts fakewallet mint automatically)
./scripts/run-local-tests.sh tests/api/test_nds_fw4_integration.py

# 3. Clean up
python3 scripts/virtual-lab.py stop-poc --host localhost
```

## Architecture

```
Host (10.99.99.2)
├── tg-poc-br (10.99.99.0/24)
│   ├── OpenWrt VM (10.99.99.1) — NDS + tollgate-wrt + nftables
│   │   └── br-lan bridge with eth0 port
│   └── Debian VM (10.99.99.100+) — test client (DHCP)
├── CDK fakewallet mint (10.99.99.2:8383) — free Cashu tokens
└── Host provides NAT + default route for VMs
```

## VM Details

| VM | IP | Image | Role |
|----|-----|-------|------|
| OpenWrt | 10.99.99.1 | `overlays/tollgate-poc.qcow2` | Router under test |
| Debian | DHCP from .100 | `overlays/debian-client.qcow2` | Test client |

- OpenWrt root password: `tollgate` (set by provisioning script)
- SSH: `sshpass -p tollgate ssh root@10.99.99.1`
- Serial consoles: `~/tollgate-virtual-lab/run/serial.sock`

## Deploying Changes

### Deploy an nftables include (e.g., PR #283)

```bash
sshpass -p tollgate ssh root@10.99.99.1 'cat > /etc/nftables.d/20-nds-enforce.nft' \
    < ~/src/tollgate-module-basic-go/packaging/files/etc/nftables.d/20-nds-enforce.nft
sshpass -p tollgate ssh root@10.99.99.1 'fw4 reload'
```

### Deploy captive portal site

```bash
# Build from configurationwizzard
cd /tmp/cw-build && npm run build

# Deploy portal to NDS htdocs
sshpass -p tollgate scp -O dist/portal/* root@10.99.99.1:/etc/nodogsplash/htdocs/

# Deploy balance page
sshpass -p tollgate ssh root@10.99.99.1 'mkdir -p /www/net4sats/assets'
sshpass -p tollgate scp -O dist/balance/* root@10.99.99.1:/www/net4sats/
```

### Configure backend to use local mint

```bash
sshpass -p tollgate ssh root@10.99.99.1 '
jq ".accepted_mints = [{"url":"http://10.99.99.2:8383"}]" \
    /etc/tollgate/config.json > /tmp/cfg.json && mv /tmp/cfg.json /etc/tollgate/config.json
/etc/init.d/tollgate-wrt restart
'
```

## Fakewallet CDK Mint

`run-local-tests.sh` starts a CDK mint with `ln_backend = "fakewallet"` on port
8383. This allows free Cashu token minting — no Lightning payment needed.

The mint auto-starts/stops with the test runner. Manual control:

```bash
# Start manually
/opt/cdk-mintd/cdk-mintd -c /tmp/cdk-mintd-local/config.toml &

# Verify
curl http://10.99.99.2:8383/v1/info
```

## Available Tests

| File | What it tests |
|------|---------------|
| `tests/api/test_nds_fw4_integration.py` | NDS + fw4/nftables enforcement chain |
| `tests/api/test_local_payment.py` | Cashu payment flow (fakewallet) |
| `tests/api/test_quote_persistence.py` | Lightning quote persistence |
| `tests/api/test_lightning_backoff.py` | Quote monitor backoff/jitter |
| `tests/api/test_payment_regression.py` | Payment edge cases |
| `tests/api/test_mint_url_fuzzy.py` | Mint URL matching |
| `tests/unit/` | Harness logic (no router needed) |

Run specific tests:

```bash
./scripts/run-local-tests.sh tests/api/test_local_payment.py
```

## Troubleshooting

### VMs won't start

```bash
# Check host readiness
python3 scripts/virtual-lab.py doctor

# Recreate base image from scratch
python3 scripts/virtual-lab.py prepare-image

# Debug running VMs
python3 scripts/virtual-lab.py debug-poc --host localhost
```

### Debian client unreachable

NDS may block DHCP (UDP 67/68). Check:

```bash
sshpass -p tollgate ssh root@10.99.99.1 \
    'iptables -L ndsRTR -n -v | grep udp'
```

If missing, add to NDS config:

```bash
sshpass -p tollgate ssh root@10.99.99.1 '
uci add_list nodogsplash.@nodogsplash[0].users_to_router="allow udp port 67"
uci add_list nodogsplash.@nodogsplash[0].users_to_router="allow udp port 68"
uci add_list nodogsplash.@nodogsplash[0].users_to_router="allow udp port 53"
uci commit nodogsplash && /etc/init.d/nodogsplash restart
'
```

### Filesystem corruption (after unclean shutdown)

```bash
# Fix overlay filesystem
sudo qemu-nbd --connect=/dev/nbd0 ~/tollgate-virtual-lab/overlays/tollgate-poc.qcow2
sudo e2fsck -fy /dev/nbd0p2
sudo qemu-nbd --disconnect /dev/nbd0
```

### Stale processes

```bash
# Kill all QEMU VMs
sudo pkill -9 -f qemu-system

# Clean up network interfaces
sudo ip link del tg-poc-tap 2>/dev/null
sudo ip link del tg-poc-tap2 2>/dev/null
```

## SHC / Cloud Lab Cleanup

```bash
# Remove stale SHC VMs (older than 2h)
python3 scripts/cloud-lab.py cleanup-stale

# Remove ALL cloud lab VMs
python3 scripts/cloud-lab.py cleanup-all
```
