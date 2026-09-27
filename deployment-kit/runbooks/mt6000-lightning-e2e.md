# Runbook — MT6000 Lightning end-to-end (verified 2026-09-17)

Concrete reproduction of the paid Lightning flow against a GL-MT6000, including
the "build on one machine, drive from the LAN host" pattern.

## Topology used

```
dq05 (build/mint tooling host)  --ssh-->  CobradorWave 192.168.2.21
                                                  │ USB-eth enx… static 192.168.1.2/24
                                                  ▼
                                          GL-MT6000 192.168.1.1 (br-lan)
                                            tollgate-wrt :2121
                                            portal SPA   :2051
                                            nodogsplash  :2050
```

`c03rad0r@192.168.2.21` is the LAN gateway host; the router answers on
`192.168.1.1` (ICMP is filtered — verify with TCP, not ping).

## Steps

### 1. Start the mint on the LAN host

```sh
# on CobradorWave (reachable at 192.168.1.2 from the router)
MINT_HOST=192.168.1.2 ./scripts/bring-up-fakewallet-mint.sh
curl -s http://192.168.1.2:3338/v1/info | head -c 120   # cdk-mintd/0.18.0
```

### 2. Point the router at it

```sh
ROUTER_PASSWORD=... ./scripts/configure-router-test-mint.sh http://192.168.1.2:3338
# advertised mints should show:
#   ["price_per_step","cashu","1","sat","http://192.168.1.2:3338","0"]
```

### 3. Prove the paid flow

```sh
ROUTER_IP=192.168.1.1 MINT_URL=http://192.168.1.2:3338 ./scripts/lightning-e2e.sh
```

Observed result:

```
POST: {"status":1,"quote":"01a0b185-…","invoice":"lnbc10n1p…","mint_url":"http://192.168.1.2:3338",
       "amount":1,"expiry":…,"state":"UNPAID","access_granted":false}
1: {"state":"ISSUED","access_granted":true,"allotment":22020096}
PASS: access granted (allotment=22020096)
```

`allotment=22020096` = 21 MiB, matching the router's `step_size` (bytes metric).

### 4. Restore

```sh
ROUTER_PASSWORD=... ./scripts/configure-router-test-mint.sh --restore
```

## What the failure modes looked like

| Symptom | Cause | Fix |
|---|---|---|
| `Signature missing or invalid` in `ensureLightningAccessGranted` | mint older than cdk-mintd ~0.17 (wallet is cdk-go 0.17.3) | run cdk-mintd `0.18.0` |
| `failed to open gate: exit status 1` | client MAC not yet known to NDS | fetch `:2050/` + `:2051/splash.html` before paying |
| `GET /ln-invoice` returns `state:""` for a while | quote not yet settled at the mint | keep polling; `ISSUED`/`PAID` precedes `access_granted` |
| mint swap ignored | stale `/etc/tollgate/wallet.db` | delete it (done by the configure script) |

## Build-on-one-host, deploy-from-another

If the portal build runs on a machine that cannot reach the router LAN, ship the
tarball through the LAN host:

```sh
tar czf /tmp/tg-portal-build.tgz -C build .
cat /tmp/tg-portal-build.tgz | ssh lanhost 'cat > /tmp/tg-portal-build.tgz'
ssh lanhost 'sshpass -p "$PW" scp -O /tmp/tg-portal-build.tgz root@192.168.1.1:/tmp/'
```

Then run the extraction/`touch`/uhttpd steps from `hot-deploy-portal.sh` on the
LAN host.

## Verified versions

- Router: GL-MT6000, `tollgate-wrt 0.6.0_alpha2_pre5-r1` (pre6+ same behaviour)
- Backend wallet: cdk-go 0.17.3 (module `src/tollwallet`)
- Mint: cdk-mintd 0.18.0, `fakewallet`, `min/max_delay_time=0`
