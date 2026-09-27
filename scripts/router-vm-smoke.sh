#!/usr/bin/env bash
# router-vm-smoke.sh — run a chosen TollGate suite against the ephemeral local
# QEMU lab (OpenWrt + Debian client + fakewallet mint).
#
# Invoked by CI *inside the lab VM* (isolation boundary). Everything is
# selectable at run time via env:
#   BACKEND   go | go-cdk | rust-basic | rust        (default: go)
#   TIER      smoke | critical | extended | all      (default: smoke)
#   PUBLISH   true | false                           (default: false)
#
# See LOCAL-VM-TESTING.md for the underlying venue.
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
mkdir -p results

cleanup() { python3 scripts/virtual-lab.py stop-poc --host localhost || true; }
trap cleanup EXIT

python3 scripts/virtual-lab.py start-poc --host localhost --backend "$BACKEND" \
  || python3 scripts/virtual-lab.py start-poc --host localhost

if [ -n "$MARK" ]; then
  python3 -m pytest -m "$MARK" tests/api -q --junitxml results/junit.xml
else
  python3 -m pytest tests/api -q --junitxml results/junit.xml
fi

if [ "$PUBLISH" = "true" ]; then
  ./scripts/publish-report.sh || true
fi
