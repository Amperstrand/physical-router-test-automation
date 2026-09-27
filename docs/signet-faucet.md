# Signet faucet — CLN auto-pay for small invoices

Deployed 2026-09-17 on **inr2.cashu.exchange** (signet CLN hub container
`cln-hub-signet`, v26.06.7, alias `playground-bridge`). Signet only — signet
funds are worthless; this turns the hub into a faucet for Cashu mints that
settle bolt11 invoices through it.

## Rule

An unpaid invoice **< 1000 sats** becomes eligible once it has been observed
for at least **(its amount in sats) seconds**. A 60-sat invoice is paid by
the next minute tick; a 500-sat invoice waits ~500 s. Amountless and larger
invoices are ignored.

## Layered caps

| Layer | Cap | Enforced by |
|---|---|---|
| Rune (CLNRest) | `pay` method only | verified: `getinfo`/`listinvoices` → 401 |
| Rune (CLNRest) | bolt11 amount < 1000 sats (`pinvbolt11_amount<1000001`) | verified: 2000-sat pay → 401 "pinvbolt11_amount is greater or equal to 1000001"; 4-sat → 201 paid |
| Rune (CLNRest) | ≤ 3 pays/minute (`rate=3`) | rune-encoded (count-based) |
| Script | ≤ **10 000 sats/hour** sat-sum budget | `state.json` hourly ledger |
| Script | ≤ 5 payment attempts per invoice, then abandon | `state.json` attempts |

Runes cannot express sat-sum-per-window (ElementsProject/lightning#7020 —
our own issue; PR #7165 added `pinvX_N` fields) — hence the layered design:
even if the script is buggy, the rune alone bounds exposure to
3 × 1000 sats/min.

**Rune grammar note (cost us one escaped-pipe bug):** alternatives within a
restriction must be **separate array elements** —
`[["method/pay","pinvbolt11_amount<1000001"]]`. Joining them with `|` inside
one string encodes a literal-pipe escape (`\|`) and silently disables the
condition.

## Files

On inr2:
- `/root/signet-faucet/faucet.py` — the faucet (in this repo:
  `scripts/signet-faucet/faucet.py`)
- `/root/signet-faucet/rune.txt` — the pay-only rate-limited rune (mode 600)
- `/root/signet-faucet/state.json` — first-seen timestamps, attempts, hourly ledger
- `/etc/cron.d/signet-faucet` — `* * * * *` root, flock-guarded
- `/var/log/signet-faucet.log`

Access path: Mac → `ssh ai-legion` → `ssh root@inr2.cashu.exchange` (keys
already in place). Reads invoices via `docker exec … lightning-cli
listinvoices`; pays via **CLNRest** `http://172.20.0.12:3010/v1/pay` with the
rune (local CLI has no rune enforcement — local socket is god-mode).

## Operation

```bash
# watch it work (from ai-legion):
ssh root@inr2.cashu.exchange tail -f /var/log/signet-faucet.log
# manual run / dry run:
ssh root@inr2.cashu.exchange /usr/bin/python3 /root/signet-faucet/faucet.py [--dry-run]
# rotate the rune (revoke): delete rune.txt, re-run deploy
```

Deployed evidence (first minutes): settled a backlog of mint invoices
(4–100 sats each, correctly age-gated), hourly ledger tracking, 2000-sat
refusal + 4-sat success through the fixed rune.
