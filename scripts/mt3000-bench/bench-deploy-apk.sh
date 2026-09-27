#!/usr/bin/env bash
#
# bench-deploy-apk.sh — deploy ONE named .apk to the bench MT3000, while holding the
# single-owner bench lock, and then PROVE what actually got installed.
#
# It exists because "the install exited 0 and a smoke test passed" is not identity
# evidence, and because a leftover /tmp/*.apk on the bench is a loaded gun:
#
#   1. NO LOCK, NO INSTALL. It refuses unless it is running inside a bench lock window
#      (`bench-with-lock.sh` / `bench-lock.sh exec`). See scripts/mt3000-bench/bench-lock.sh.
#   2. IT NAMES ITS ARTIFACT. `--apk FILE --sha256 HEX` is mandatory: the helper prints the
#      path, size and sha256 it intends to install, and refuses if the file does not hash
#      to the sha256 it was told to expect. It also extracts usr/bin/tollgate-wrt from that
#      apk and records the PAYLOAD sha256 — the identity it will verify after the install.
#   3. IT ROTATES STALE STAGED APKS. Every /tmp/*.apk it did not stage for this window is
#      renamed aside (`.rotated-<ts>`) and reported — never deleted, another window may own
#      the bytes. `--refuse-foreign-staged` refuses instead of rotating.
#   4. IT REFUSES SUBSTITUTED ARTIFACTS. If an apk is already staged under the name this
#      deploy intends to use but its bytes differ from the named artifact, or if
#      /etc/tollgate/install.json `package_path` points at an apk (the revert bomb), the
#      helper stops BEFORE installing.
#   5. IT VERIFIES THE INSTALLED BINARY. After the install it reads
#      `sha256sum $TG_BIN/tollgate-wrt` + size on the router and compares them with the
#      payload bytes extracted from the named artifact. Mismatch => loud failure (exit 8),
#      with both hashes and the router's apk.log tail as forensics. Exit code is never
#      treated as identity evidence.
#
# USAGE
#   bench-with-lock.sh --purpose "deploy <build>" -- \
#       bench-deploy-apk.sh --apk /path/tollgate-wrt-....apk --sha256 <64 hex> \
#           [--name <basename to stage as>] [--router 192.168.1.1] \
#           [--task t_xxxx] [--verify-only] [--dry-run] \
#           [--refuse-foreign-staged] [--clear-package-path] [--install-timeout SECONDS]
#
#   --verify-only   read-only: verify the router's CURRENT installed identity against the
#                   named artifact (no rotation, no upload, no install). This is the honest
#                   way to check "is the bench still running the build I handed over?".
#   --dry-run       do everything up to (not including) the install.
#
# EXIT CODES
#   0 ok | 2 usage | 4 not holding the bench lock | 6 local artifact/tooling problem
#   7 substituted/stale-staged artifact or package_path refusal (nothing installed)
#   8 installed identity MISMATCH (loud) | 9 transfer failed (nothing installed)
#   10 install did not complete
#
# CREDENTIALS: never on argv, never in this file. The lab credential is read from
# $BENCH_ROUTER_PW_FILE (default ~/.tg-e2e/pw) or, as a warned fallback,
# $BENCH_ROUTER_PASSWORD. See the `tollgate-development` skill (physical-router-testing →
# references/lab-credentials-and-http-probe.md).
#
# TEST SEAM (offline harness only): the remote scripts read TG_TMP / TG_BIN / TG_ETC /
# TG_APKLOG (defaults /tmp, /usr/bin, /etc/tollgate, /var/log/apk.log). The no-router test
# double in tests/mt3000-bench/harness/ exports these into a throw-away root so the SAME
# script text runs against a stand-in router. Nothing else in this file is test-aware.
#
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
LOCK="$HERE/bench-lock.sh"

EX_OK=0
EX_USAGE=2
EX_NO_LOCK=4
EX_LOCAL=6
EX_SUBSTITUTED=7
EX_IDENTITY=8
EX_TRANSFER=9
EX_INSTALL=10

ROUTER="${BENCH_ROUTER_IP:-192.168.1.1}"
APK=""
EXPECTED_SHA=""
STAGE_NAME=""
PURPOSE="bench-deploy-apk"
TASK="-"
VERIFY_ONLY=0
DRY_RUN=0
REFUSE_FOREIGN=0
CLEAR_PACKAGE_PATH=0
INSTALL_TIMEOUT="${BENCH_INSTALL_TIMEOUT:-300}"
R_TMP="${BENCH_ROUTER_TMP:-/tmp}"
KNOWN_HOSTS="${BENCH_KNOWN_HOSTS:-$HOME/.hermes/state/bench-mt3000.known_hosts}"
RUN_ID="$(date +%Y%m%dT%H%M%S)-$$"

log()  { printf 'bench-deploy: %s\n' "$*"; }
warn() { printf 'bench-deploy: WARNING: %s\n' "$*" >&2; }
die()  { local rc="$1"; shift; printf 'bench-deploy: %s\n' "$*" >&2; exit "$rc"; }

usage() { sed -n '3,60p' "$0" | sed 's/^# \{0,1\}//' >&2; exit "$EX_USAGE"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --apk) APK="$2"; shift 2 ;;
    --sha256) EXPECTED_SHA="$2"; shift 2 ;;
    --name) STAGE_NAME="$2"; shift 2 ;;
    --router) ROUTER="$2"; shift 2 ;;
    --purpose) PURPOSE="$2"; shift 2 ;;
    --task) TASK="$2"; shift 2 ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --refuse-foreign-staged) REFUSE_FOREIGN=1; shift ;;
    --clear-package-path) CLEAR_PACKAGE_PATH=1; shift ;;
    --install-timeout) INSTALL_TIMEOUT="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) die "$EX_USAGE" "unknown option '$1' (see --help)" ;;
  esac
done

# ---------------------------------------------------------------- 0. the lock, first

[ -x "$LOCK" ] || die "$EX_LOCAL" "missing $LOCK (the bench lock is mandatory)"
"$LOCK" require || die "$EX_NO_LOCK" "refusing to touch the bench: the lock gate said no (exit $?)."

# ---------------------------------------------------------------- 1. name the artifact

[ -n "$APK" ] || die "$EX_USAGE" "--apk FILE is mandatory: a deploy must name its artifact"
[ -n "$EXPECTED_SHA" ] || die "$EX_USAGE" "--sha256 HEX is mandatory: a deploy must name the sha256 it expects to install"
[ -f "$APK" ] || die "$EX_LOCAL" "no such artifact: $APK"
[ -n "$STAGE_NAME" ] || STAGE_NAME="$(basename "$APK")"

APK="$(cd "$(dirname "$APK")" && pwd)/$(basename "$APK")"
APK_SIZE="$(wc -c < "$APK" | tr -d ' ')"
APK_SHA="$(sha256sum "$APK" | cut -d' ' -f1)"
[ "$APK_SHA" = "$EXPECTED_SHA" ] || die "$EX_LOCAL" \
  "artifact identity mismatch: $APK hashes to $APK_SHA, but --sha256 named $EXPECTED_SHA"
log "ARTIFACT name=$STAGE_NAME path=$APK size=$APK_SIZE sha256=$APK_SHA"

# payload identity: the bytes the router must end up running
PAYLOAD_SHA=""; PAYLOAD_SIZE=""
extract_payload() {
  local dest="$1" static=""
  for cand in "${BENCH_APK_STATIC:-}" "$HOME/.cache/apk-v3/apk.static" "$(command -v apk.static 2>/dev/null || true)"; do
    [ -n "$cand" ] && [ -x "$cand" ] && { static="$cand"; break; }
  done
  [ -n "$static" ] || return 1
  mkdir -p "$dest" || return 1
  "$static" extract --allow-untrusted --destination "$dest" "$APK" >/dev/null 2>&1 || return 1
  [ -f "$dest/usr/bin/tollgate-wrt" ] || return 1
  printf '%s %s\n' "$(sha256sum "$dest/usr/bin/tollgate-wrt" | cut -d' ' -f1)" \
                   "$(wc -c < "$dest/usr/bin/tollgate-wrt" | tr -d ' ')"
}

PAYLOAD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bench-payload.XXXXXX")"
trap 'rm -rf "$PAYLOAD_DIR"' EXIT
PAYLOAD_INFO="$(extract_payload "$PAYLOAD_DIR")" || die "$EX_LOCAL" \
  "cannot extract usr/bin/tollgate-wrt from $APK (need apk.static: set BENCH_APK_STATIC, or warm ~/.cache/apk-v3 via the tollgate-development skill's scripts/extract-openwrt-apk-v3.sh). FAIL-CLOSED: not deploying an unverified artifact."
PAYLOAD_SHA="${PAYLOAD_INFO%% *}"
PAYLOAD_SIZE="${PAYLOAD_INFO##* }"
log "PAYLOAD usr/bin/tollgate-wrt size=$PAYLOAD_SIZE sha256=$PAYLOAD_SHA"

# ---------------------------------------------------------------- 2. transport

PW_FILE="${BENCH_ROUTER_PW_FILE:-$HOME/.tg-e2e/pw}"
if [ -s "$PW_FILE" ]; then
  SSHPASS=(sshpass -f "$PW_FILE")
elif [ -n "${BENCH_ROUTER_PASSWORD:-}" ]; then
  warn "using \$BENCH_ROUTER_PASSWORD from the environment (prefer BENCH_ROUTER_PW_FILE: argv/env leak)"
  SSHPASS=(sshpass -p "$BENCH_ROUTER_PASSWORD")
else
  die "$EX_LOCAL" "no bench credential: set BENCH_ROUTER_PW_FILE (default ~/.tg-e2e/pw) or BENCH_ROUTER_PASSWORD"
fi

SSH_OPTS=(-o ControlMaster=no -o ControlPath=none -o StrictHostKeyChecking=accept-new
          -o UserKnownHostsFile="$KNOWN_HOSTS" -o LogLevel=ERROR -o ConnectTimeout=8)
mkdir -p "$(dirname "$KNOWN_HOSTS")" 2>/dev/null || true

RRUN_TMP="$(mktemp -d "${TMPDIR:-/tmp}/bench-rrun.XXXXXX")"
trap 'rm -rf "$PAYLOAD_DIR" "$RRUN_TMP"' EXIT

# push a local script with scp -O (dropbear has no sftp-server) and run it: ONE ssh per stage
rscript() {   # $1=script file  $2=remote name ; rest = args
  local src="$1" name="$2"; shift 2
  "${SSHPASS[@]}" scp "${SSH_OPTS[@]}" -O -q "$src" "root@$ROUTER:$R_TMP/$name" \
    || die "$EX_TRANSFER" "scp -O of $name failed (router untouched)"
  # shellcheck disable=SC2029
  "${SSHPASS[@]}" ssh "${SSH_OPTS[@]}" "root@$ROUTER" "sh $R_TMP/$name $*"
}

rput() {   # $1=script file  $2=remote name : push only (no execution)
  local src="$1" name="$2"
  "${SSHPASS[@]}" scp "${SSH_OPTS[@]}" -O -q "$src" "root@$ROUTER:$R_TMP/$name" \
    || die "$EX_TRANSFER" "scp -O of $name failed (router untouched)"
}

rssh() {   # single short remote command, no quotes
  "${SSHPASS[@]}" ssh "${SSH_OPTS[@]}" "root@$ROUTER" "$@"
}

field() { printf '%s\n' "$1" | sed -n "s/^${2}=//p" | head -1; }

# ---------------------------------------------------------------- 3. remote scripts

cat > "$RRUN_TMP/preflight.sh" <<'EOS'
TG_TMP="${TG_TMP:-/tmp}"; TG_BIN_DIR="${TG_BIN:-/usr/bin}"; TG_ETC="${TG_ETC:-/etc/tollgate}"; TG_APKLOG="${TG_APKLOG:-/var/log/apk.log}"
BIN="$TG_BIN_DIR/tollgate-wrt"
echo "TG_VERSION=$(apk info -v 2>/dev/null | sed -n 's/^\(tollgate-wrt-[^ ]*\).*/\1/p' | head -1)"
if [ -f "$BIN" ]; then
  echo "TG_BIN_SHA=$(sha256sum "$BIN" 2>/dev/null | cut -d' ' -f1)"
  echo "TG_BIN_SIZE=$(wc -c < "$BIN" 2>/dev/null | tr -d ' ')"
else
  echo "TG_BIN_SHA=none"; echo "TG_BIN_SIZE=0"
fi
echo "TG_APKLOG_INSTALLS=$(grep -c 'Running .apk add' "$TG_APKLOG" 2>/dev/null | head -1)"
if [ -f "$TG_ETC/install.json" ]; then
  echo "TG_PACKAGE_PATH=$(sed -n 's/.*"package_path"[ ]*:[ ]*"\([^"]*\)".*/\1/p' "$TG_ETC/install.json" | head -1)"
else
  echo "TG_PACKAGE_PATH="
fi
for f in "$TG_TMP"/*.apk; do
  [ -e "$f" ] || continue
  echo "TG_STAGED=$(basename "$f")|$(wc -c < "$f" | tr -d ' ')|$(sha256sum "$f" | cut -d' ' -f1)"
done
echo "TG_PREFLIGHT_DONE"
EOS

cat > "$RRUN_TMP/rotate.sh" <<'EOS'
TG_TMP="${TG_TMP:-/tmp}"; TS="$1"; shift
for f in "$@"; do
  if [ -e "$TG_TMP/$f" ]; then
    mv "$TG_TMP/$f" "$TG_TMP/$f.rotated-$TS" && echo "ROTATED $f -> $f.rotated-$TS"
  fi
done
echo "ROTATE_DONE"
EOS

cat > "$RRUN_TMP/verify-stage.sh" <<'EOS'
TG_TMP="${TG_TMP:-/tmp}"; N="$1"; SHA="$2"; SZ="$3"
P="$TG_TMP/$N"
[ -f "$P" ] || { echo "STAGE_MISSING"; exit 1; }
s=$(wc -c < "$P" | tr -d ' '); h=$(sha256sum "$P" | cut -d' ' -f1)
echo "STAGE_SIZE=$s"; echo "STAGE_SHA=$h"
if [ "$h" = "$SHA" ] && [ "$s" = "$SZ" ]; then echo "STAGE_OK"; exit 0; fi
echo "STAGE_MISMATCH"; exit 1
EOS

cat > "$RRUN_TMP/clear-package-path.sh" <<'EOS'
TG_ETC="${TG_ETC:-/etc/tollgate}"; BAK="$1"
F="$TG_ETC/install.json"
[ -f "$F" ] || { echo "NO_INSTALL_JSON"; exit 0; }
cp "$F" "$F.bak-$BAK" 2>/dev/null || true
sed -i 's#"package_path"[ ]*:[ ]*"[^"]*"#"package_path": "false"#' "$F"
echo "CLEARED_PACKAGE_PATH"
sed -n 's/.*"package_path".*/PACKAGE_PATH_LINE=&/p' "$F"
EOS

cat > "$RRUN_TMP/poll-log.sh" <<'EOS'
TG_TMP="${TG_TMP:-/tmp}"; L="$TG_TMP/$1"
[ -f "$L" ] || { echo "LOG_MISSING"; exit 1; }
if grep -q '^DONE$' "$L"; then echo "LOG_DONE"; else echo "LOG_PENDING"; fi
tail -n 12 "$L"
EOS

cat > "$RRUN_TMP/verify-identity.sh" <<'EOS'
TG_TMP="${TG_TMP:-/tmp}"; TG_BIN_DIR="${TG_BIN:-/usr/bin}"; TG_APKLOG="${TG_APKLOG:-/var/log/apk.log}"
EXP_SHA="$1"; EXP_SIZE="$2"
BIN="$TG_BIN_DIR/tollgate-wrt"
[ -f "$BIN" ] || { echo "BIN_SHA=none"; echo "BIN_MISSING"; exit 1; }
h=$(sha256sum "$BIN" | cut -d' ' -f1); s=$(wc -c < "$BIN" | tr -d ' ')
echo "BIN_SHA=$h"; echo "BIN_SIZE=$s"
echo "BIN_VERSION=$(apk info -v 2>/dev/null | sed -n 's/^\(tollgate-wrt-[^ ]*\).*/\1/p' | head -1)"
echo "TG_APKLOG_INSTALLS=$(grep -c 'Running .apk add' "$TG_APKLOG" 2>/dev/null | head -1)"
echo "TG_APKLOG_TAIL=$(grep -a 'apk add' "$TG_APKLOG" 2>/dev/null | tail -2 | tr '\n' ';')"
# Secondary evidence only. `$BIN` is a TARGET-ARCH binary: never let the shell fall back to
# interpreting it as a script (POSIX sh does that on ENOEXEC, and a 12 MB binary read as
# shell code can hang the whole deploy — measured in the offline harness). `timeout` is a
# real program, so its execve just fails cleanly; if the applet is absent we skip instead.
if [ -x "$BIN" ]; then
  if command -v timeout >/dev/null 2>&1; then
    CLI_OUT="$(timeout 5 "$BIN" version 2>&1 | head -3)"
    if [ -n "$CLI_OUT" ]; then printf '%s\n' "$CLI_OUT" | sed 's/^/BIN_CLI=/'; else echo "BIN_CLI=<no version output>"; fi
  else
    echo "BIN_CLI=<skipped: no timeout applet>"
  fi
fi
if [ "$h" = "$EXP_SHA" ] && [ "$s" = "$EXP_SIZE" ]; then echo "IDENTITY_MATCH"; else echo "IDENTITY_MISMATCH"; fi
EOS

# ---------------------------------------------------------------- 4. preflight (read-only)

PRE="$(rscript "$RRUN_TMP/preflight.sh" "bench-preflight-$RUN_ID.sh")" \
  || die "$EX_TRANSFER" "router preflight failed (router untouched)"
printf '%s\n' "$PRE" | sed 's/^/bench-deploy: preflight: /'
printf '%s\n' "$PRE" | grep -q '^TG_PREFLIGHT_DONE$' \
  || die "$EX_TRANSFER" "router preflight did not complete; router untouched"

CUR_SHA="$(field "$PRE" TG_BIN_SHA)"
CUR_VERSION="$(field "$PRE" TG_VERSION)"
APKLOG_BEFORE="$(field "$PRE" TG_APKLOG_INSTALLS)"; APKLOG_BEFORE="${APKLOG_BEFORE:-0}"
log "ROUTER installed=${CUR_VERSION:-none} installed_sha256=${CUR_SHA:-none}"

# ---------------------------------------------------------------- 5. substitution / rotation

STAGED_BLOCK="$(printf '%s\n' "$PRE" | sed -n 's/^TG_STAGED=//p')"
REUSE=0
TO_ROTATE=()
while IFS='|' read -r sname ssize ssha; do
  [ -n "${sname:-}" ] || continue
  if [ "$sname" = "$STAGE_NAME" ]; then
    if [ "$ssha" = "$EXPECTED_SHA" ]; then
      log "STAGED-REUSE $sname already on the router and hash-matched (size=$ssize sha256=$ssha)"
      REUSE=1
    else
      die "$EX_SUBSTITUTED" "SUBSTITUTED ARTIFACT REFUSED: an apk is already staged as '$sname' on the router but its bytes are NOT the artifact this deploy named (staged sha256=$ssha size=$ssize vs named sha256=$EXPECTED_SHA size=$APK_SIZE). Nothing was installed. Reconcile or delete the staged file, then re-run."
    fi
  else
    if [ "$REFUSE_FOREIGN" = 1 ]; then
      die "$EX_SUBSTITUTED" "FOREIGN STAGED APK REFUSED (--refuse-foreign-staged): $sname (size=$ssize sha256=$ssha) is staged on the router. A leftover /tmp/*.apk re-installed the wrong build on this bench on 2026-09-24 (19:50/20:05/20:10). Remove it deliberately, then re-run."
    fi
    TO_ROTATE+=("$sname")
  fi
done <<< "$STAGED_BLOCK"

STAGED_PATH=""
if [ "$REUSE" = 1 ]; then
  STAGED_PATH="$R_TMP/$STAGE_NAME"
else
  STAGED_PATH="$R_TMP/bench-${EXPECTED_SHA:0:12}-$STAGE_NAME"
fi

PPATH="$(field "$PRE" TG_PACKAGE_PATH)"
if [ -n "$PPATH" ] && [ "$PPATH" != "false" ]; then
  if [ "$CLEAR_PACKAGE_PATH" = 1 ]; then
    warn "install.json package_path='$PPATH' is a REVERT BOMB (the shipped /usr/bin/check_package_path re-installs it). Clearing it as requested (--clear-package-path)."
  elif [ "$VERIFY_ONLY" = 1 ]; then
    warn "install.json package_path='$PPATH' is a REVERT BOMB; verify-only does not clear it (use --clear-package-path when you deploy)."
  else
    die "$EX_SUBSTITUTED" "REFUSED: /etc/tollgate/install.json package_path='$PPATH' points at an apk. That is the revert bomb that silently re-installed the bench on 2026-09-24. Re-run with --clear-package-path to neutralise it as part of this deploy."
  fi
fi

if [ "$VERIFY_ONLY" != 1 ] && [ "${#TO_ROTATE[@]}" -gt 0 ]; then
  log "ROTATING ${#TO_ROTATE[@]} stale staged apk(s) aside (never deleted): ${TO_ROTATE[*]}"
  ROT="$(rscript "$RRUN_TMP/rotate.sh" "bench-rotate-$RUN_ID.sh" "$RUN_ID" "${TO_ROTATE[@]}")" \
    || die "$EX_TRANSFER" "rotation failed; nothing installed"
  printf '%s\n' "$ROT" | sed 's/^/bench-deploy: /'
  printf '%s\n' "$ROT" | grep -q '^ROTATE_DONE$' || die "$EX_TRANSFER" "rotation did not complete; nothing installed"
fi

if [ "$CLEAR_PACKAGE_PATH" = 1 ] && [ -n "$PPATH" ] && [ "$PPATH" != "false" ]; then
  rscript "$RRUN_TMP/clear-package-path.sh" "bench-clear-pp-$RUN_ID.sh" "$RUN_ID" \
    | sed 's/^/bench-deploy: /'
fi

# ---------------------------------------------------------------- 6. verify-only / stage

if [ "$VERIFY_ONLY" = 1 ]; then
  log "VERIFY-ONLY: no rotation, no upload, no install. Comparing the router's installed binary with the named artifact."
  V="$(rscript "$RRUN_TMP/verify-identity.sh" "bench-identity-$RUN_ID.sh" "$PAYLOAD_SHA" "$PAYLOAD_SIZE")" \
    || die "$EX_TRANSFER" "identity read-back failed"
  printf '%s\n' "$V" | sed 's/^/bench-deploy: /'
  RSHA="$(field "$V" BIN_SHA)"
  if printf '%s\n' "$V" | grep -q '^IDENTITY_MATCH$'; then
    log "IDENTITY VERIFIED (verify-only) router_sha256=$RSHA artifact_payload_sha256=$PAYLOAD_SHA"
    exit "$EX_OK"
  fi
  die "$EX_IDENTITY" "IDENTITY MISMATCH (verify-only): the router runs $RSHA but the named artifact '$STAGE_NAME' carries $PAYLOAD_SHA. The bench is NOT running the build this deploy names. Nothing was installed."
fi

if [ "$REUSE" != 1 ]; then
  log "STAGING $STAGE_NAME -> $STAGED_PATH (scp -O)"
  "${SSHPASS[@]}" scp "${SSH_OPTS[@]}" -O -q "$APK" "root@$ROUTER:$STAGED_PATH" \
    || die "$EX_TRANSFER" "transfer failed; router untouched"
  S="$(rscript "$RRUN_TMP/verify-stage.sh" "bench-verify-stage-$RUN_ID.sh" "$(basename "$STAGED_PATH")" "$EXPECTED_SHA" "$APK_SIZE")" \
    || die "$EX_TRANSFER" "staged file verification failed; router untouched"
  printf '%s\n' "$S" | sed 's/^/bench-deploy: /'
  printf '%s\n' "$S" | grep -q '^STAGE_OK$' \
    || die "$EX_TRANSFER" "staged bytes on the router do not match the artifact; nothing installed"
fi
log "STAGED-VERIFIED $STAGED_PATH sha256=$EXPECTED_SHA"

if [ "$DRY_RUN" = 1 ]; then
  log "DRY-RUN: would now 'apk add --allow-untrusted $STAGED_PATH' and then verify the installed binary against $PAYLOAD_SHA. Stopping."
  exit "$EX_OK"
fi

# ---------------------------------------------------------------- 7. install (detached, polled)

INST_LOG="bench-install-$RUN_ID.log"
cat > "$RRUN_TMP/install.sh" <<EOS
#!/bin/sh
TG_TMP="\${TG_TMP:-/tmp}"
exec > "\$TG_TMP/$INST_LOG" 2>&1
echo "BEGIN \$(date)"
apk add --allow-untrusted "\$TG_TMP/$(basename "$STAGED_PATH")"
RC=\$?
echo "APK_RC=\$RC"
echo "EXIT=\$RC"
echo "DONE"
EOS

rput "$RRUN_TMP/install.sh" "bench-install-$RUN_ID.sh"
# BusyBox here has no nohup; setsid survives the postinst's network/wifi restarts.
# The `trap '' HUP` matters: this ssh runs under sshpass, which allocates a pty, and when
# it exits the pty closes -> the session's process group takes SIGHUP. Measured in the
# offline harness: `setsid CMD &` alone was killed before it ever ran, leaving no log at
# all. Ignoring HUP in the forking shell (inherited across exec) closes that window.
rssh "trap '' HUP; setsid /bin/sh $R_TMP/bench-install-$RUN_ID.sh </dev/null >/dev/null 2>&1 & echo LAUNCHED" \
  | sed 's/^/bench-deploy: install: /' | grep -q 'LAUNCHED' \
  || die "$EX_INSTALL" "install could not be launched detached"

log "INSTALL launched (detached); polling $R_TMP/$INST_LOG for DONE (timeout ${INSTALL_TIMEOUT}s)"
DEADLINE=$(( $(date +%s) + INSTALL_TIMEOUT ))
POLL=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  POLL="$(rscript "$RRUN_TMP/poll-log.sh" "bench-poll-$RUN_ID.sh" "$INST_LOG" || true)"
  printf '%s\n' "$POLL" | grep -q '^LOG_DONE$' && break
  sleep 5
done
printf '%s\n' "$POLL" | grep -q '^LOG_DONE$' \
  || die "$EX_INSTALL" "install did not report DONE within ${INSTALL_TIMEOUT}s (see $R_TMP/$INST_LOG on the router)"
printf '%s\n' "$POLL" | sed 's/^/bench-deploy: install-log: /'
APK_RC="$(field "$POLL" APK_RC)"

# ---------------------------------------------------------------- 8. identity read-back

V="$(rscript "$RRUN_TMP/verify-identity.sh" "bench-identity-$RUN_ID.sh" "$PAYLOAD_SHA" "$PAYLOAD_SIZE")" \
  || die "$EX_TRANSFER" "identity read-back failed after install"
printf '%s\n' "$V" | sed 's/^/bench-deploy: /'
RSHA="$(field "$V" BIN_SHA)"
APKLOG_AFTER="$(field "$V" TG_APKLOG_INSTALLS)"; APKLOG_AFTER="${APKLOG_AFTER:-0}"

if printf '%s\n' "$V" | grep -q '^IDENTITY_MISMATCH$'; then
  die "$EX_IDENTITY" "IDENTITY MISMATCH - LOUD FAILURE: the router now runs $RSHA but the artifact this deploy named carries $PAYLOAD_SHA (size router=$(field "$V" BIN_SIZE) vs artifact=$PAYLOAD_SIZE). Another install happened (apk.log installs before=$APKLOG_BEFORE after=$APKLOG_AFTER; tail: $(field "$V" TG_APKLOG_TAIL)). Do not report this bench as ready."
fi

log "INSTALLED VERIFIED router_sha256=$RSHA artifact_payload_sha256=$PAYLOAD_SHA size=$PAYLOAD_SIZE package=$(field "$V" BIN_VERSION)"
[ -n "$(field "$V" BIN_CLI)" ] && log "CLI identity: $(field "$V" BIN_CLI)"
if [ "${APK_RC:-0}" != "0" ]; then
  warn "apk returned APK_RC=$APK_RC yet the installed bytes match the artifact; treat the package's own exit code as suspicious, not the identity."
fi
if [ "$APKLOG_AFTER" != "$APKLOG_BEFORE" ]; then
  log "apk.log install counter moved $APKLOG_BEFORE -> $APKLOG_AFTER (this deploy's own install; a jump of more than 1 means an EXTERNAL install happened)"
fi
log "Handover rule: re-verify immediately before telling anyone the bench is ready (bench-deploy-apk.sh --verify-only --apk ... --sha256 ...)."
exit "$EX_OK"
