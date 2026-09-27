# Rig signet stack (owner milestone: real-signet E2E)

Prior art: conwrt branches `tollgate-signet-e2e` (2454d83) and
`tollgate-signet-e2e-live` (41d60bb) — the recorded green flow was
NUT-04 quote → CLN xpay (restricted rune) → nutshell ecash → tollgate
POST → NDS auth.

## Pieces on this host

- **Signet mint (canonical for beta + phone): `http://192.168.103.2:8190`**
  — the cdk-signet-pilot on `inr2.cashu.exchange` (cdk-mintd/0.18.0-rc.0,
  CoreLightning backend), NOT a local daemon: it rides a persistent SSH
  tunnel from `~/rig-signet-tunnel/start.sh`, bound on 127.0.0.1:8190
  (host tools), 192.168.103.2:8190 (beta/phone view), 192.168.105.2:8190
  (alpha view). clnrest :3011 stays loopback-only (payer API never faces
  customer networks). One-spelling rule per router (#375/#480 class).
- **CLN payer creds**: `~/.config/conwrt/signet-cln.json` (restricted rune:
  xpay/listpays/getinfo, rate 3/s — recipe in conwrt
  tests/integration/signet_cln.py docstring).
- **nutshell CLI**: `~/src/conwrt/tests/integration/.venv-cashu/bin/cashu`
  (fresh wallet per run — the pilot mint rotates keysets; stale cached
  keysets fail with 12001).
- **Phone wallet (web, plain http by design)**: cashu.me self-host —
  `http://192.168.103.2:3080` (docker compose in `~/src/cashu-me`,
  container `cashu.me`, ufw open for 103.0/24; loopback:3000 was occupied
  by an unrelated service hence 3080). Engraft is NOT verified to exist —
  do not use. Native APKs block plain-http mints (cleartext policy,
  researched) — web wallet on same-scheme http is the only on-phone
  wallet class that works here.
- **Rehearsal mint**: local fakewallet `http://192.168.105.2:8383`
  (canonical spelling for alpha-side tooling; for the PHONE use
  `http://192.168.103.2:8383` — same listener, beta's view; do not mix
  spellings within one router's config).

## Pending on beta's return (bench-hands)

1. NDS preauth allowlist: tcp to 192.168.103.2 ports 3080+8190+8383
   (wallet + mints reachable pre-auth; wallet swaps work pre-payment).
2. Register the SIGNET mint in beta's accepted_mints; wallet re-fund is
   fakewallet-only — for signet the ROUTER wallet needs its own signet
   tokens only if we test payouts; for pay→access the customer token
   suffices.
3. Soak 1–2 min before GO (policy after the 14:24 flap).

## Token minting (host, for seeding the phone wallet)

```
CASHU=~/src/conwrt/tests/integration/.venv-cashu/bin/cashu
W=$(mktemp -d)
$CASHU -w $W mint http://192.168.103.2:8190 <sats>   # NUT-04 quote → xpay
# or the conwrt test's programmatic flow (signet_cln.xpay + nutshell)
```
The token's embedded URL is whatever --mint-url used — mint with the
103.2 spelling so phone→portal→router all agree.
