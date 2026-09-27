# Demo recording — synchronized player + video from test runs

`scripts/record-demo.py` turns a TollGate test/demo run into something you
can **watch**: a scrubbable HTML player (and optional `.webm` video) where
the laptop terminal, the router's service log, the status-bar state, and
payment events are synchronized to one wall clock.

This is the recording lane for the laptop client
([tollgate-clientd](tollgate-clientd.md)); the same recorder works for any
`--clientd-cmd`, including portal flows via Playwright video spliced in
later.

## What you get

```
evidence/<date>-<slug>/
  events.json   raw timeline: streams (terminal, router), status snapshots, events
  player.html   self-contained player — open in any browser, no server needed
  demo.webm     video export of the player (--export-video)
```

The player: play/pause (space), 0.5–4× speed, scrub, event dots on the
timeline (payments = green, failures = red, notices = yellow). Panes: the
client terminal, the router log, the waybar-style status pill (critical →
warning while topping up → good), and the event feed.

## Running against the module repo's cloud lab

Prereqs: docker; the lab images built once from
`tollgate-module-basic-go/tests/cloud-lab` (`docker compose up -d mint
upstream` there); a funded wallet directory.

```bash
LAB=../tollgate-module-basic-go/tests/cloud-lab/docker-compose.yml
WALLET=/tmp/demo-wallet

# fund a wallet from the lab mint (FakeWallet — no real sats)
docker compose -f $LAB run --rm -v $WALLET:/w --entrypoint cdk-cli client \
    -w /w mint http://mint:8085 100

python3 scripts/record-demo.py \
    --clientd-cmd "docker compose -f $LAB run --rm --entrypoint python3 \
        -v $WALLET:/w client python3 /clientd/tollgate-clientd.py \
        --gateway upstream --mac 02:00:00:00:00:20 --wallet cdk-cli \
        --wallet-dir /w --steps 1 --renew-below 45s --interval 1" \
    --log-source docker:tg-upstream \
    --duration 45 --out evidence/$(date +%F)-clientd-demo \
    --title "clientd auto-top-up (cloud lab)" \
    --export-video --export-speed 2
```

Restart the upstream container between recordings — sessions are in-memory
per client MAC (`docker compose -f $LAB restart upstream`).

If two labs run at once they collide on the compose project name and fixed
subnet; give the recording lab its own project (`-p prta-demo`) and
subnet, or stop the other one first.

## Log sources

| `--log-source` | Collects |
|---|---|
| `docker:<container>` | `docker logs -f --timestamps` (true log timestamps) |
| `cmd:<shell cmd>` | any tailed command — e.g. `cmd:ssh router logread -f` for the physical/VM lanes |
| `file:<path>` | `tail -f` on a file |
| `none` | no router pane |

## Video export

`--export-video` plays the player headlessly in Chromium at
`--export-speed` (default 2×) and saves `demo.webm`. Requires
`pip install playwright && playwright install chromium`.

## How synchronization works

- Every terminal line is stamped on arrival; docker log lines use their
  own RFC3339 timestamps (no clock-skew between panes).
- Status snapshots and events are parsed from the clientd output
  (`-> paid`, `!! top-up failed`, countdown lines, `[topping up...]`).
- The player renders all streams by `t = epoch − start`, so scrubbing shows
  exactly what the router was logging at the moment the client paid.

## Regression lane

`tests/unit/test_record_demo.py` pins the parsers, event deduper, manifest
shape, and player rendering — no docker, no hardware. Recorded evidence
from the cloud lab lives in `evidence/2026-09-19-clientd-demo-player/`
(2 payments incl. threshold renewal, 194 router log lines, 27s export).
Media files (`*.webm`, `*.png`) are gitignored by repo policy — only
`events.json` and the self-contained `player.html` are committed;
re-record or `--export-video` to regenerate media locally.
