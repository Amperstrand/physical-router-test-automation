#!/usr/bin/env bash
#
# run-pr-vm-smoke.sh — formalized per-PR VM smoke for the local QEMU lab.
#
# Mirrors the manual sequence used in the 2026-09-28 review marathon
# (PRs #619/#549/#533): fetch the PR head, build the binary locally,
# ensure the local virtual lab + a fakewallet mint container are up,
# point the router's config at the lab mint, deploy the binary and
# cleanly restart the service, register the Debian client with NDS,
# mint a token via the cloud-lab-client image using the ROUTER-VISIBLE
# mint URL, pay by POSTing the raw token from the client, and assert
# the kind-1022 session event plus the NDS Authenticated state.
#
# Usage:
#   run-pr-vm-smoke.sh --pr N [--repo OpenTollGate/tollgate-module-basic-go]
#                      [--module src] [--binary-name tollgate-wrt]
#                      [--keep-lab] [--assert-cmd 'ssh cmd']
#
# Teardown discipline (other sessions' QEMUs share this host):
#   * scripts/virtual-lab.py stop-poc is NEVER invoked.
#   * teardown kills ONLY the QEMU PIDs this script itself started —
#     i.e. ~/tollgate-virtual-lab/run/*.pid PIDs that were NOT alive
#     when this script started (PIDs observed at start are protected).
#   * the router's original /etc/tollgate/config.json is restored on
#     exit unless --keep-lab is given.
#
# Test seams (used by tests/unit/test_vm_smoke_script.py): every external
# command (git, go, docker, curl, python3, sshpass/ssh/scp) resolves via
# PATH, and `kill` is always invoked as `env kill` so shims on PATH are
# honored. Paths/addresses are overridable via VMSMOKE_* env vars.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# --- Lab topology (LOCAL-VM-TESTING.md) --------------------------------
ROUTER_IP="${VMSMOKE_ROUTER_IP:-10.99.99.1}"
CLIENT_IP="${VMSMOKE_CLIENT_IP:-10.99.99.100}"
HOST_IP="${VMSMOKE_HOST_IP:-10.99.99.2}"
MINT_PORT="${VMSMOKE_MINT_PORT:-8383}"
MINT_URL="http://${HOST_IP}:${MINT_PORT}"
ROUTER_PASSWORD="${VMSMOKE_ROUTER_PASSWORD:-tollgate}"
RUN_DIR="${VMSMOKE_RUN_DIR:-${HOME}/tollgate-virtual-lab/run}"
OUT_DIR="${VMSMOKE_OUT_DIR:-${REPO_ROOT}/results/vm-smoke}"
DOCKER_BIN="${VMSMOKE_DOCKER:-docker}"
SSH_CONNECT_TIMEOUT="${VMSMOKE_SSH_CONNECT_TIMEOUT:-5}"
TOKEN_AMOUNT="${VMSMOKE_AMOUNT:-12}"

MINT_CONTAINER="lab-smoke-mint"
MINT_IMAGE="cloud-lab-mint"
CLIENT_IMAGE="cloud-lab-client"
MINT_MNEMONIC="abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"

ROUTER_CONFIG="/etc/tollgate/config.json"
ROUTER_CONFIG_BACKUP="/etc/tollgate/config.json.vm-smoke-orig"

# --- Options ------------------------------------------------------------
PR=""
REPO_SLUG="OpenTollGate/tollgate-module-basic-go"
MODULE="src"
BINARY_NAME="tollgate-wrt"
KEEP_LAB=0
ASSERT_CMD=""

usage() {
    printf 'Usage: run-pr-vm-smoke.sh --pr N [--repo OpenTollGate/tollgate-module-basic-go]\n'
    printf '                             [--module src] [--binary-name tollgate-wrt]\n'
    printf '                             [--keep-lab] [--assert-cmd '\''ssh cmd'\'']\n'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --pr) PR="${2:?--pr requires a value}"; shift 2 ;;
        --repo) REPO_SLUG="${2:?--repo requires a value}"; shift 2 ;;
        --module) MODULE="${2:?--module requires a value}"; shift 2 ;;
        --binary-name) BINARY_NAME="${2:?--binary-name requires a value}"; shift 2 ;;
        --keep-lab) KEEP_LAB=1; shift ;;
        --assert-cmd) ASSERT_CMD="${2:?--assert-cmd requires a value}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$PR" ] || ! [ "$PR" -gt 0 ] 2>/dev/null; then
    printf 'ERROR: --pr N is required (got: %s)\n\n' "${PR:-<empty>}" >&2
    usage >&2
    exit 2
fi

# --- Logging / step bookkeeping ------------------------------------------
log()  { printf '[vm-smoke] %s\n' "$*"; }
fail() { printf '[vm-smoke] FAIL: %s\n' "$*" >&2; }

STEP_NAMES=(fetch-and-build virtual-lab-up lab-mint router-deploy client-pay assert-cmd)
STEP_STATUS=()
STEP_DETAIL=()
for _ in "${STEP_NAMES[@]}"; do
    STEP_STATUS+=("SKIP")
    STEP_DETAIL+=("")
done
unset _

set_step() { # index status detail
    STEP_STATUS[$1]=$2
    STEP_DETAIL[$1]=$3
    printf '[STEP %d] %-16s %s — %s\n' "$(($1 + 1))" "${STEP_NAMES[$1]}" "$2" "$3"
}

json_escape() {
    local s=$1
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//$'\n'/\\n}
    s=${s//$'\r'/}
    s=${s//$'\t'/\\t}
    printf '%s' "$s"
}

# --- Runtime state -------------------------------------------------------
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/pr-vm-smoke.XXXXXX")"
SRC_DIR="${WORK_DIR}/pr-src"
BUILD_LOG="${WORK_DIR}/build.log"
DOCKER_LOG="${WORK_DIR}/docker.log"
PR_HEAD=""
CONFIG_TOUCHED=0
SUMMARY_PATH=""
SUMMARY_WRITTEN=0
declare -a OWNED_PIDS=()
declare -a SERVICE_LOG_TAIL=()

pid_alive() {
    env kill -0 "$1" 2>/dev/null
}

live_run_pids() { # print live PIDs recorded in $RUN_DIR/*.pid
    local f pid
    for f in "${RUN_DIR}"/*.pid; do
        [ -f "$f" ] || continue
        pid="$(cat "$f" 2>/dev/null | tr -d '[:space:]')"
        [ -n "$pid" ] || continue
        if pid_alive "$pid"; then
            printf '%s\n' "$pid"
        fi
    done
}

ssh_router() { # run a command on the OpenWrt VM as root
    sshpass -p "$ROUTER_PASSWORD" ssh \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR -o ConnectTimeout="$SSH_CONNECT_TIMEOUT" \
        "root@${ROUTER_IP}" "$*"
}

ssh_client() { # run a command on the Debian client VM (key-based)
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR -o ConnectTimeout="$SSH_CONNECT_TIMEOUT" \
        "debian@${CLIENT_IP}" "$*"
}

scp_to_router() { # src dest
    sshpass -p "$ROUTER_PASSWORD" scp -O -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
        "$1" "root@${ROUTER_IP}:$2"
}

collect_service_log_tail() {
    local line
    while IFS= read -r line; do
        SERVICE_LOG_TAIL+=("$line")
    done < <(ssh_router 'logread 2>/dev/null | grep -i tollgate | tail -n 20' 2>/dev/null || true)
}

write_summary() { # overall
    [ "$SUMMARY_WRITTEN" -eq 1 ] && return 0
    SUMMARY_WRITTEN=1
    mkdir -p "$OUT_DIR"
    SUMMARY_PATH="${OUT_DIR}/pr${PR}-$(date -u +%Y%m%dT%H%M%SZ).json"
    local overall=$1
    {
        printf '{\n'
        printf '  "schema_version": 1,\n'
        printf '  "generated_utc": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf '  "pr": %s,\n' "$PR"
        printf '  "repo": "%s",\n' "$(json_escape "$REPO_SLUG")"
        printf '  "module": "%s",\n' "$(json_escape "$MODULE")"
        printf '  "binary_name": "%s",\n' "$(json_escape "$BINARY_NAME")"
        printf '  "pr_head": "%s",\n' "$(json_escape "$PR_HEAD")"
        printf '  "mint_url": "%s",\n' "$(json_escape "$MINT_URL")"
        printf '  "router_ip": "%s",\n' "$ROUTER_IP"
        printf '  "client_ip": "%s",\n' "$CLIENT_IP"
        printf '  "keep_lab": %s,\n' "$([ "$KEEP_LAB" -eq 1 ] && echo true || echo false)"
        printf '  "overall": "%s",\n' "$overall"
        printf '  "owned_pids": ['
        local first=1 pid
        for pid in "${OWNED_PIDS[@]}"; do
            [ $first -eq 1 ] || printf ', '
            printf '"%s"' "$pid"
            first=0
        done
        printf '],\n'
        printf '  "steps": [\n'
        local i
        for i in "${!STEP_NAMES[@]}"; do
            printf '    {"name": "%s", "status": "%s", "detail": "%s"}%s\n' \
                "${STEP_NAMES[$i]}" "${STEP_STATUS[$i]}" \
                "$(json_escape "${STEP_DETAIL[$i]}")" \
                "$([ $((i + 1)) -lt ${#STEP_NAMES[@]} ] && printf ',')"
        done
        printf '  ],\n'
        printf '  "service_log_tail": ['
        first=1
        for pid in "${SERVICE_LOG_TAIL[@]}"; do
            [ $first -eq 1 ] || printf ', '
            printf '"%s"' "$(json_escape "$pid")"
            first=0
        done
        printf '],\n'
        printf '  "work_dir": "%s"\n' "$(json_escape "$WORK_DIR")"
        printf '}\n'
    } >"$SUMMARY_PATH"
    log "summary written to ${SUMMARY_PATH}"
}

teardown() {
    local rc=$?
    trap - EXIT
    # Restore the router's original config unless --keep-lab. Do this
    # BEFORE any PID teardown so the restore can still reach the router.
    if [ "$KEEP_LAB" -ne 1 ] && [ "$CONFIG_TOUCHED" -eq 1 ]; then
        log 'teardown: restoring original router config'
        ssh_router "[ -f ${ROUTER_CONFIG_BACKUP} ] && cp ${ROUTER_CONFIG_BACKUP} ${ROUTER_CONFIG}" >/dev/null 2>&1 || true
        if [ "${#OWNED_PIDS[@]}" -eq 0 ]; then
            # Router VM belongs to another session: bring the service back
            # to its original (pre-smoke) state cleanly.
            restart_service >/dev/null 2>&1 || true
        fi
    fi
    # PID discipline: kill ONLY the QEMU PIDs this script started (never
    # stop-poc, never the PIDs that were alive when we started).
    if [ "$KEEP_LAB" -ne 1 ] && [ "${#OWNED_PIDS[@]}" -gt 0 ]; then
        local pid
        for pid in "${OWNED_PIDS[@]}"; do
            log "teardown: killing QEMU pid ${pid} (started by this run)"
            env kill "$pid" 2>/dev/null || true
            sleep 1
            if pid_alive "$pid"; then
                env kill -9 "$pid" 2>/dev/null || true
            fi
        done
    fi
    if [ "$OVERALL_STATUS" = "FAIL" ]; then
        collect_service_log_tail
    fi
    write_summary "$OVERALL_STATUS"
    exit "$rc"
}

OVERALL_STATUS="FAIL"
trap teardown EXIT

restart_service() { # cleanly: stop, kill ALL strays, start ONE
    ssh_router "/etc/init.d/${BINARY_NAME} stop" >/dev/null 2>&1 || true
    ssh_router 'kill -9 $(pidof '"${BINARY_NAME}"') 2>/dev/null; true' >/dev/null 2>&1
    sleep 2
    ssh_router "/etc/init.d/${BINARY_NAME} start" >/dev/null 2>&1
}

# =========================================================================
# Step 1: fetch PR head and build the binary locally
# =========================================================================
step_fetch_and_build() {
    local i=0
    mkdir -p "$SRC_DIR"
    if ! git -C "$SRC_DIR" init -q \
        || ! git -C "$SRC_DIR" remote add origin "https://github.com/${REPO_SLUG}.git" \
        || ! git -C "$SRC_DIR" fetch --depth=1 origin "pull/${PR}/head" \
        || ! git -C "$SRC_DIR" checkout -q FETCH_HEAD; then
        set_step $i FAIL "git fetch/checkout of pull/${PR}/head failed"
        return 1
    fi
    PR_HEAD="$(git -C "$SRC_DIR" rev-parse HEAD)"
    log "PR #${PR} head: ${PR_HEAD}"
    if ! (cd "${SRC_DIR}/${MODULE}" && CGO_ENABLED=0 GOARCH=amd64 \
        go build -o "${WORK_DIR}/${BINARY_NAME}" .) >"$BUILD_LOG" 2>&1; then
        # Fail loudly with the compiler error — this is how #549's
        # root-module break was caught.
        printf '%s\n' '--- go build output (compiler error) ---' >&2
        tail -n 60 "$BUILD_LOG" >&2
        printf '%s\n' '---------------------------------------' >&2
        set_step $i FAIL "go build failed in ${MODULE} (see stderr; full log: ${BUILD_LOG})"
        return 1
    fi
    set_step $i PASS "head=${PR_HEAD} binary=${WORK_DIR}/${BINARY_NAME}"
}

# =========================================================================
# Step 2: ensure the local virtual lab is up (status/start-poc)
# =========================================================================
step_virtual_lab_up() {
    local i=1
    # Snapshot the live QEMU PIDs BEFORE any start so teardown can protect
    # PIDs that belonged to other sessions when this script began.
    local pre_pids live_now pid owned
    pre_pids="$(live_run_pids | sort -u)"
    local status
    status="$(python3 "${SCRIPT_DIR}/virtual-lab.py" status-poc --host localhost 2>&1)" || true
    if printf '%s\n' "$status" | grep -q 'running pid='; then
        log "virtual lab already running (status-poc): $(printf '%s' "$status" | grep -m1 'running pid=')"
    else
        log 'virtual lab not running — starting (start-poc)'
        if ! python3 "${SCRIPT_DIR}/virtual-lab.py" start-poc --host localhost; then
            set_step $i FAIL "virtual-lab.py start-poc failed"
            return 1
        fi
    fi
    # Wait for the router to become SSH-reachable.
    local attempt
    for attempt in $(seq 1 60); do
        if [ "$(ssh_router 'echo ssh-ok' 2>/dev/null)" = "ssh-ok" ]; then
            break
        fi
        sleep 2
        if [ "$attempt" -eq 60 ]; then
            set_step $i FAIL "router ${ROUTER_IP} not SSH-reachable after start-poc"
            return 1
        fi
    done
    # PIDs recorded now that were NOT alive at start == ours to kill later.
    live_now="$(live_run_pids | sort -u)"
    owned="$(comm -13 <(printf '%s\n' "$pre_pids" | grep .) <(printf '%s\n' "$live_now" | grep .) || true)"
    while IFS= read -r pid; do
        [ -n "$pid" ] && OWNED_PIDS+=("$pid")
    done < <(printf '%s\n' "$owned" | grep . || true)
    printf '%s\n' "${OWNED_PIDS[@]:-}" >"${WORK_DIR}/owned-pids.txt"
    set_step $i PASS "router reachable; owned QEMU pids: ${OWNED_PIDS[*]:-none}"
}

# =========================================================================
# Step 3: ensure the lab mint container (lab-smoke-mint) runs on :8383
# =========================================================================
ensure_image() { # image dockerfile context  -> 0 if image available
    if "$DOCKER_BIN" image inspect "$1" >/dev/null 2>&1; then
        return 0
    fi
    log "image ${1} pruned — rebuilding from ${2}"
    "$DOCKER_BIN" build -t "$1" -f "$2" "$3" >"$DOCKER_LOG" 2>&1 || {
        tail -n 40 "$DOCKER_LOG" >&2
        return 1
    }
}

step_lab_mint() {
    local i=2
    if ! printf '%s' "$("$DOCKER_BIN" inspect -f '{{.State.Running}}' "$MINT_CONTAINER" 2>/dev/null)" | grep -qx true; then
        if "$DOCKER_BIN" ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$MINT_CONTAINER"; then
            log "container ${MINT_CONTAINER} exists but is stopped — starting it"
            "$DOCKER_BIN" start "$MINT_CONTAINER" >/dev/null
        else
            if ! ensure_image "$MINT_IMAGE" \
                "${SRC_DIR}/tests/cloud-lab/Dockerfile.mint" \
                "${SRC_DIR}/tests/cloud-lab"; then
                set_step $i FAIL "cannot build ${MINT_IMAGE} from PR tree"
                return 1
            fi
            # Env-var config as used by the marathon (cloud-lab docker-compose
            # mint service: fakewallet, fixed mnemonic, sqlite, memory cache).
            if ! "$DOCKER_BIN" run -d --name "$MINT_CONTAINER" \
                -p "${MINT_PORT}:8085" \
                -e "CDK_MINTD_URL=${MINT_URL}" \
                -e CDK_MINTD_LN_BACKEND=fakewallet \
                -e CDK_MINTD_LISTEN_HOST=0.0.0.0 \
                -e CDK_MINTD_LISTEN_PORT=8085 \
                -e "CDK_MINTD_MNEMONIC=${MINT_MNEMONIC}" \
                -e CDK_MINTD_DATABASE=sqlite \
                -e CDK_MINTD_DATABASE_PATH=data/mint \
                -e CDK_MINTD_CACHE_BACKEND=memory \
                -v lab-smoke-mint-data:/data \
                "$MINT_IMAGE" >/dev/null; then
                set_step $i FAIL "docker run ${MINT_CONTAINER} on :${MINT_PORT} failed"
                return 1
            fi
        fi
    fi
    local attempt
    for attempt in $(seq 1 30); do
        if curl -sf --max-time 3 "${MINT_URL}/v1/keys" >/dev/null 2>&1; then
            set_step $i PASS "mint healthy at ${MINT_URL}"
            return 0
        fi
        sleep 2
    done
    set_step $i FAIL "mint did not become healthy at ${MINT_URL}/v1/keys within 60s"
    return 1
}

# =========================================================================
# Step 4: point router config at the lab mint, deploy binary, clean restart
# =========================================================================
step_router_deploy() {
    local i=3
    # Backup the original config ONCE, restore on exit (teardown).
    if ! ssh_router "[ -f ${ROUTER_CONFIG_BACKUP} ] || cp ${ROUTER_CONFIG} ${ROUTER_CONFIG_BACKUP}"; then
        set_step $i FAIL "cannot back up ${ROUTER_CONFIG} on router"
        return 1
    fi
    CONFIG_TOUCHED=1
    if ! ssh_router "jq '.accepted_mints = [{\"url\":\"${MINT_URL}\"}]' ${ROUTER_CONFIG} > /tmp/vm-smoke-cfg.json && mv /tmp/vm-smoke-cfg.json ${ROUTER_CONFIG}"; then
        set_step $i FAIL "cannot set accepted_mints to ${MINT_URL}"
        return 1
    fi
    if ! scp_to_router "${WORK_DIR}/${BINARY_NAME}" "/tmp/${BINARY_NAME}" \
        || ! ssh_router "mv /tmp/${BINARY_NAME} /usr/bin/${BINARY_NAME} && chmod +x /usr/bin/${BINARY_NAME}"; then
        set_step $i FAIL "deploying ${BINARY_NAME} to router failed"
        return 1
    fi
    restart_service
    # Poll :2121 until the advertisement answers.
    local attempt ad
    for attempt in $(seq 1 30); do
        ad="$(curl -sf --max-time 3 "http://${ROUTER_IP}:2121/" 2>/dev/null || true)"
        if [ -n "$ad" ] && printf '%s' "$ad" | grep -q 10021; then
            if [ "$(ssh_router "pidof ${BINARY_NAME} | wc -w" 2>/dev/null | tr -d '[:space:]')" = "1" ]; then
                set_step $i PASS "config→${MINT_URL}; binary deployed; advertisement up (kind 10021); one ${BINARY_NAME} pid"
                return 0
            fi
        fi
        sleep 2
    done
    set_step $i FAIL "service did not answer with advertisement on :2121 (or pid count != 1)"
    return 1
}

# =========================================================================
# Step 5: register client with NDS, mint token, pay, assert kind-1022 + Authenticated
# =========================================================================
step_client_pay() {
    local i=4
    # 5a. Register the Debian client with NDS (any HTTP attempt through the
    # gateway makes NDS track the client).
    ssh_client "curl -sS --max-time 20 -o /dev/null 'http://example.com/'" >/dev/null 2>&1 || true
    log 'NDS registration traffic sent from Debian client'
    # 5b. Mint a token via the cloud-lab-client image, using the
    # ROUTER-VISIBLE mint URL.
    if ! ensure_image "$CLIENT_IMAGE" \
        "${SRC_DIR}/tests/cloud-lab/Dockerfile.client" \
        "${SRC_DIR}/tests/cloud-lab"; then
        set_step $i FAIL "cannot build ${CLIENT_IMAGE} from PR tree"
        return 1
    fi
    local token_out token
    if ! token_out="$("$DOCKER_BIN" run --rm --network host "$CLIENT_IMAGE" \
        sh -c "echo ${TOKEN_AMOUNT} | cdk-cli -w /tmp/vm-smoke-wallet mint ${MINT_URL} ${TOKEN_AMOUNT} >/dev/null && echo ${TOKEN_AMOUNT} | cdk-cli -w /tmp/vm-smoke-wallet send --mint-url ${MINT_URL} --v3" 2>"${WORK_DIR}/cdk-cli.err")" \
        || [ -z "$token_out" ]; then
        tail -n 20 "${WORK_DIR}/cdk-cli.err" >&2 || true
        set_step $i FAIL "cdk-cli mint/send via ${CLIENT_IMAGE} failed"
        return 1
    fi
    token="$(printf '%s\n' "$token_out" | awk 'NF {last=$0} END {print last}')"
    log "minted ${TOKEN_AMOUNT}-sat token (len ${#token}) from ${MINT_URL}"
    # 5c. Pay by POSTing the raw token from the client.
    local pay_resp
    pay_resp="$(printf '%s' "$token" | ssh_client "curl -s --max-time 20 -d @- -H 'Content-Type: text/plain' 'http://${ROUTER_IP}:2121/'" 2>/dev/null || true)"
    if ! printf '%s' "$pay_resp" | grep -Eq '"kind"[[:space:]]*:[[:space:]]*1022'; then
        set_step $i FAIL "payment did not return kind-1022 session event: $(printf '%s' "$pay_resp" | head -c 300)"
        return 1
    fi
    log "payment accepted (kind 1022): $(printf '%s' "$pay_resp" | head -c 200)"
    # 5d. Assert the NDS Authenticated state for the client.
    local nds_state
    nds_state="$(ssh_router 'ndsctl clients 2>/dev/null' | awk -v ip="${CLIENT_IP}" '
        $0 ~ ip {window=25}
        window > 0 {
            if (match($0, /state=[A-Za-z]+/)) {print substr($0, RSTART, RLENGTH); exit}
            window--
        }')"
    if ! printf '%s' "$nds_state" | grep -q Authenticated; then
        set_step $i FAIL "client ${CLIENT_IP} not Authenticated in ndsctl (state: '${nds_state:-not-listed}')"
        return 1
    fi
    set_step $i PASS "paid ${TOKEN_AMOUNT} sats; kind-1022; ${nds_state}"
}

# =========================================================================
# Step 6: PR-specific assertion command on the router
# =========================================================================
step_assert_cmd() {
    local i=5
    if [ -z "$ASSERT_CMD" ]; then
        set_step $i PASS "no --assert-cmd requested"
        return 0
    fi
    log "running assert-cmd on router: ${ASSERT_CMD}"
    if ssh_router "$ASSERT_CMD"; then
        set_step $i PASS "assert-cmd succeeded: ${ASSERT_CMD}"
    else
        set_step $i FAIL "assert-cmd failed: ${ASSERT_CMD}"
        return 1
    fi
}

# =========================================================================
# Step 7: report (stdout + JSON summary; nonzero exit on any failure)
# =========================================================================
main() {
    log "VM smoke for ${REPO_SLUG}#${PR} (mint ${MINT_URL}, router ${ROUTER_IP}, client ${CLIENT_IP})"
    local fatal=0
    step_fetch_and_build || fatal=1
    if [ $fatal -eq 0 ]; then step_virtual_lab_up || fatal=1; fi
    if [ $fatal -eq 0 ]; then step_lab_mint || fatal=1; fi
    if [ $fatal -eq 0 ]; then step_router_deploy || fatal=1; fi
    if [ $fatal -eq 0 ]; then step_client_pay || fatal=1; fi
    if [ $fatal -eq 0 ]; then step_assert_cmd || fatal=1; fi

    local st overall=PASS
    for st in "${STEP_STATUS[@]}"; do
        [ "$st" = "FAIL" ] && overall=FAIL
    done
    OVERALL_STATUS="$overall"
    log "overall: ${overall}"
    if [ "$overall" = "FAIL" ]; then
        fail "one or more steps failed — keeping ${WORK_DIR} for inspection"
    else
        rm -rf "$WORK_DIR"
    fi
    [ "$overall" = "PASS" ]
}

main
