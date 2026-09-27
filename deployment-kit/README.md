# physical-router-deployment-kit

Reproducible bring-up + end-to-end test tooling for **physical** TollGate routers
(GL.iNet GL-MT3000 / GL-MT6000 and friends).

This kit captures the exact, hard-won procedure for driving a real router through
a **paid** Lightning / Cashu flow without real funds, plus the fast portal
hot-deploy loop. It complements
[`physical-router-test-automation`](https://github.com/OpenTollGate/physical-router-test-automation)
(Playwright/pytest suites) by providing the deployment scaffolding those tests
assume.

## Why a local FakeWallet mint

A TollGate's `/ln-invoice` flow creates a **NUT-04 mint quote** at the
configured mint and waits for it to be paid. Public mints cannot be made to
auto-pay, so a paid flow is impossible without funds. `cdk-mintd`'s `fakewallet`
backend auto-settles quotes, so the backend can mint tokens and grant access
end-to-end.

**Version pin matters.** The Go backend's wallet (`cashubtc/cdk-go 0.17.3`)
verifies the mint-quote signature. Mints older than cdk-mintd ~0.17 do not emit
a signature it accepts; settlement fails with:

```
monitorLightningQuote: ensureLightningAccessGranted failed: Signature missing or invalid
```

Use `CDK_VER=0.18.0` (verified working). A hosted testnut mint is **not**
sufficient — this is a mint-generation mismatch, not a network issue.

## Topology

```
  LAN gateway / test host  (e.g. "CobradorWave", 192.168.1.2)
    ├─ cdk-mintd 0.18 FakeWallet  :3338     <- reachable from the router LAN
    └─ drives scripts below (ssh to the router)
                    │
  TollGate router  (192.168.1.1)
    ├─ tollgate-wrt  backend      :2121
    ├─ uhttpd portal SPA          :2051   (/etc/tollgate/tollgate-captive-portal-site)
    └─ nodogsplash                :2050
```

The client driving the flow must sit **on the router LAN** (a second machine, or
the router's own LAN peer). The backend binds the quote to the client's MAC and
calls `ndsctl auth <mac>`; running the curl from the router itself breaks MAC
lookup.

## Quick start — prove Lightning end-to-end

On a host with router-LAN access:

```bash
# 1. start a local FakeWallet mint (auto-pays NUT-04 quotes)
./scripts/bring-up-fakewallet-mint.sh
#    -> reachable at http://192.168.1.2:3338 (host autodetected)

# 2. point the router at it (backs up config + wallet cache first)
ROUTER_PASSWORD=... ./scripts/configure-router-test-mint.sh http://192.168.1.2:3338

# 3. drive the paid flow
ROUTER_IP=192.168.1.1 MINT_URL=http://192.168.1.2:3338 ./scripts/lightning-e2e.sh
#    -> [ln] PASS: access granted (allotment=22020096)

# 4. restore the router's shipping config
ROUTER_PASSWORD=... ./scripts/configure-router-test-mint.sh --restore
```

## Scripts

| Script | Purpose |
|---|---|
| `bring-up-fakewallet-mint.sh` | Download + run cdk-mintd `fakewallet` natively on a host reachable from the router LAN. |
| `configure-router-test-mint.sh` | Backup the router config/wallet and swap `accepted_mints` to the test mint (and `--restore`). |
| `hot-deploy-portal.sh` | Build the portal SPA and hot-deploy it to `:2051` without a feed rebuild. |
| `lightning-e2e.sh` | Prime NDS, create an invoice, poll until `access_granted=true`. |

## Portal hot-deploy loop

```bash
PORTAL_REPO=~/repos/tollgate-captive-portal-site \
ROUTER_PASSWORD=... \
  ./scripts/hot-deploy-portal.sh
```

The served webroot on the router is
`/etc/tollgate/tollgate-captive-portal-site` (uhttpd instance on `:2051`). The
first deploy keeps a copy at `.pre-deployment-kit`. The script `touch`es the
files after copying so uhttpd advertises a sane `Last-Modified` (reproducible
packages ship `1970-01-01`, which makes browsers cache the SPA heuristically for
years).

## Gotchas (learned the hard way)

- **Mint generation must match the wallet.** cdk-mintd `<0.17` → `Signature
  missing or invalid` during settlement even though invoice creation succeeds.
- **`wallet.db` caches mints.** After changing mint URLs, delete
  `/etc/tollgate/wallet.db` (the script does) — otherwise the change is ignored.
- **Prime NDS before paying.** `ndsctl auth` only works for MACs NDS already
  tracks, so fetch `http://<router>:2050/` (and the `:2051` portal) from the
  client first, or gate-open fails with `failed to open gate: exit status 1`
  (payment was already consumed).
- **Cashu at the mint is consumed before the gate opens.** A failed gate-open
  still burns the token.
- **Use `scp -O`** for OpenWrt (BusyBox has no `sftp-server`).
- **The router's `:2121` is bound on all interfaces** and clients may reach it;
  this is by design for the payment flow.

## Companion suites

Hardware/browser regression specs live in `physical-router-test-automation`
(e.g. `tests/browser/tollgate-portal-*.spec.mjs`). Point `ROUTER_IP` at the
router and run them from the same LAN host.

## License

GPL-3.0-only (matches the TollGate project).
