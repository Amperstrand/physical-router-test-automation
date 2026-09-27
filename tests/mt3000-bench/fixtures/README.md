# fixtures — what the two binaries actually logged (verbatim)

These files feed the `pre17_forced_restart` and `fix_forced_restart` directions of
`tests/mt3000-bench/zombie-settle-control.sh`. Each directory holds the two windows the settle
phase reads:

| file | what the run's readers return |
|---|---|
| `whole.log`    | the whole `logread` ring buffer (`router_log_grep`) |
| `anchored.log` | only the lines after the phase's `LOG-ANCHOR` marker (`router_log_since`) |

Every line is taken VERBATIM from a capture, ANSI escapes included — that is part of the point:
the bench's logrus fields arrive as `unconfirmed_closes\x1b[0m=2134`, so a reader without the
run's `strip_ansi` parses **0** for ever, and a counter assertion that always reads 0 is a
false-PASS generator. The control pipes these files through the run's own `strip_ansi`.

## `pre17_forced_restart/` — the NEGATIVE control (must fire)

Provenance: `/home/c03rad0r/tg-e2e/zombie-forced/run.log`, the BEFORE stage of the forced-drift
capture on the bench MT3000, 2026-09-26, `tollgate-wrt-0.6.0_alpha4_pre17-r1`
(binary sha256 prefix `f3211b42771c9c4f`).

The two escalation lines are the same client (`02:11:22:33:77:0c`) at the two times the capture
sampled, 14:38:57 and 14:41:39: the same "Gate close NOT confirmed" for an address nodogsplash no
longer knows, with the running total climbing from `unconfirmed_closes=1711` to `2136`. In a live
buffer those are consecutive samples of one endless loop; here they are concatenated, because the
control replays a SEQUENCE and the two counter samples must be able to differ. The two
`unreadable for N sweeps` ERROR lines are the same client's neighbours from the same capture, also
verbatim, and stand for the noise a real window carries.

There is NO `Client already gone` / `nothing left to deauthorize` line anywhere in this fixture,
and that is the measured truth about pre17: the fix's positive statement does not exist in that
binary's output.

## `fix_forced_restart/` — the POSITIVE control (must not fire)

Provenance: `/home/c03rad0r/tg-manual/zombie-GREEN-merchant.log` (lines 42-43 and 54-55 of the
module's own captured test output on the fix branch, module PR #595 `pr/zombie-session-retire`),
client `aa:bb:cc:dd:ee:60`.

The INFO line is the fix's own wording from `src/valve/valve.go`: the gate is closed BY DEFINITION
for a MAC nodogsplash does not know, there is nothing left to deauthorize, and no retry is armed.
The second line is the reconciliation that follows it.

**Honest limit.** This is the fix's code path as captured by the module's own test run, not a bench
run of the fix. The bench's 2026-09-26 AFTER stage never got a quote at all: the module had just
been swapped and restarted, so the purchase went into the 20-40 s window in which its HTTP API is
dead (`POST /ln-invoice` answered without a quote), no PAID session existed to drift, and the
"no new loop lines in 240 s" that followed cannot be read as absence of a loop (that count came from
the whole rotating ring buffer). So the fix direction here proves the ASSERTIONS accept the fix's own
statement; it does not prove the fix on the bench. A bench run of
`scripts/mt3000-bench/second-purchase-e2e.sh --purchase --lane ln` with the fix binary installed is
what does that — and PHASE 5b's assertions are written so one such run is enough.
