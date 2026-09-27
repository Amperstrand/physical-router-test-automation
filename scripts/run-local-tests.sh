#!/usr/bin/env bash
set -euo pipefail

# Local test runner for TollGate virtual lab on ai-legion-small.
# Starts CDK mint, configures OpenWrt, runs pytest — no cloud VM needed.
# Results stay local (TOLLGATE_VM_PROVIDER=local, can_publish=False).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
WORKDIR="${HOME}/tollgate-virtual-lab"
PASSWORD="${TOLLGATE_VIRTUAL_LAB_PASSWORD:-$(jq -r .password "${REPO_DIR}/credentials/virtual-lab-credentials.json" 2>/dev/null || echo tollgate)}"

OPENWRT_IP="10.99.99.1"
DEBIAN_IP="10.99.99.100"
HOST_BRIDGE_IP="10.99.99.2"
MINT_PORT=8383
MINT_URL="http://${HOST_BRIDGE_IP}:${MINT_PORT}"

CDK_BIN="/opt/cdk-mintd/cdk-mintd"
CDK_CONFIG="/tmp/cdk-mintd-local/config.toml"
CDK_LOG="/tmp/cdk-mintd-local.log"
CDK_PID_FILE="/tmp/cdk-mintd-local.pid"
# Override with CDK_VER when a specific mint release is needed; the binaries
# themselves are installed by scripts/bake-snapshot.py / shc bootstrap.
CDK_VER="${CDK_VER:-0.18.0}"

log() { echo "[run-local] $*"; }

cdk_minor_version() {
  "${CDK_BIN}" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1 | cut -d. -f2
}

start_mint() {
  if curl -sf "${MINT_URL}/v1/info" >/dev/null 2>&1; then
    log "CDK mint already running at ${MINT_URL}"
    # Record the existing daemon's PID so the EXIT trap cleans it up too —
    # a manually-started mint otherwise survives the runner.
    pgrep -f 'cdk-mintd.*cdk-mintd-local' | head -1 > "${CDK_PID_FILE}" 2>/dev/null || true
    verify_mint_fakewallet || restart_mint
    return
  fi

  if [ ! -x "${CDK_BIN}" ]; then
    log "ERROR: CDK mint not installed at ${CDK_BIN}"
    exit 1
  fi

  log "Starting CDK V2 mint on port ${MINT_PORT}..."
  mkdir -p /tmp/cdk-mintd-local

  local cdk_minor
  cdk_minor=$(cdk_minor_version)
  if [ "${cdk_minor:-0}" -ge 18 ]; then
    # v0.18+: config lives in the mint DB ([ln] renamed to [payment_backend],
    # config.toml ignored at start; `config init --new-mint` required pre-start).
    # Fakewallet value is disposable and the mnemonic is fixed, so start from a
    # fresh work dir every time — no legacy-DB migration edge cases.
    rm -rf /tmp/cdk-mintd-local
    mkdir -p /tmp/cdk-mintd-local
    cat > /tmp/cdk-mintd-local/config.toml << EOF
[info]
url = "${MINT_URL}/"
listen_host = "0.0.0.0"
listen_port = ${MINT_PORT}
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
    export CDK_MINTD_MNEMONIC="abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
    export CDK_MINTD_WORK_DIR=/tmp/cdk-mintd-local
    "${CDK_BIN}" --work-dir /tmp/cdk-mintd-local config validate \
      --file /tmp/cdk-mintd-local/config.toml || { log "ERROR: v0.18 config validation failed"; exit 1; }
    "${CDK_BIN}" --work-dir /tmp/cdk-mintd-local config init --new-mint \
      --file /tmp/cdk-mintd-local/config.toml || { log "ERROR: v0.18 config init failed"; exit 1; }
    setsid bash -c "exec ${CDK_BIN} --work-dir /tmp/cdk-mintd-local" >"${CDK_LOG}" 2>&1 &
  else
    cat > /tmp/cdk-mintd-local/config.toml << EOF
[info]
url = "${MINT_URL}/"
listen_host = "0.0.0.0"
listen_port = ${MINT_PORT}
mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"

[database]
engine = "sqlite"

[ln]
ln_backend = "fakewallet"

[fake_wallet]
supported_units = ["sat"]
fee_percent = 0
reserve_fee_min = 0
min_delay_time = 0
max_delay_time = 0
EOF
    setsid bash -c "exec ${CDK_BIN} -c ${CDK_CONFIG}" >"${CDK_LOG}" 2>&1 &
  fi
  echo $! > "${CDK_PID_FILE}"

  for i in $(seq 1 15); do
    if curl -sf "${MINT_URL}/v1/info" >/dev/null 2>&1; then
      log "CDK mint ready (PID $(cat ${CDK_PID_FILE}))"
      verify_mint_fakewallet || restart_mint
      resync_backend_wallet
      return
    fi
    sleep 1
  done
  log "ERROR: CDK mint did not become healthy"
  cat "${CDK_LOG}" | tail -10
  exit 1
}

# /v1/info answering is not proof of health: a wedged cdk-mintd accepts
# quotes but its FakeWallet never settles them, which makes every
# payment test hang until the outer timeout. Probe the actual
# quote→PAID path; a healthy FakeWallet settles in ~1s.
verify_mint_fakewallet() {
  local quote state
  quote=$(curl -sf --max-time 5 -X POST "${MINT_URL}/v1/mint/quote/bolt11" \
    -H 'Content-Type: application/json' -d '{"amount":1,"unit":"sat"}' \
    | jq -r '.quote // empty' 2>/dev/null) || return 1
  [ -n "$quote" ] || return 1
  for _ in $(seq 1 6); do
    state=$(curl -sf --max-time 5 "${MINT_URL}/v1/mint/quote/bolt11/${quote}" \
      | jq -r '.state // empty' 2>/dev/null) || return 1
    [ "$state" = "PAID" ] && return 0
    sleep 2
  done
  log "WARNING: mint FakeWallet did not settle a probe quote (state=${state:-none})"
  return 1
}

resync_backend_wallet() {
  # The Go wallet caches mint state in /etc/tollgate/wallet.db. After the
  # mint work-dir is wiped between suite runs (fresh DB, same keysets), the
  # backend keeps serving kind 21023 "no-reachable-mints" even though its
  # proactive prober reports ok=true and the mint answers 200 — payments
  # still work but discovery/degraded tests cascade-fail. Restarting the
  # backend makes the wallet re-register the mint (kind 10021 within ~10s).
  # Root-caused 2026-09-04: probes ok=true + curl 200 + discovery 21023.
  sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
    "root@${OPENWRT_IP}" "/etc/init.d/tollgate-wrt restart" >/dev/null 2>&1 || true
  sleep 8
  log "Backend wallet resync: tollgate-wrt restarted"
}

restart_mint() {
  log "Restarting wedged CDK mint..."
  if [ -f "${CDK_PID_FILE}" ]; then
    kill "$(cat ${CDK_PID_FILE})" 2>/dev/null || true
    sleep 1
  fi
  pkill -f "cdk-mintd.*cdk-mintd-local" 2>/dev/null || true
  sleep 1
  start_mint
}

stop_mint() {
  if [ -f "${CDK_PID_FILE}" ]; then
    kill "$(cat ${CDK_PID_FILE})" 2>/dev/null || true
    rm -f "${CDK_PID_FILE}"
  fi
  # The pid file can hold a launcher-shell pid (pgrep matches the wrapper
  # too); sweep by pattern so no mint daemon outlives the runner.
  pkill -f 'cdk-mintd.*cdk-mintd-local' 2>/dev/null || true
  log "CDK mint stopped"
}

check_vms() {
  if ! sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 "root@${OPENWRT_IP}" "echo ok" >/dev/null 2>&1; then
    log "ERROR: OpenWrt VM not reachable at ${OPENWRT_IP}"
    log "Run: python3 scripts/virtual-lab.py start-poc --host localhost"
    exit 1
  fi
  DEBIAN_VM_UP=false
  if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 "root@${DEBIAN_IP}" "echo ok" >/dev/null 2>&1; then
    DEBIAN_VM_UP=true
    # The client's cloud-init default resolver (host systemd-resolved) only
    # answers during provisioning; steady-state DNS must come from the router.
    # Without this, internet-verification tests fail on DNS, not gating.
    ssh -o StrictHostKeyChecking=no "root@${DEBIAN_IP}" \
      "grep -q 'nameserver 10.99.99.1' /etc/resolv.conf 2>/dev/null || printf 'nameserver 10.99.99.1\n' > /etc/resolv.conf" \
      2>/dev/null || log "WARNING: could not set client DNS to router"
  else
    log "WARNING: Debian VM not reachable at ${DEBIAN_IP} (payment tests will skip)"
  fi
  log "VMs OK"
}

configure_mint() {
  log "Configuring OpenWrt to use local mint..."
  sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no "root@${OPENWRT_IP}" "
    jq '.accepted_mints = [{\"url\": \"${MINT_URL}\", \"min_balance\": 0, \"balance_tolerance_percent\": 0, \"price_per_step\": 1, \"price_unit\": \"sats\", \"purchase_min_steps\": 0}]' /etc/tollgate/config.json > /tmp/cfg.json
    mv /tmp/cfg.json /etc/tollgate/config.json
    /etc/init.d/tollgate-wrt restart
  " 2>&1 | tail -3

  for i in $(seq 1 20); do
    if sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no "root@${OPENWRT_IP}" \
      "wget -qO- --timeout=3 http://127.0.0.1:2121/ 2>/dev/null | head -c 20" 2>/dev/null | grep -q "10021\|21023"; then
      log "Backend healthy with local mint"
      return
    fi
    sleep 2
  done
  log "WARNING: Backend health check timed out"
}

run_tests() {
  local targets=() flags=()
  local arg
  for arg in "$@"; do
    case "$arg" in
      -*) flags+=("$arg") ;;
      *) targets+=("$arg") ;;
    esac
  done
  if [ "${#targets[@]}" -eq 0 ]; then
    targets=(tests/api/test_quote_persistence.py tests/api/test_lightning_backoff.py tests/api/test_payment_regression.py tests/api/test_mint_url_fuzzy.py)
  fi

  local expanded=() t
  for t in "${targets[@]}"; do
    if [ -d "${t}" ]; then
      local dir_files
      dir_files=$(find "${t}" -maxdepth 1 -name 'test_*.py' | sort)
      if [ -n "${dir_files}" ]; then
        while IFS= read -r dir_files; do expanded+=("${dir_files}"); done <<< "${dir_files}"
      else
        log "WARNING: no test_*.py found in ${t}"
      fi
    else
      expanded+=("${t}")
    fi
  done
  targets=("${expanded[@]}")

  cd "${REPO_DIR}"
  source "${HOME}/.tollgate-test-venv/bin/activate"

  export TOLLGATE_SSH_HOST="${OPENWRT_IP}"
  export TOLLGATE_LUCI_PASSWORD="${PASSWORD}"
  export TOLLGATE_SSH_PASSWORD="${PASSWORD}"
  export TOLLGATE_TEST_MINT_URL="${MINT_URL}"
  export TOLLGATE_BACKEND=go
  export TOLLGATE_CLIENT_TYPE=container
  export TOLLGATE_VM_PROVIDER=local
  export TOLLGATE_CASHU_VENV=/opt/cashu-venv
  export TOLLGATE_CLIENT_IP="${DEBIAN_IP}"
  export TOLLGATE_CLIENT_MAC="de:54:4e:91:49:da"
  export CASHU_DIR=/tmp/cashu-local

  mkdir -p /tmp/cashu-local

  # Pre-trigger NDS for Debian client (required for payment tests)
  ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 "root@${DEBIAN_IP}" \
    "curl -s -o /dev/null --max-time 5 http://example.com" 2>/dev/null || true

  # Portal e2e only runs with --client=container; default it on when the
  # client VM is up and the caller passed no --client (no change if VM absent).
  local client_default=""
  local client_seen=false
  for arg in "${flags[@]}"; do
    case "$arg" in
      --client|--client=*) client_seen=true ;;
    esac
  done
  for arg in "${targets[@]}"; do
    case "$arg" in
      --client|--client=*) client_seen=true ;;
    esac
  done
  if [ "${DEBIAN_VM_UP:-false}" = true ] && [ "${client_seen}" = false ]; then
    client_default="--client=container"
    log "Debian client VM up and no --client given: defaulting to --client=container"
  fi

  # Per-file hard budget (issue #105): pytest-timeout's signal method cannot
  # kill a test hung in a thread pool or blocking C call — the whole suite sat
  # on it forever. `timeout` SIGTERMs the pytest process (SIGKILL after the
  # grace period), so one hang costs one file, not the run. Set to 0 to
  # disable the hard kill and rely on pytest-timeout alone (old behavior).
  local file_budget="${LOCAL_TEST_FILE_BUDGET:-1200}"
  local timeout_cmd=()
  if [ "${file_budget}" != "0" ]; then
    timeout_cmd=(timeout --kill-after=15 "${file_budget}")
  fi

  local pytest_common=(-v --no-deploy --timeout=180 --timeout-method=signal --tb=short -rA)
  local overall_rc=0

  if [ "${#targets[@]}" -eq 1 ]; then
    log "Running pytest (single invocation): ${targets[0]} ${flags[*]:-}"
    "${timeout_cmd[@]}" python3 -m pytest "${targets[0]}" "${pytest_common[@]}" \
      ${client_default} --junitxml=/tmp/local-suite-junit.xml "${flags[@]}"
    return $?
  fi

  local junit_dir=/tmp/local-suite-junit
  rm -rf "${junit_dir}" && mkdir -p "${junit_dir}"
  log "Running pytest per-file (${#targets[@]} files, budget ${file_budget}s/file):"
  printf '  %s\n' "${targets[@]}"

  local i=0 rc=0
  for t in "${targets[@]}"; do
    i=$((i + 1))
    local base
    base=$(basename "${t}" .py)
    local junit_file
    junit_file="${junit_dir}/$(printf '%03d' "${i}")-${base}.xml"
    log "[${i}/${#targets[@]}] ${t}"
    rc=0
    "${timeout_cmd[@]}" python3 -m pytest "${t}" "${pytest_common[@]}" \
      ${client_default} --junitxml="${junit_file}" "${flags[@]}" || rc=$?
    if [ "${rc}" -eq 124 ] || [ "${rc}" -eq 137 ]; then
      log "TIMEOUT: ${t} exceeded ${file_budget}s hard budget (rc=${rc}) — continuing with remaining files"
      overall_rc=1
      printf '%s' "${t}" > "${junit_file}.timeout"
    elif [ "${rc}" -ne 0 ]; then
      log "FAIL: ${t} (rc=${rc})"
      overall_rc=1
    fi
  done

  merge_junit "${junit_dir}" /tmp/local-suite-junit.xml
  return "${overall_rc}"
}

# Merge per-file junit XMLs into one suite file. A file that hit the hard
# budget is marked by a sibling <file>.xml.timeout marker and gets a synthetic
# failure entry.
merge_junit() {
  local junit_dir="$1" out="$2"
  python3 - "${junit_dir}" "${out}" <<'PYEOF'
import sys, os
import xml.etree.ElementTree as ET

junit_dir, out = sys.argv[1], sys.argv[2]
suite = ET.Element("testsuites")
for name in sorted(os.listdir(junit_dir)):
    if name.endswith(".timeout"):
        # Killed before pytest could write junit — synthesize the failure here
        # (paired markers whose .xml also exists are handled below).
        base = name[: -len(".timeout")]
        if os.path.exists(os.path.join(junit_dir, base)):
            continue
        target = open(os.path.join(junit_dir, name)).read().strip()
        ts = ET.SubElement(suite, "testsuite", {
            "name": f"{base} (runner timeout)",
            "tests": "1", "failures": "1", "errors": "0", "time": "0",
        })
        tc = ET.SubElement(ts, "testcase", {
            "classname": "runner", "name": f"{target} — hard budget exceeded",
        })
        ET.SubElement(tc, "failure", {
            "message": f"pytest exceeded the per-file hard budget and was killed: {target}",
        }).text = "Hung past LOCAL_TEST_FILE_BUDGET; the file must be rerun individually."
        continue
    if not name.endswith(".xml"):
        continue
    path = os.path.join(junit_dir, name)
    marker = path + ".timeout"
    if os.path.exists(marker):
        target = open(marker).read().strip()
        ts = ET.SubElement(suite, "testsuite", {
            "name": f"{name} (runner timeout)",
            "tests": "1", "failures": "1", "errors": "0", "time": "0",
        })
        tc = ET.SubElement(ts, "testcase", {
            "classname": "runner", "name": f"{target} — hard budget exceeded",
        })
        ET.SubElement(tc, "failure", {
            "message": f"pytest exceeded the per-file hard budget and was killed: {target}",
        }).text = "Hung past LOCAL_TEST_FILE_BUDGET; the file must be rerun individually."
        continue
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        ts = ET.SubElement(suite, "testsuite", {
            "name": f"{name} (unparsable junit)",
            "tests": "1", "failures": "0", "errors": "1", "time": "0",
        })
        tc = ET.SubElement(ts, "testcase", {"classname": "runner", "name": name})
        ET.SubElement(tc, "error", {"message": "junit XML could not be parsed"})
        continue
    if root.tag == "testsuites":
        suite.extend(list(root))
    else:
        suite.append(root)
ET.ElementTree(suite).write(out, encoding="utf-8", xml_declaration=True)
print(f"[run-local] merged junit -> {out}")
PYEOF
}

nds_healthy() {
  # Host-side timeout: the router has no `timeout` binary, and a wedged NDS
  # hangs the ssh channel forever (ndsctl never returns).
  local out
  out=$(timeout 15 sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no \
    -o ConnectTimeout=8 "root@${OPENWRT_IP}" "ndsctl json 2>/dev/null | head -c 200" 2>/dev/null) || return 1
  case "${out}" in "{"*) return 0 ;; *) return 1 ;; esac
}

ensure_nds_healthy() {
  # A wedged NDS (documented trap: SIGKILL'd runs leave ndsctl hanging while
  # the portal stays up) poisons the whole suite — every container_nds_preflight
  # times out as setup errors. Restart NDS + backend once; abort if still
  # wedged instead of burning a 2h run on cascade errors.
  if nds_healthy; then
    return 0
  fi
  log "ndsctl unresponsive — restarting nodogsplash + tollgate-wrt"
  timeout 30 sshpass -p "${PASSWORD}" ssh -o StrictHostKeyChecking=no \
    -o ConnectTimeout=8 "root@${OPENWRT_IP}" \
    "/etc/init.d/nodogsplash restart; /etc/init.d/tollgate-wrt restart" >/dev/null 2>&1 || true
  sleep 12
  if nds_healthy; then
    log "NDS recovered after restart"
    return 0
  fi
  log "ERROR: NDS still unresponsive after restart — reboot the OpenWrt VM (virtual-lab.py stop-poc + start-poc) before rerunning"
  exit 1
}

cleanup() {
  stop_mint
}
trap cleanup EXIT

log "=== TollGate Local Test Runner ==="
check_vms
start_mint
configure_mint
ensure_nds_healthy
run_tests "$@"
