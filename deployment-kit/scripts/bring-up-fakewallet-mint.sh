#!/usr/bin/env bash
# Bring up a local, deterministic Cashu "FakeWallet" mint for physical-router tests.
#
# Why a local mint? Production mints (and old public testnuts) cannot auto-pay a
# NUT-04 quote, so a paid Lightning / Cashu flow cannot be driven without real
# funds. cdk-mintd's `fakewallet` backend auto-settles quotes, which lets the
# TollGate backend mint tokens and grant access end-to-end.
#
# IMPORTANT: the TollGate Go backend's Cashu wallet (cashubtc/cdk-go) verifies the
# mint quote signature. Mints older than cdk-mintd ~0.17 do not produce a
# signature it accepts, and settlement fails with:
#     ensureLightningAccessGranted failed: Signature missing or invalid
# Pin CDK_VER to a version of the same generation as the backend's cdk-go
# (0.18.0 is verified working against the Go backend built with cdk-go 0.17.3).
#
# Run this on a host that the router can reach over its LAN (e.g. the laptop
# directly cabled to the router, or the LAN gateway box). The mint must bind an
# address reachable from the router, not 127.0.0.1.
#
# Env:
#   CDK_VER   cdk version to run              (default 0.18.0)
#   DIR       install/work dir                (default $HOME/tg-mint018)
#   PORT      listen port                     (default 3338)
#   MINT_HOST reachable host/IP               (default: first non-loopback IPv4)
#   MNEMONIC  fixed mnemonic                  (default: well-known abandon...)
#
# After it starts, point the router at it with:
#   scripts/configure-router-test-mint.sh "http://${MINT_HOST}:${PORT}"
set -euo pipefail

CDK_VER="${CDK_VER:-0.18.0}"
DIR="${DIR:-$HOME/tg-mint018}"
PORT="${PORT:-3338}"
MNEMONIC="${MNEMONIC:-abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about}"

if [ -z "${MINT_HOST:-}" ]; then
  MINT_HOST="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^127\.' | grep -E '^[0-9]+\.' | head -1 || true)"
fi
MINT_HOST="${MINT_HOST:-127.0.0.1}"
URL="http://${MINT_HOST}:${PORT}"

mkdir -p "$DIR"
cd "$DIR"

if [ ! -x ./cdk-mintd ]; then
  echo "[mint] downloading cdk-mintd ${CDK_VER}..."
  wget -q -O cdk-mintd "https://github.com/cashubtc/cdk/releases/download/v${CDK_VER}/cdk-mintd-${CDK_VER}-x86_64"
  chmod +x ./cdk-mintd
fi

cat > config.toml <<EOF
[info]
url = "${URL}/"
listen_host = "0.0.0.0"
listen_port = ${PORT}
mnemonic = "env:CDK_MINTD_MNEMONIC"

[database]
engine = "sqlite"

[payment_backend]
backend = "fakewallet"

[fake_wallet]
fee_percent = 0
reserve_fee_min = 0
min_delay_time = 0
max_delay_time = 0
EOF

export CDK_MINTD_MNEMONIC="$MNEMONIC"

# 0.18 stores config authoritatively in the mint DB; always init from a fresh
# work dir so no stale identity/fakewallet state leaks between runs.
rm -f cdk-mintd.sqlite cdk-mintd.sqlite-shm cdk-mintd.sqlite-wal
./cdk-mintd --work-dir "$DIR" config validate --file "$DIR/config.toml"
./cdk-mintd --work-dir "$DIR" config init --new-mint --file "$DIR/config.toml"

# stop a previous instance started by this script, if any
pkill -f "[c]dk-mintd --work-dir ${DIR}" 2>/dev/null || true
sleep 1

setsid ./cdk-mintd --work-dir "$DIR" </dev/null > "$DIR/cdk-mintd.log" 2>&1 &

for _ in $(seq 1 20); do
  if curl -fsS -m 2 "${URL}/v1/info" >/dev/null 2>&1; then break; fi
  sleep 1
done

echo "[mint] version: $(./cdk-mintd --version 2>&1 | head -1)"
echo "[mint] reachable at: ${URL}"
curl -fsS -m 5 "${URL}/v1/info" | head -c 200 || { echo "[mint] NOT healthy"; tail -20 "$DIR/cdk-mintd.log"; exit 1; }
echo
echo "[mint] OK — now run: scripts/configure-router-test-mint.sh ${URL}"
