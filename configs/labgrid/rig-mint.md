# Rig lab Cashu mint + tollwallet funding path

Host-side mint for the router-to-router payment plan (Phase 2, 2026-09-25).
Recipe follows PRTA `scripts/provision-local-lab.sh` (cdk-mintd +
`ln_backend = "fakewallet"`, zero fees, sat only), adapted to the new
cdk-mintd 0.18 database-backed config CLI.

## Endpoint

- **Mint URL (canonical): `http://192.168.105.2:8383/`**
  (= host secondary IP on router-alpha's L2; the URL string is baked into
  tokens/keysets, so it must be the one the ROUTERS use).
- Mintd listens on `0.0.0.0:8383` (reachable via every host IP:
  192.168.13.221, 192.168.103.2, 192.168.105.2, 192.168.108.2).
- Version cdk-mintd/0.18.0, pubkey
  `03d902f35f560e0470c63313c7369168d9d7df2d49bf295fd9fb7cb109ccee0494`
  (from `curl http://192.168.105.2:8383/v1/info`, 2026-09-25).
- Fakewallet LN backend: mint quotes auto-pay instantly, no Lightning
  needed. Fees: 0 (fee_percent 0, reserve_fee_min 0).

## Files / lifecycle (host)

- `~/rig-lab-mint/config.toml` — legacy-format source (kept for reference)
- `~/rig-lab-mint/config-v2.toml` — migrated 0.18 document (validated)
- `~/rig-lab-mint/cdk-mintd-secrets/mint-mnemonic` — mint seed (extracted
  by `config migrate`; DO NOT delete — keysets/counter derivation depend
  on it)
- `~/rig-lab-mint/db/` — sqlite database (wallets of the mint, keysets)
- `~/rig-lab-mint/start.sh` — idempotent start (kills old, serves new,
  log at `~/rig-lab-mint/mintd.log`)
- Health: `curl -sf http://192.168.105.2:8383/v1/info`

One-time init already done:
`cdk-mintd -w db config init --file config-v2.toml --new-mint`.
Config changes: edit `config-v2.toml` → `config apply --file ...` → restart.

## Host-side token minting (test funding source)

```sh
CLI=/opt/cdk-mintd/cdk-cli
MINT=http://192.168.105.2:8383/
W=$(mktemp -d)
$CLI -w $W mint $MINT 1000                 # fakewallet auto-pays the quote
TOKEN=$(echo 500 | $CLI -w $W send --mint-url $MINT | grep '^cashu' | tail -1)
```

Round-trip smoke (proven 2026-09-25): mint 1000 → send 500 → second wallet
`receive --allow-untrusted <token>` → balances 500/500. Note: cdk-cli 0.18
requires `--allow-untrusted` for a mint not in the wallet's trust list on
first receive.

## Router-side funding path (once golden routers are back)

Two steps, both over SSH (`ssh root@<dut>`), no router internet needed:

1. **Register the mint** in `/etc/tollgate/config.json`
   (`accepted_mints`, plain http is accepted — same shape as PRTA's local
   lab: `{"url": "http://192.168.105.2:8383", "min_balance": 0,
   "balance_tolerance_percent": 0, "price_per_step": 1,
   "price_unit": "sats", "purchase_min_steps": 0}`), then restart the
   module. TMBG dev builds also auto-inject a test mint — remove/override
   via config when this mint must be the one used.
2. **Fund the wallet**: `tollgate wallet fund <cashu-token>` (operator
   guide §Wallet operations; the CLI talks to the running module's UNIX
   socket `/var/run/tollgate.sock`). Verify with `tollgate wallet
   balance` / `wallet info`; recover funds with `wallet drain cashu`.

## Constraints / gotchas

- **Mint URL aliasing is fund-brick class** (TMBG #375/#480): one router
  must know the mint under exactly ONE spelling. Router-alpha can only
  route within 192.168.105.0/24 → it must use `192.168.105.2`. If beta
  lands on a different L2 post-golden, either place both routers on one
  L2 or give the host a routing path — never register `103.2:8383` and
  `105.2:8383` as mints on the SAME router.
- Router-side restores/restarts are safe (mint state is host-side); the
  host mint must keep the same mnemonic + DB to stay the "same" mint.
- cdk-mintd 0.18 rejects the legacy `-c` startup; always start via
  `~/rig-lab-mint/start.sh`.
