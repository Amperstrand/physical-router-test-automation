#!/usr/bin/env bash
# router-vm-smoke.sh — run a chosen TollGate suite against the ephemeral local
# QEMU lab (OpenWrt + Debian client + fakewallet mint).
#
# Invoked by CI *inside the lab host*. Everything is selectable at run time:
#   BACKEND   go | go-cdk | rust-basic | rust        (default: go)
#   TIER      smoke | critical | extended | all      (default: smoke)
#   PUBLISH   true | false                           (default: false)
#   TOLLGATE_BRANCH  branch/ref whose CI .ipk to deploy (default: main)
#   TOLLGATE_DEPLOY  1 to deploy the backend via the conftest (default: 1)
set -euo pipefail

BACKEND="${BACKEND:-go}"
TIER="${TIER:-smoke}"
PUBLISH="${PUBLISH:-false}"
REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$REPO"

case "$TIER" in
  smoke)    MARK=smoke ;;
  critical) MARK=critical ;;
  extended) MARK=extended ;;
  all)      MARK="" ;;
  *) echo "router-vm-smoke: bad TIER '$TIER'"; exit 2 ;;
esac

# shellcheck disable=SC1090
source "$HOME/.tollgate-test-venv/bin/activate" 2>/dev/null || true
export PATH="$HOME/.local/bin:$PATH"
mkdir -p results

RPC_PID=""
cleanup() {
  [ -n "$RPC_PID" ] && kill "$RPC_PID" 2>/dev/null || true
  python3 scripts/virtual-lab.py stop-poc --host localhost || true
}
trap cleanup EXIT

python3 scripts/virtual-lab.py start-poc --host localhost

# npub-gated issuance RPC (deliberate auth bypass). Whitelisted npubs may mark a
# quote paid / receive test e-cash; everyone else is rejected (NIP-98 proof).
if [ -f scripts/mint_allowlist_rpc.py ]; then
  MINT_ALLOWLIST_CONFIG="${MINT_ALLOWLIST_CONFIG:-configs/mint_allowlist.example.json}" \
  MINT_ALLOWLIST_PORT="${MINT_ALLOWLIST_PORT:-8391}" \
  MINT_ADMIN_URL="${MINT_ADMIN_URL:-}" \
    python3 scripts/mint_allowlist_rpc.py >/tmp/mint-allowlist-rpc.log 2>&1 &
  RPC_PID=$!
fi

# Router/backend env for pytest (mirrors scripts/run-local-tests.sh).
PASSWORD="${TOLLGATE_VIRTUAL_LAB_PASSWORD:-$(jq -r .password credentials/virtual-lab-credentials.json 2>/dev/null || echo tollgate)}"
export TOLLGATE_SSH_HOST="${TOLLGATE_SSH_HOST:-10.99.99.1}"
export TOLLGATE_SSH_PASSWORD="$PASSWORD"
export TOLLGATE_LUCI_PASSWORD="$PASSWORD"
export TOLLGATE_TEST_MINT_URL="${TOLLGATE_TEST_MINT_URL:-http://10.99.99.2:8383}"
export TOLLGATE_CLIENT_IP="${TOLLGATE_CLIENT_IP:-10.99.99.100}"
export TOLLGATE_CLIENT_MAC="${TOLLGATE_CLIENT_MAC:-de:54:4e:91:49:da}"
export TOLLGATE_VIRTUAL_LAB=1
export TOLLGATE_BACKEND="$BACKEND"
export TOLLGATE_ROUTER_ARCH="${TOLLGATE_ROUTER_ARCH:-x86_64}"

# Backend deploy (conftest deploy_session). Explicit and reproducible:
#   TOLLGATE_BINARY=<path>   deploy a locally built tollgate-wrt binary
#   TOLLGATE_BRANCH=<ref>    deploy the CI .ipk for that ref (downloads artifact)
#   (neither set)            backend is expected pre-deployed (no deploy step)
DEPLOY_ARGS=()
if [ -n "${TOLLGATE_BINARY:-}" ]; then
  DEPLOY_ARGS=(--binary "$TOLLGATE_BINARY")
elif [ -n "${TOLLGATE_BRANCH:-}" ]; then
  DEPLOY_ARGS=(--tollgate-branch "$TOLLGATE_BRANCH" --tollgate-arch "$TOLLGATE_ROUTER_ARCH")
fi

if [ -n "$MARK" ]; then
  python3 -m pytest -m "$MARK" tests/api -q --junitxml results/junit.xml "${DEPLOY_ARGS[@]}"
else
  python3 -m pytest tests/api -q --junitxml results/junit.xml "${DEPLOY_ARGS[@]}"
fi

if [ "$PUBLISH" = "true" ]; then
  ./scripts/publish-report.sh || true
fi
