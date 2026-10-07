#!/usr/bin/env bash
# extensive-test — one command for the TollGate extensive-test effort.
#
# Wraps venue bring-up (optional), tiered pytest runs, and artifact
# collection into a single entry point. Behavior maps live in
# docs/user-journeys.md (persona journeys × happy/failure/regression)
# and docs/user-stories.generated.md (story × test map — regenerate
# with scripts/story-coverage.py).
#
# Usage:
#   ./scripts/extensive-test.sh                     # virtual venue, full sweep
#   TOLLGATE_VENUE=physical ./scripts/extensive-test.sh
#   TIERS="smoke critical" ./scripts/extensive-test.sh
#   STORIES="G2 G4" ./scripts/extensive-test.sh     # journey-scoped (OR list)
#   PATHS="failure regression" ./scripts/extensive-test.sh
#
# Env:
#   TOLLGATE_VENUE   virtual (default) | physical | skip
#                    virtual: refuses politely if another lane drives the lab
#                    physical: assumes TOLLGATE_SSH_HOST already deployed
#   TOLLGATE_IPK     optional .ipk to deploy first (physical venue)
#   TIERS            pytest -m tier selector list (default: smoke critical extended)
#   STORIES          pytest --story selector list (optional)
#   PATHS            -m path-class filter: happy|failure|regression (optional)
#   BRINGUP=0        skip venue bring-up entirely (assume it is up)
set -uo pipefail

REPO_ROOT=$(git -C "$(dirname -- "$0")" rev-parse --show-toplevel)
cd "$REPO_ROOT"

VENUE="${TOLLGATE_VENUE:-virtual}"
TIERS="${TIERS:-smoke critical extended}"
STORIES="${STORIES:-}"
PATHS="${PATHS:-}"
PY="${TOLLGATE_PYTHON:-$HOME/.tollgate-test-venv/bin/python}"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="results/extensive-${STAMP}"
mkdir -p "$OUT/raw"

fail() { echo "extensive-test: $*" >&2; exit 1; }

tier_expr() {
  # tiers are hierarchical subsets: select the union
  echo "$TIERS"
}

# ---------------------------------------------------------------- venue ----
if [ "${BRINGUP:-1}" = "1" ] && [ "$VENUE" = "virtual" ]; then
  echo "== venue: virtual lab"
  if ssh -o BatchMode=yes -o ConnectTimeout=6 ai-legion \
      'pgrep -af "dnsmasq.*omarchy-tb|socat.*7301" >/dev/null 2>&1'; then
    fail "another lane is driving the ai-legion lab (omarchy/socat markers present) — coordinate or use TOLLGATE_VENUE=physical|skip"
  fi
  RUNNING=$(ssh -o BatchMode=yes ai-legion 'pgrep -c qemu-system' 2>/dev/null || echo 0)
  if [ "${RUNNING:-0}" -lt 2 ]; then
    echo "== lab down, starting (start-poc)…"
    ssh -o BatchMode=yes ai-legion \
      'sudo bash -c "cd /root/src/physical-router-test-automation && HOME=/root python3 scripts/virtual-lab.py start-poc --host localhost"' \
      | tee "$OUT/bringup.log" | tail -3 \
      || fail "lab bring-up failed — see $OUT/bringup.log (issue #21 tracks known instability)"
  fi
elif [ "$VENUE" = "physical" ]; then
  echo "== venue: physical (${TOLLGATE_SSH_HOST:-TOLLGATE_SSH_HOST unset!})"
  [ -n "${TOLLGATE_SSH_HOST:-}" ] || fail "physical venue needs TOLLGATE_SSH_HOST"
  if [ -n "${TOLLGATE_IPK:-}" ]; then
    echo "== deploying $TOLLGATE_IPK"
    scp -O "$TOLLGATE_IPK" "root@${TOLLGATE_SSH_HOST}:/tmp/tollgate.ipk" \
      && ssh "root@${TOLLGATE_SSH_HOST}" \
        'opkg install --force-overwrite /tmp/tollgate.ipk; rm -f /etc/tollgate/wallet.db; /etc/init.d/tollgate-wrt restart' \
      | tee "$OUT/deploy.log" | tail -2
  fi
fi

# ----------------------------------------------------------------- runs ----
select_args=()
[ -n "$STORIES" ] && select_args+=(--story "$(echo "$STORIES" | tr ' ' ',')")
[ -n "${TIPS:-}" ] && select_args+=(--tip "$(echo "$TIPS" | tr ' ' ',')")
PATH_EXPR=""
[ -n "$PATHS" ] && PATH_EXPR=" and ($(echo "$PATHS" | tr ' ' ' or '))"

run_tier() {
  local tier="$1"
  echo "== tier: $tier ${STORIES:+stories=$STORIES }${PATHS:+paths=$PATHS}"
  "$PY" -m pytest tests/ -m "${tier}${PATH_EXPR}" \
    "${select_args[@]}" \
    -q --tb=short \
    --junitxml="$OUT/raw/junit-${tier}.xml" \
    2>&1 | tee "$OUT/raw/output-${tier}.log" | tail -2
  # keep going: one tier failing must not hide the others; verdict at the end
}

RC=0
for tier in $TIERS; do run_tier "$tier" || RC=1; done

# ------------------------------------------------------------- artifacts ----
"$PY" scripts/story-coverage.py >/dev/null && cp docs/user-stories.generated.md "$OUT/"
cp docs/user-journeys.md "$OUT/" 2>/dev/null || true

PASS=$(grep -ho 'passed' "$OUT"/raw/output-*.log | wc -l)
echo
echo "== done → $OUT"
echo "   story map + journey map copied; junit + logs under raw/"
echo "   verdict: $([ $RC -eq 0 ] && echo GREEN || echo 'RED (see per-tier logs)')"
exit $RC
