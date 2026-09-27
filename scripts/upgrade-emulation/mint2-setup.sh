#!/bin/bash
# mint2-setup.sh — second Cashu mint for multi-mint upgrade tests (CDK 0.18.0 fakewallet)
#
# Runs on ai-legion-small using /opt/cdk-mintd (cdk-mintd 0.18.0 + cdk-cli 0.18.0).
# Binds 0.0.0.0:8383, mint URL http://10.99.99.2:8383/ — reachable from BOTH the
# bench VM (10.99.99.1 via NAT gateway) and the host.
#
# CDK 0.18.0-final gotcha (differs from AGENTS.md 0.18-rc recipe):
#   - `cdk-mintd --config` is GONE as a startup input. Config lives in the DB:
#     `config validate` + `config init --new-mint --file config.toml` once, then
#     start BARE `cdk-mintd` with CDK_MINTD_WORK_DIR set.
#   - secrets as refs: mnemonic = "env:CDK_MINTD_MNEMONIC" (plaintext rejected).
set -e
MINT=~/upgrade-test/mint2
BIN=/opt/cdk-mintd/cdk-mintd
mkdir -p "$MINT"; cd "$MINT"

cat > config.toml << 'EOF'
[info]
url = "http://10.99.99.2:8383/"
listen_host = "0.0.0.0"
listen_port = 8383
mnemonic = "env:CDK_MINTD_MNEMONIC"

[database]
engine = "sqlite"

[payment_backend]
backend = "fakewallet"

[fake_wallet]
supported_units = ["sat"]
fee_percent = 0
reserve_fee_min = 0
min_delay_time = 0
max_delay_time = 0
EOF

export CDK_MINTD_MNEMONIC="abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
export CDK_MINTD_WORK_DIR="$MINT"

if [ ! -f "$MINT/mint.db" ]; then
  $BIN config validate --file config.toml
  $BIN config init --new-mint --file config.toml
fi

pgrep -f "cdk-mintd$" >/dev/null || setsid nohup $BIN > $MINT/mintd.log 2>&1 < /dev/null &
sleep 4

# Health: /v1/info + settle probe (1-sat quote must reach PAID — AGENTS.md lesson:
# a wedged mint answers /v1/info but never settles quotes).
curl -s -m 5 http://127.0.0.1:8383/v1/info | head -c 120; echo
QUOTE=$(curl -s -m 5 -X POST http://127.0.0.1:8383/v1/mint/quote/bolt11 -H 'Content-Type: application/json' -d '{"unit":"sat","amount":1}')
QID=$(echo "$QUOTE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["quote"])')
for i in $(seq 1 12); do
  STATE=$(curl -s -m 5 http://127.0.0.1:8383/v1/mint/quote/bolt11/$QID | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))')
  [ "$STATE" = "PAID" ] && { echo "settle-probe: PAID after ${i}s"; exit 0; }
  sleep 1
done
echo "settle-probe FAILED state=$STATE"; tail -5 "$MINT/mintd.log"; exit 1
