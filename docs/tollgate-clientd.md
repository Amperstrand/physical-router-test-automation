# tollgate-clientd — Debian laptop lane

`tollgate-clientd` keeps a laptop alive behind a TollGate: it discovers the
gateway on `:2121`, registers with the captive portal, shows remaining
bytes/seconds, and auto-tops-up with ecash from a local wallet before the
allotment runs out.

- **Source of truth**: `scripts/tollgate-clientd.py` here is vendored from
  [tollgate-module-basic-go](https://github.com/OpenTollGate/tollgate-module-basic-go)
  (`scripts/tollgate-clientd.py`, commit `9e89ab5`). Re-vendor on updates and
  bump the provenance line in the docstring.
- **Wallets**: `cdk-cli` (modern `--v3 --amount` and legacy stdin CLIs) or
  nutshell (`cashu --host <mint> --yes send <amount>`), auto-detected.
- **Protocol**: `GET :2121/` advertisement → `POST :2121/?mac=` raw token →
  `GET :2121/usage` `"used/allotment"` (`-1/-1` = no session).

## Quick start (Debian lane)

```bash
pip install cdk-cli   # or: cargo install cdk-cli; or pip install cashu (nutshell)

# one-shot status (JSON for scripts)
scripts/tollgate-clientd.py --status --json

# daemon: buy 1 step at a time, renew when 30s remain
scripts/tollgate-clientd.py --steps 1 --renew-below 30s
```

Against the module repo's cloud lab (real backend, no hardware):

```bash
cd tollgate-module-basic-go/tests/cloud-lab
docker compose up -d mint upstream
docker compose restart upstream                      # clear in-memory sessions
docker compose run --rm client -sv test_clientd_autotopup.py
```

## Status bar (waybar)

`--waybar` emits module JSON; add to waybar config:

```json
"custom/tollgate": {
    "exec": "~/src/physical-router-test-automation/scripts/tollgate-clientd.py --waybar",
    "interval": 5,
    "return-type": "json",
    "tooltip": true
}
```

CSS classes: `good` (green), `warning` (within 2× the renewal threshold),
`critical` (no session). Preview render:
`evidence/2026-09-19-tollgate-clientd/waybar-preview.png`.

## The laptop lane (real router, real NoDogSplash)

`tests/laptop/test_laptop_clientd.py` runs clientd from the controller
host against a real TollGate router — the one class the docker cloud-lab
cannot test: the router itself resolves our IP to a MAC (ARP), port-80
traffic registers the MAC with NoDogSplash, and a real payment leaves the
MAC `Authenticated` in `ndsctl`. Verified on the local QEMU OpenWrt venue
(router at `10.99.99.1`, NDS 5.0.2) — the same lane runs unchanged
against a physical router by pointing the env vars at it.

Provisioning (local QEMU venue):

1. **Mint** the router's `accepted_mints` URL must answer from the router.
   The venue routers expect `http://10.99.99.2:8085`; run a FakeWallet
   cdk-mintd there (`config init --new-mint` with the canonical mnemonic,
   see `~/tollgate-virtual-lab/mint-8085/`) and open the host firewall:
   `sudo ufw allow from 10.99.99.0/24 to any port 8085 proto tcp`.
2. **Wallet** a funded cdk-cli wallet the host can call. Without a rust
   toolchain, a shim runs cdk-cli from the cloud-lab-client image:

   ```bash
   # ~/bin/cdk-cli — forwards to docker, mounting any -w wallet dir
   # (auto-detects the modern --v3 --amount CLI)
   exec docker run --rm --network host $(mount args…) \
       --entrypoint cdk-cli cloud-lab-client "$@"
   ```

   Fund it: `cdk-cli -w /tmp/laptop-wallet mint http://10.99.99.2:8085 100`.
3. **Run** (the Makefile target takes the hardware lock):

   ```bash
   make lock PHASE="laptop lane"
   PATH=~/bin:$PATH LAPTOP_GATEWAY=10.99.99.1 make test-laptop-clientd
   ```

Recording a demo with the router's live log (`cmd:ssh` log source —
works for physical routers too):

```bash
python3 scripts/record-demo.py \
    --clientd-cmd "python3 scripts/tollgate-clientd.py --gateway 10.99.99.1 \
        --wallet-dir /tmp/laptop-wallet --steps 1 --renew-below 25MB --interval 2" \
    --log-source "cmd:sshpass -p tollgate ssh root@10.99.99.1 logread -f" \
    --duration 60 --out evidence/$(date +%F)-laptop-lane --export-video
```

Session reset between runs: `ssh root@<router> 'ndsctl deauth <mac>;
/etc/init.d/tollgate-wrt restart'` (sessions are in-memory per MAC).

## Mint sources: FakeWallet fixture vs the signet zoo

The lanes default to the throwaway FakeWallet fixture mint (identical
keys everywhere, tokens are free, nothing is real). For real-settlement
runs — or when the fixture box is down — point the rig at the standing
[cashu-mint-zoo](https://github.com/Amperstrand/cashu-mint-zoo) signet
mints instead. As of 2026-09-22 the **router side works against zoo
mints** (V2 keysets verified end-to-end, kind-1022 from both cdk 0.18
and nutshell 0.21 mints; see the zoo's
`evidence/zoo-router-acceptance.md`).

Two differences matter operationally:

1. **Funding is real**: `pay-and-mint.sh <wallet> <mint-url> 1000` from
   the zoo checkout pays a signet invoice through inr2 and mints real
   tokens (keep amounts in the 1000–5000 sat range). There is no
   FakeWallet auto-refill — budget tokens per run, and remember each
   *rejected* payment burns its token.
2. **Router mint entries need their pricing fields**: repointing
   `accepted_mints` with a bare `{"url": …}` object passes the health
   probe but grants nothing — payments fail at allotment calculation
   (price_per_step 0, division by zero) *after* the token was received.
   Copy a full mint entry and change only the URL:

```bash
ssh root@$ROUTER 'jq ".accepted_mints = [{url: \"https://cdk-d3dec24.cashu.exchange\",
  min_balance: 0, balance_tolerance_percent: 0, price_per_step: 1,
  price_unit: \"sats\", purchase_min_steps: 0}]" /etc/tollgate/config.json \
  > /tmp/c.json && mv /tmp/c.json /etc/tollgate/config.json \
  && /etc/init.d/tollgate-wrt restart'
```

The venue router needs outbound HTTPS to the zoo hostnames (default
route via a NAT-ing lab host is enough — the mints answer through
their public Cloudflare tunnel from anywhere). Client-side, the zoo is
drop-in via `TOLLGATE_TEST_MINT_URL=https://cdk-d3dec24.cashu.exchange`
and wallets mint/pay exactly as against the fixture.

## Regression lane

`tests/unit/test_tollgate_clientd.py` runs the script's built-in
`--selftest` (in-process mock TollGate — no hardware, mint, or wallet) and
pins the CLI flags the lanes and waybar config depend on.

## Evidence

- `evidence/2026-09-19-tollgate-clientd/cloud-lab-demo.txt` — full demo
  against the real backend: discovery, status, waybar JSON, daemon paying
  1 sat for a minute, auto-renewal at the 45s threshold (`0:45 left →
  paid → 1:59 left`), persistence after daemon exit.
- `evidence/2026-09-19-tollgate-clientd/waybar-preview.png` — status-bar
  rendering of the three module states.
- `evidence/2026-09-20-laptop-lane-hardware/` — the laptop lane recorded
  against the real OpenWrt/NDS venue: 2 payments (initial + threshold
  renewal) with the router's own `logread -f` synchronized alongside —
  including the `nodogsplash: Authenticating <ip> <mac>` line proving the
  port-80 MAC registration. `events.json` + `player.html` committed; the
  `demo.webm` export is gitignored, regenerate with `--export-video`.
