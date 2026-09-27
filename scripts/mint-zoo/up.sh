#!/bin/bash
# mint-zoo up.sh — version-parameterized Cashu mint fleet for interop testing.
#
# Runs on the bench host (ai-legion-small) with docker. Ports bind 0.0.0.0 so
# the bench router (10.99.99.1 via NAT gw 10.99.99.2) can reach every mint.
# Fleet mirrors the ai-legion mint-battery (PRTA #134): nutshell 16.5→20.3
# + cdk-mintd 0.17.x/0.18.x, all FakeWallet (quotes auto-settle).
#
# Usage: scripts/mint-zoo/up.sh [up|down|status]
set -u
CMD="${1:-up}"

# name|image|port  (nutshell = env config; cdk = version-dialect config)
# Focus: latest + second-latest of each implementation — what real mints run
# (user-directed 2026-09-19; the 0.16-0.19 history fleet was retired).
NUTSHELL_FLEET="
ns-2100|cashubtc/nutshell:0.21.0|33210
ns-2003|cashubtc/nutshell:0.20.3|33203
"
CDK_FLEET="
cdk-0181|cashubtc/mintd:0.18.1|33381
cdk-0180|cashubtc/mintd:0.18.0|33380
cdk-0176|cashubtc/mintd:0.17.6|33376
"
ZOO_DIR="$HOME/mint-zoo"
HOST_IP=10.99.99.2

# per-mint deterministic private key (fakewallet value is disposable; any
# 32-byte hex works and keeps keysets stable across restarts)
mint_key() { printf "%064x" "$1"; }

up_nutshell() {
  local name="$1" image="$2" port="$3" idx="$4"
  docker pull -q "$image" >/dev/null 2>&1 || true
  docker rm -f "zoo-$name" >/dev/null 2>&1 || true
  docker run -d --name "zoo-$name" --restart unless-stopped \
    -e MINT_LISTEN_HOST=0.0.0.0 \
    -e MINT_LISTEN_PORT="$port" \
    -e MINT_RATE_LIMIT=FALSE \
    -e MINT_BACKEND_BOLT11_SAT=FakeWallet \
    -e MINT_PRIVATE_KEY="$(mint_key "$idx")" \
    -p "$HOST_IP:$port:$port" \
    "$image" poetry run mint >/dev/null
}

up_cdk() {
  local name="$1" image="$2" port="$3" idx="$4"
  docker pull -q "$image" >/dev/null 2>&1 || true
  local dir="$ZOO_DIR/$name"
  mkdir -p "$dir"
  # 0.17.x and 0.18.x speak different config dialects (AGENTS: [ln] ln_backend
  # was renamed to [payment_backend] backend in 0.18).
  local backend_section mnemonic_line=""
  case "$name" in
    cdk-018*)
      backend_section='[payment_backend]
backend = "fakewallet"'
      # 0.18-final: secrets must be in-config env: references (plaintext rejected)
      mnemonic_line='mnemonic = "env:CDK_MINTD_MNEMONIC"'
      ;;
  esac
  docker rm -f "zoo-$name" >/dev/null 2>&1 || true
  case "$name" in
    cdk-018*)
      # 0.18-final: config lives in the DB — one-shot init, then bare start.
      cat > "$dir/config.toml" << EOF
[info]
url = "http://$HOST_IP:$port/"
listen_host = "0.0.0.0"
listen_port = $port
$mnemonic_line

[database]
engine = "sqlite"

$backend_section

[fake_wallet]
supported_units = ["sat"]
fee_percent = 0
reserve_fee_min = 0
min_delay_time = 0
max_delay_time = 0
EOF
      docker run --rm -v "$dir:/data" \
        -e CDK_MINTD_MNEMONIC="abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about" \
        "$image" cdk-mintd -w /data config init --new-mint --file /data/config.toml >/dev/null 2>&1 || true
      docker run -d --name "zoo-$name" --restart unless-stopped \
        -v "$dir:/data" \
        -e CDK_MINTD_MNEMONIC="abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about" \
        -p "$HOST_IP:$port:$port" \
        "$image" cdk-mintd -w /data >/dev/null
      ;;
    *)
      # 0.17.x: env-driven config (the file dialect fights LnOneOrMany);
      # mirrors the ai-legion mint-battery containers.
      docker run -d --name "zoo-$name" --restart unless-stopped \
        -e CDK_MINTD_URL="http://$HOST_IP:$port/" \
        -e CDK_MINTD_MINT_NAME="zoo-$name" \
        -e CDK_MINTD_MNEMONIC="abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about" \
        -e CDK_MINTD_LN_BACKEND=FakeWallet \
        -e CDK_MINTD_FAKE_WALLET_SUPPORTED_UNITS=sat \
        -e CDK_MINTD_LISTEN_HOST=0.0.0.0 \
        -e CDK_MINTD_LISTEN_PORT="$port" \
        -p "$HOST_IP:$port:$port" \
        "$image" cdk-mintd >/dev/null
      ;;
  esac
}

case "$CMD" in
  up)
    idx=1
    while IFS='|' read -r name image port; do
      [ -z "$name" ] && continue
      if up_nutshell "$name" "$image" "$port" "$idx"; then echo "up zoo-$name ($image :$port)"; fi
      idx=$((idx+1))
    done <<< "$NUTSHELL_FLEET"
    idx=50
    while IFS='|' read -r name image port; do
      [ -z "$name" ] && continue
      if up_cdk "$name" "$image" "$port" "$idx"; then echo "up zoo-$name ($image :$port)"; fi
      idx=$((idx+1))
    done <<< "$CDK_FLEET"
    ;;
  down)
    docker ps -aq --filter "name=zoo-" | xargs -r docker rm -f >/dev/null 2>&1
    echo "zoo down"
    ;;
  status)
    for spec in $NUTSHELL_FLEET $CDK_FLEET; do
      name="${spec%%|*}"; rest="${spec#*|}"; port="${rest##*|}"
      state=$(docker inspect "zoo-$name" --format "{{.State.Status}}" 2>/dev/null || echo missing)
      health=$(curl -s -m 3 "http://$HOST_IP:$port/v1/info" | head -c 40 2>/dev/null)
      printf "%-12s %-10s %s\n" "$name" "$state" "${health:0:40}"
    done
    ;;
esac
