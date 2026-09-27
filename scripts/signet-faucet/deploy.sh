#!/usr/bin/env bash
# Deploys the signet faucet on inr2.cashu.exchange (signet CLN only).
# Idempotent; run via: ssh root@inr2 'bash -s' < deploy.sh
set -euo pipefail

DIR=/root/signet-faucet
CONTAINER=cln-hub-signet
mkdir -p "$DIR"

# 1. Rate-limited rune: pay-only, bolt11 amount < 1000 sats, <= 3 pays/minute.
#    (Runes cannot express sat-sum-per-window; the script enforces 10k/hr.)
if [ ! -s "$DIR/rune.txt" ]; then
  docker exec "$CONTAINER" lightning-cli --network=signet createrune null \
    '[["method=pay"],["method/pay|pinvbolt11_amount<1000001"],["method/pay|rate=3"]]' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["rune"])' > "$DIR/rune.txt"
  chmod 600 "$DIR/rune.txt"
  echo "rune created"
fi
RUNE=$(cat "$DIR/rune.txt")
docker exec "$CONTAINER" lightning-cli --network=signet showrunes "$RUNE" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin)["runes"][0]; print("rune restrictions:", " AND ".join(x["english"] for x in r["restrictions"]))'

# 2. Install the faucet script (piped in by the caller after this file).
cat > "$DIR/faucet.py"

# 3. Cron: once a minute, flock-guarded, logged.
cat > /etc/cron.d/signet-faucet <<'CRON'
* * * * * root flock -n /run/signet-faucet.lock /usr/bin/python3 /root/signet-faucet/faucet.py >> /var/log/signet-faucet.log 2>&1
CRON
chmod 644 /etc/cron.d/signet-faucet

# 4. Self-test: create a 5-sat invoice on the hub, then dry-run the faucet.
INV=$(docker exec "$CONTAINER" lightning-cli --network=signet invoice 5000msat "faucet-selftest-$(date +%s)" "signet faucet selftest" 3600 | python3 -c 'import json,sys; print(json.load(sys.stdin)["bolt11"])')
echo "selftest invoice: ${INV:0:40}…"
echo "--- dry run ---"
/usr/bin/python3 "$DIR/faucet.py" --dry-run
echo "--- live run (invoice is 5 sats; eligible after 5 s of observation) ---"
sleep 6
/usr/bin/python3 "$DIR/faucet.py"
echo "--- log tail ---"
tail -5 /var/log/signet-faucet.log 2>/dev/null || true
echo "DEPLOY-DONE (cron active; watch /var/log/signet-faucet.log)"
