#!/usr/bin/env python3
"""signet-faucet — auto-pay small invoices on a signet CLN node.

Design (agreed 2026-09-17, signet only — signet funds are worthless, this
turns the node into a faucet for Cashu mints that settle invoices through it):

- An unpaid invoice under MAX_SATS (1000) becomes eligible when it has been
  observed for at least (its amount in sats) seconds. A 60-sat invoice paid
  by the next minute tick; a 500-sat invoice waits 500 s.
- Eligible invoices are paid through a rate-limited rune: method=pay only,
  bolt11 amount < 1000 sats (pinvbolt11_amount), at most 3 pays/minute.
- The script additionally enforces a hard sat-sum budget of
  MAX_SATS_PER_HOUR (10000) tracked in its state file — runes cannot express
  sat-sum-per-window (ElementsProject/lightning#7020), so the cap is layered.
- Invoices that fail payment are retried a few times then abandoned, so a
  permanently unroutable invoice cannot wedge the faucet.

Runs on the signet server via /etc/cron.d (once a minute, flock-guarded).
State:  <state_dir>/state.json   Rune: <state_dir>/rune.txt (mode 600)
Logs to stdout (cron redirects to /var/log/signet-faucet.log).
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CONTAINER = "cln-hub-signet"
NETWORK = "signet"
CLNREST_URL = "http://172.20.0.12:3010"
STATE_DIR = Path("/root/signet-faucet")
RUNE_FILE = STATE_DIR / "rune.txt"
STATE_FILE = STATE_DIR / "state.json"
MAX_SATS = 1000
MAX_SATS_PER_HOUR = 10000
MAX_ATTEMPTS = 5
DRY_RUN = "--dry-run" in sys.argv


def cli(*args: str) -> dict | list | None:
    proc = subprocess.run(
        ["docker", "exec", CONTAINER, "lightning-cli", f"--network={NETWORK}", *args],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"lightning-cli {args[0]}: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def clnrest_pay(bolt11: str, rune: str) -> dict:
    """Pay through CLNRest authenticated by the rate-limited rune, so the
    pay-only / <1000 sat / rate caps hold even if this script is buggy."""
    req = urllib.request.Request(
        f"{CLNREST_URL}/v1/pay",
        data=json.dumps({"bolt11": bolt11}).encode(),
        headers={"Rune": rune, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read() or b"{}")


def log(msg: str) -> None:
    print(f"[{time.strftime('%FT%TZ', time.gmtime())}] {msg}", flush=True)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_FILE)


def hour_budget(state: dict) -> int:
    now = time.gmtime()
    key = f"{now.tm_year}{now.tm_mon:02d}{now.tm_mday:02d}{now.tm_hour:02d}"
    spend = state.setdefault("hourly", {})
    for k in [k for k in spend if k < key]:
        del spend[k]
    return int(spend.get(key, 0))


def main() -> int:
    rune = RUNE_FILE.read_text().strip()
    state = load_state()
    seen: dict = state.setdefault("seen", {})
    attempts: dict = state.setdefault("attempts", {})

    invoices = cli("listinvoices")["invoices"]
    unpaid = [i for i in invoices if i.get("status") == "unpaid"]
    now = int(time.time())

    live_hashes = set()
    eligible = []
    for inv in unpaid:
        h = inv["payment_hash"]
        live_hashes.add(h)
        msat = inv.get("amount_msat")
        if not isinstance(msat, int) or msat <= 0:
            continue
        sats = msat // 1000
        if sats >= MAX_SATS:
            continue
        if int(inv.get("expires_at", 0)) <= now:
            continue
        first_seen = seen.setdefault(h, now)
        age = now - first_seen
        if age >= sats and attempts.get(h, 0) < MAX_ATTEMPTS:
            eligible.append((sats, h, inv["bolt11"], inv.get("label", "?")))

    for h in [h for h in list(seen) if h not in live_hashes]:
        del seen[h]
        attempts.pop(h, None)

    if not eligible:
        log(f"no eligible invoices ({len(unpaid)} unpaid observed)")
        save_state(state)
        return 0

    eligible.sort()
    spent = hour_budget(state)
    now_key = f"{time.gmtime().tm_year}{time.gmtime().tm_mon:02d}{time.gmtime().tm_mday:02d}{time.gmtime().tm_hour:02d}"
    for sats, h, bolt11, label in eligible:
        if spent + sats > MAX_SATS_PER_HOUR:
            log(f"hourly budget exhausted ({spent}/{MAX_SATS_PER_HOUR} sats); skipping {sats}sat {label}")
            break
        if DRY_RUN:
            log(f"DRY-RUN would pay {sats} sats to {label} ({h[:12]}…)")
            continue
        log(f"paying {sats} sats to {label} ({h[:12]}…)")
        try:
            result = clnrest_pay(bolt11, rune)
            log(f"  paid: payment_hash={result.get('payment_hash', h)[:12]}… "
                f"fee={result.get('amount_sent_msat', 0) - sats * 1000}msat")
            spent += sats
            state["hourly"][now_key] = spent
            attempts.pop(h, None)
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:160]
            attempts[h] = attempts.get(h, 0) + 1
            log(f"  pay refused/failed (attempt {attempts[h]}/{MAX_ATTEMPTS}): {body}")
        except RuntimeError | OSError | ValueError as e:
            attempts[h] = attempts.get(h, 0) + 1
            log(f"  pay failed (attempt {attempts[h]}/{MAX_ATTEMPTS}): {str(e)[:160]}")
        save_state(state)
    save_state(state)
    return 0


if __name__ == "__main__":
    LOCK = open("/run/signet-faucet.lockfile", "w")
    try:
        fcntl.flock(LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("previous faucet run still active; exiting", flush=True)
        sys.exit(0)
    sys.exit(main())
