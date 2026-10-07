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
  echo "== venue: virtual lab (ai-legion)"
  # Politeness: an ACTIVE omarchy lane (its QEMU, or a serial bridge whose
  # target socket exists) means the lab fabric is contended — yield. An
  # orphaned bridge/dnsmasq (VM gone, socket gone) is a known leftover:
  # warn and proceed (AGENTS 2026-09-28 partial-teardown class).
  if ssh -o BatchMode=yes -o ConnectTimeout=6 ai-legion '
      busy=0
      pgrep -af "qemu.*omarchy-v[m]" | grep -v pgrep >/dev/null && busy=1
      # a bridge with live clients = someone is attached to the poc console
      [ "$(ss -tn 2>/dev/null | grep -c ":7301")" -gt 0 ] && busy=1
      [ "$busy" -eq 1 ]'; then
    fail "another lane is actively driving the ai-legion lab (omarchy VM / live serial bridge) — coordinate or use TOLLGATE_VENUE=physical|skip"
  fi
  ssh -o BatchMode=yes ai-legion \
      'pgrep -f "dnsmasq.*omarchy-tb" >/dev/null' 2>/dev/null \
      && echo "   note: orphaned omarchy dnsmasq present (no VM) — proceeding"
  RUNNING=$(ssh -o BatchMode=yes ai-legion 'pgrep -c qemu-system' 2>/dev/null || echo 0)
  if [ "${RUNNING:-0}" -lt 2 ]; then
    echo "== lab down (${RUNNING} qemu), starting (start-poc)…"
    ssh -o BatchMode=yes ai-legion \
      'sudo bash -c "cd /root/src/physical-router-test-automation && HOME=/root python3 scripts/virtual-lab.py start-poc --host localhost"' \
      2>&1 | tee "$OUT/bringup.log" | tail -3 \
      || fail "lab bring-up failed — see $OUT/bringup.log (issue #21 tracks known instability)"
  fi

wait_for_vm() {
  echo "== waiting for VM ssh (max 150s)…"
  for _ in $(seq 1 30); do
    if ssh -o BatchMode=yes ai-legion \
        'pgrep -f "qemu.*tollgate-po[c]" >/dev/null && timeout 6 bash -c "exec 3<>/dev/tcp/10.99.99.1/22" 2>/dev/null'; then
      return 0
    fi
    sleep 5
  done
  return 1
}

  wait_for_vm || fail "poc VM came up but SSH never answered (see bringup.log; issue #21)"
  # Fresh overlays are password-only and the baked password has drifted
  # from .env — inject the lab key over the serial console (chunked,
  # echo-verified) so every later step uses key auth.
  keyed=1
  attempt=0
  while [ "$attempt" -lt 3 ]; do
    attempt=$((attempt + 1))
    if ssh -o BatchMode=yes ai-legion \
        'sudo ssh -i /home/ubuntu/.ssh/id_ed25519 -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 root@10.99.99.1 true' 2>/dev/null; then
      keyed=0; break
    fi
    # console may still be under the provisioner's fingers — retry the
    # chunked key inject until ssh answers (issue #21: fresh overlays
    # also mean fresh host keys, so never pin them here)
    echo "== injecting lab key over serial console (attempt $attempt)…"
    scp -q "$REPO_ROOT/scripts/vm-keyinject.py" ai-legion:/tmp/ \
      && ssh -o BatchMode=yes ai-legion 'sudo python3 /tmp/vm-keyinject.py' | tail -1
    sleep 5
  done
  [ "$keyed" -eq 0 ] || fail "could not establish key auth to the poc VM after 3 attempts (console contention / issue #21)"
  # The ipk path is the deploy default: uci-defaults performs the full
  # NDS+portal setup, exactly how the cloud lab deploys.
  IPK="${TOLLGATE_IPK:-$HOME/src/tollgate-module-basic-go/packaging/tollgate-wrt_v0.6.0-rc1-local_x86_64.ipk}"
  if [ -f "$IPK" ]; then
    echo "== deploying $IPK to the VM"
    scp -q "$IPK" ai-legion:/tmp/tollgate-extensive.ipk
    ssh -o BatchMode=yes ai-legion 'sudo bash -c "
        K=/home/ubuntu/.ssh/id_ed25519
        scp -q -O -i \$K -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null /tmp/tollgate-extensive.ipk root@10.99.99.1:/tmp/
        ssh -i \$K -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@10.99.99.1 \
        \"opkg update >/dev/null 2>&1; opkg install --force-overwrite /tmp/tollgate-extensive.ipk | tail -2; rm -f /etc/tollgate/wallet.db; /etc/init.d/tollgate-wrt restart; sleep 6\"
        # post-deploy health gate: an aborted opkg transaction orphan-removes
        # runtime deps (nodogsplash zeroed — tbmg fork#93 class); without this
        # gate the portal is silently dead and every tier fails opaquely.
        ssh -i \$K -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@10.99.99.1 \
        \"[ -s /usr/bin/nodogsplash ] || { echo NDS-BINARY-EMPTY; exit 1; }; ndsctl status >/dev/null 2>&1 || { echo NDSCTL-DEAD; netstat -tln | grep 2050 || true; exit 1; }; curl -s -m 5 http://127.0.0.1:2121/ | head -c 120\""' \
      2>&1 | tee "$OUT/deploy.log" | tail -4
  else
    echo "== no ipk at $IPK — falling back to deploy-local-vm.sh (needs VM key auth)"
    ssh -o BatchMode=yes ai-legion \
      'sudo bash -c "cd /root/src/physical-router-test-automation && bash scripts/deploy-local-vm.sh"' \
        2>&1 | tee "$OUT/deploy.log" | tail -3
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

run_tier_local() {
  # virtual venue: delegate to run-local-tests.sh ON the lab host — it owns
  # the venue env (fakewallet mint, VM ssh, client MAC). Flags pass through
  # to pytest; its junit lands at /tmp/local-suite-junit.xml.
  local tier="$1"
  echo "== tier: $tier (virtual, via run-local-tests.sh) ${STORIES:+stories=$STORIES }${PATHS:+paths=$PATHS}"
  local rc=0
  ssh -o BatchMode=yes ai-legion \
    "sudo bash -c 'cd /root/src/physical-router-test-automation && \
     scripts/run-local-tests.sh tests/ -m \"${tier}${PATH_EXPR}\" ${select_args[*]:-}'" \
      2>&1 | tee "$OUT/raw/output-${tier}.log" | tail -3 || rc=1
  ssh -o BatchMode=yes ai-legion 'sudo cat /tmp/local-suite-junit.xml 2>/dev/null' \
      > "$OUT/raw/junit-${tier}.xml" || true
  return $rc
}

run_tier_physical() {
  local tier="$1"
  echo "== tier: $tier ${STORIES:+stories=$STORIES }${PATHS:+paths=$PATHS}"
  "$PY" -m pytest tests/ -m "${tier}${PATH_EXPR}" \
    "${select_args[@]}" \
    -q --tb=short \
    --junitxml="$OUT/raw/junit-${tier}.xml" \
    2>&1 | tee "$OUT/raw/output-${tier}.log" | tail -2
}

run_tier() {
  # keep going: one tier failing must not hide the others; verdict at the end
  if [ "$VENUE" = "virtual" ]; then run_tier_local "$1" || return 1
  else run_tier_physical "$1" || return 1; fi
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
