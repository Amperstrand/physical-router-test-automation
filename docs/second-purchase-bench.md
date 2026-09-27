# Second-purchase bench — does buy #2 re-open the gate?

**Lane:** `scripts/mt3000-bench/second-purchase-e2e.sh` (dry run by default)
**Status of the measured result below:** one configuration, one run, 2026-09-26. Not a fix, not
a release gate — a reproducible reproduction kit plus an honest record of what it showed.

## The question

A client buys internet, exhausts the allotment, the gate closes — and a **second purchase does
not re-open it**. That is the operator's report. The lane below answers it for one specific
configuration (cashu-token lane, 63 steps, wired macvlan client) and *only* for that one.

## Running it

```sh
# 0. what will it do? (no router, no lock, nothing spent)
make second-purchase-e2e

# 1. the real run (spends both tokens; takes the bench lock itself)
make second-purchase-e2e SECOND_PURCHASE_ARGS="--purchase" \
     TOKEN_1=~/.tg-e2e/tokens/tok1.txt TOKEN_2=~/.tg-e2e/tokens/tok2.txt

# 1b. long runs: launch detached and poll, never in a foreground shell you might kill
make second-purchase-detached SECOND_PURCHASE_ARGS="--purchase" TOKEN_1=... TOKEN_2=...
journalctl --user -f -u tg-second-purchase-*      # or: tail -f $LOG_DIR/e2e-<ts>.log

# the same thing under the sanctioned single-owner window, if you prefer to hold it yourself
bench-with-lock.sh --purpose "second purchase e2e" -- \
  scripts/mt3000-bench/second-purchase-e2e.sh --purchase TOKEN_1=... TOKEN_2=...
```

Tokens (both are single-use — verify before you spend):

```sh
make bench-token-mint BENCH_TOKEN_ARGS=--yes            # 64 sat from the test mint
make bench-token-verify TOKEN_FILE=~/.tg-e2e/tokens/tok1.txt
```

State capture during/after a run:

```sh
make bench-snapshot            # ndsctl json/status, both nft guard chains, /balance /usage, log greps
make bench-snapshot-payload    # the payload it will run on the router, printed locally
```

Every knob is env-driven (`ROUTER_IP`, `BENCH_NIC`, `CLIENT_MAC`, `CLIENT_IP`, `TOKEN_1`,
`TOKEN_2`, `LOG_DIR`, `BURN_ROUNDS`, `BURN_PARALLEL`, `BURN_URLS`, `PROBE_URL`, `EGRESS_URL`,
…). The script's `--help` is its own header and is the authoritative list. No path, NIC or IP
of this machine is baked in: `BENCH_NIC` is auto-detected as **the wired NIC on the router's
/24** (never the default-route interface, which is Wi-Fi), and `CLIENT_IP` defaults to
`<router>/24 + .222`.

## Procedure

| phase | what happens | what it asserts |
|---|---|---|
| 0 | fresh MAC, never seen by nodogsplash | probe `307` → `http://<router>:2050/splash.html?redir=…`, `egress 000`, `session_active:false` |
| 1 | **buy #1** POSTed from the *client's own* interface | `HTTP=200`, `kind:1022`, an `allotment`, probe flips to `204`, log: `Authorization successful…` + `Set data baseline` |
| 2 | **exhaust** — parallel downloads through the router until the meter runs out | usage climbs to the allotment; `session_active:false`; probe flips `204 → 307` twice; log: `Data allotment reached`, `Successfully closed gate`, `Removed expired session` |
| 3 | post-exhaustion state | balance/usage + full router snapshot captured |
| 4 | **`ndsctl deauth <mac>` discriminator** | `rc=1 Client not found` ⇒ no stale nodogsplash session was holding the gate shut |
| 5 | **buy #2** from the client's interface | `kind:1022` with a **new** allotment, probes `204/204`, `egress code=200` |

Exit codes: `0` every assertion held · `2` usage/preflight · `3` bench held by another window ·
`4` not in a bench window · `5` stale holder line (explicit reclaim only) · `10` buy #1 never
opened the gate (setup problem) · `11` allotment never exhausted inside the budget
(**inconclusive, never a pass**) · `12` buy #2 did not re-open the gate (bug reproduced) ·
`13` an assertion failed · `14` tokens are not spendable.

## Measured result — 2026-09-26, bench MT3000

Configuration: OpenWrt 25.12.5, `tollgate-wrt 0.6.0_alpha4_pre17-r1`, module pin `2796d96c`,
wired macvlan client with a fresh MAC, raw log `~/tg-manual/e2e-20260926T091043Z.log`.

* fresh MAC: probe **307** → `http://<router>:2050/splash.html?redir=...`, egress **000**,
  `session_active false`
* buy 1 (cashu token POST from the **client's own interface**):
  `HTTP=200 {"kind":1022,...,"allotment":"1387266048"}` then probe **204**;
  module log `Authorization successful for MAC attempts=1 mac_address="..." output="Client ...
  authenticated."` + `Set data baseline`
* exhaustion: usage climbed to **1,287,270,400 of 1,387,266,048 B**, then `session_active:false`
  and the probe flipped **204 → 307 twice**; module log `Data allotment reached for <mac>:
  1.3 GB / 1.3 GB` / `Successfully closed gate` / `Removed expired session`
* `ndsctl deauth <mac>` → `rc=1 Client not found` (no stale nodogsplash session)
* buy 2 → `kind:1022` with a **NEW** allotment, probe **204/204**,
  `egress code=200 bytes=2000000`

**Conclusion (recorded as measured, not as a fix).** In this configuration — the cashu-token
lane, 63 steps, a wired macvlan client — the second purchase **does** re-open the gate. The
operator's reported failure used a **different lane** (portal Lightning invoice), a **one-step
21 MiB allotment** and a **Wi-Fi client**; those remain untested (separate card `t_07e7f66f`).
Do not read this page as "the operator report is fixed".

## What this lane does NOT cover

* the portal **Lightning-invoice** purchase lane (only the cashu-token POST is exercised);
* a **one-step** allotment (this lane uses a 63-step ~1.3 GB allotment);
* a **Wi-Fi** client (a macvlan cannot present a second MAC through an AP association);
* the second purchase as a *state* problem across a reboot/restart.

## Traps (each one cost real time)

1. **The module authorises the MAC of the REQUESTING SOCKET.** A purchase POSTed from the bench
   host with `?mac=<other>` authenticates the **host**, not the client. Every purchase must be
   issued through the client's own interface (`curl --interface <client-ip>`), and the client's
   routes must live in a **separate policy table** (`ip rule from <client-ip> table 100`) so the
   host's own management path to the router survives the run. The script asserts the interface's
   MAC *and* prints the host's own route to the router before it buys anything.
2. **`> /dev/stdout` in a helper TRUNCATES a log file that stdout is redirected to.** Capture
   into a temp file and `cat` it (`router-snapshot.sh` does exactly this and never writes to
   `/dev/stdout`).
3. **Leftover macvlan / ip-rule / ip-route state from a killed run** makes the next run die with
   `RTNETLINK answers: File exists`. Delete first, tolerate a missing object. Worse: a
   **NetworkManager profile** for the vif with `autoconnect=yes` silently re-creates the macvlan
   with a **random cloned MAC** the instant you delete it — the script disables it for the run
   and tells you it did. Plain `nmcli` is not authorised to deactivate a connection here; it
   needs `sudo nmcli`.
4. **Long runs must be launched detached and polled.** A crashed run leaves a straggler holding
   the bench flock and the next run just waits. To kill by pattern use the bracket trick
   (`pkill -f '[e]2e-second-purchase'`) — without it you kill your own shell.
5. **The router DROPS ICMP.** Probe with TCP/HTTP, never `ping`. "Liveness" here is
   `http://<router>:2121/` answering `kind:10021`.
6. **Tokens are single-use.** Verify `UNSPENT` with NUT-07 immediately before a paid run:
   `make bench-token-verify TOKEN_FILE=...`. The e2e does it itself and **fails closed** (exit
   14) when the mint cannot answer — an unreadable spend state is not "unspent".
7. **A stale bench holder line is not a crash.** `bench-lock.sh status` reporting
   `STALE-METADATA` (a holder line with no flock behind it) means the previous owner died. The
   bench is free, but recovery is explicit and operator-only: `bench-lock.sh take
   --reclaim-stale`. The lane refuses (exit 5) rather than stealing the bench.
8. **A macvlan on Wi-Fi proves nothing** — every probe returns `000`. Pick the wired NIC by
   matching an interface address against the router's /24, never the default-route interface.

## Verifying the lane itself (no router)

```sh
bash -n scripts/mt3000-bench/second-purchase-e2e.sh scripts/mt3000-bench/router-snapshot.sh
shellcheck -s bash -S warning scripts/mt3000-bench/*.sh
make bench-tests                     # 23 offline negative controls, including this lane's
python3 -m pytest tests/unit/test_recover_tokens.py
```

`make bench-tests` proves, without a router: the e2e is dry-run by default; a paid run without
tokens is refused (exit 2); a paid run **is refused while another window owns the bench**
(exit 3, holder named, no router probe, no transcript directory created); the snapshot payload
is accepted by both `sh -n` and BusyBox `ash -n`; and the token tool mints nothing without
`--yes`.
