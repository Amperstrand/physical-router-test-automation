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
`SETTLE_BUDGET`, `SETTLE_WINDOW`, `FORCE_DRIFT`, `CLEAN_PAIR_EPILOGUE`, …). The script's
`--help` is its own header and is the authoritative list. No path, NIC or IP
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
| 5b | **force the drift** — a DELIBERATE, attributable nodogsplash restart while the PAID allotment of buy#2 is open | the module states `Client already gone … nothing left to deauthorize` (naming the MAC), `unconfirmed_closes` does not move, nodogsplash still does not know the MAC, no `Socket is not ready for communication`, ndsctl answers |

Exit codes: `0` every assertion held · `2` usage/preflight · `3` bench held by another window ·
`4` not in a bench window · `5` stale holder line (explicit reclaim only) · `10` buy #1 never
opened the gate (setup problem) · `11` allotment never exhausted inside the budget
(**inconclusive, never a pass**) · `12` buy #2 did not re-open the gate (bug reproduced) ·
`13` an assertion failed · `14` tokens are not spendable · `15` the box restarted under the run
(INVALID — rerun it).

## PHASE 5b — the close has to be FORCED

A client that just **leaves nodogsplash** (`ndsctl deauth`) with its paid allotment still open is
**not** a reproduction: measured on this bench on 2026-09-26, the module holds the session and does
nothing at all (`N_DEAUTH=0 N_UNCONF=0` in the window). The failing deauth only happens after
something TRIGGERS a close. The deterministic trigger is a **deliberate restart of nodogsplash**:
NDS comes back not knowing the client, the module's sweeps find the session's counters unreadable
and must close the gate, and `ndsctl deauth <mac>` answers `Client <mac> not found.` rc=1. On
pre17 the close loop started within 30 s (`unconfirmed_closes` reached 2141); with the fix binary
the same scenario produced no new loop lines in 240 s.

PHASE 5b therefore does three things the older lane did not:

1. **Forces** it (`FORCE_DRIFT=restart`, the default; `FORCE_DRIFT=deauth` keeps the old
   ndsctl-only step for the ablation, and says in the transcript that a settle after it proves
   nothing).
2. **Attributes** it: the step writes a `BENCH ACTION` line into the ROUTER's own log before it
   restarts, prints the nodogsplash pid change, and the box guard fires (exit 15) if
   **tollgate-wrt** moved with it — a module restart there is a product self-restart, not our step.
   The baseline is re-pinned afterwards, so the phase's end check compares against the
   post-restart box. An unattributed restart once invalidated a sibling's run and was mistaken for
   a product self-restart; that must not be possible again.
3. **Asserts a STATE CHANGE, not an absence of errors**: the module's own positive line, the
   counter not growing, nodogsplash still not knowing the MAC (read from the box), and the ndsctl
   socket still answering. A ring buffer that rotates the error lines out must not be able to look
   like convergence — that is exactly why the 2026-09-26 AFTER capture could not prove the fix in
   one run.

After the phase, `CLEAN_PAIR_EPILOGUE=1` (default) bounces nodogsplash + tollgate-wrt and waits for
the API, because a pre-fix build is left with the close loop running and the next purchase cannot be
authorised in that state. `CLEAN_PAIR_EPILOGUE=0` leaves the box exactly as the phase left it.

**Negative control (no bench needed):** `tests/mt3000-bench/zombie-settle-control.sh` drives the
extracted PHASE 5b with the lines the two binaries actually logged — the measured pre17 window
(which MUST fire) and the fix's own INFO line (which must not) — plus every synthetic direction
(never settles, counter climbs, escalation names the client, unmetered claim, wedged socket, no
positive statement, client re-appears, the step forced nothing). Provenance:
`tests/mt3000-bench/fixtures/README.md`.
**Negative control (on the bench):** the pre17 build IS the offending build, so run the lane on a
box carrying it and PHASE 5b must fail (`RESULT: assertions failed (n)`, exit 13).

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

## The forcing step — measured 2026-09-26 (module PR #595's before/after)

Raw capture: `~/tg-e2e/zombie-forced/run.log` on c03rad0r (BEFORE = installed pre17
`f3211b42771c9c4f`, AFTER = the fix binary `254da4c28da22144`, swapped in by hand with a sha256
check and restored afterwards).

* **BEFORE (pre17)**: one LN step on a fresh MAC through the client's own socket
  (`GRANTED … allotment 22020096`), then the deliberate nodogsplash restart. Inside a 240 s window
  the module was in the close loop: `Gate close NOT confirmed for client … exit status 1` repeating
  with `unconfirmed_closes` at 1711 at 14:38:57 and 2136 by 14:41:39, plus
  `the usage of the bytes session of … has been unreadable for 122/145 sweeps (… not found in
  ndsctl)` — the failing deauth the defect needs. **That is the negative control, in one run.**
* **AFTER (the fix binary)**: the swap verified (on-device hash 254da4c2…) and the box restored to
  pre17 at the end — but the AFTER stage itself is **not evidence**: the purchase came back with no
  quote at all (`POST /ln-invoice` answered nothing) because it ran ~5 s after the module restart,
  inside the 20-40 s window in which the module's HTTP API is dead. No PAID session existed to
  drift, and the "no new loop lines in 240 s" that followed was a count over the whole rotating ring
  buffer, which cannot show absence. The fix's positive INFO line did not survive in the buffer
  either. This is exactly the gap PHASE 5b's assertions close: one run of the lane on the fix binary
  now produces the state-change evidence or fails.

**Honest summary:** the reproducer is confirmed end to end on pre17; the fix is confirmed by the
module's own test suite, and on the bench it is *not yet* — a lane run with the fix binary installed
is what settles it.

## What this lane does NOT cover

* the portal **Lightning-invoice** purchase lane (only the cashu-token POST is exercised);
* a **one-step** allotment (this lane uses a 63-step ~1.3 GB allotment);
* a **Wi-Fi** client (a macvlan cannot present a second MAC through an AP association);
* the second purchase as a *state* problem across a **router reboot** (PHASE 5b covers the
  nodogsplash-restart variant, not a full reboot — a reboot is exit 15 by design).

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
9. **After PHASE 5b's forcing step the client must stay SILENT.** Any packet FROM the client
   re-creates its nodogsplash record within a sweep or two (`Adding <ip> <mac> … to client list`)
   and the drift dissolves — the phase would then be measuring nothing. That is why PHASE 5b reads
   its evidence from the box and from the module's log and **never** calls `probe()`, `balance()` or
   `egress()` once the restart has happened, and why it asserts the precondition (nodogsplash holds
   no record for the MAC) before it asserts anything else.
10. **The `unconfirmed_closes` field is COLOURED in the ring buffer.** tollgate-wrt's logrus lines
    arrive as `unconfirmed_closes\x1b[0m=2134`, so a pattern or sed containing `unconfirmed_closes=`
    matches **nothing** — on the 2026-09-26 capture it read 0 while the total was over 2100. The
    lane normalises every log window with `strip_ansi` before it parses; a router-side `grep`
    pattern must therefore never depend on the `=`.

## Verifying the lane itself (no router)

```sh
bash -n scripts/mt3000-bench/second-purchase-e2e.sh scripts/mt3000-bench/router-snapshot.sh
shellcheck -s bash -S warning scripts/mt3000-bench/*.sh
make bench-tests                     # 24 offline negative controls, including this lane's
python3 -m pytest tests/unit/test_recover_tokens.py
```

`make bench-tests` proves, without a router: the e2e is dry-run by default; a paid run without
tokens is refused (exit 2); a paid run **is refused while another window owns the bench**
(exit 3, holder named, no router probe, no transcript directory created); the snapshot payload
is accepted by both `sh -n` and BusyBox `ash -n`; the token tool mints nothing without
`--yes`; and the settle phase's own control (`tests/mt3000-bench/zombie-settle-control.sh`) fires
in every documented direction — including the **measured pre17 window** and the **fix's own log
line**, replayed verbatim from `tests/mt3000-bench/fixtures/` — while the forcing step it drives is
checked for its `BENCH ACTION` attribution and for parsing under the router's shell.
