#!/usr/bin/env bash
# Shared helpers for the offline bench-harness tests (tests/mt3000-bench/run-tests.sh).
#
# Nothing here touches a router: the harness is a throw-away directory that stands in for
# the router's filesystem, plus PATH doubles for ssh/scp/apk (harness/bin/).
set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HARNESS_DIR/../../.." && pwd)"
BENCH_DIR="$REPO_ROOT/scripts/mt3000-bench"
BENCH_LOCK="$BENCH_DIR/bench-lock.sh"
BENCH_WITH_LOCK="$BENCH_DIR/bench-with-lock.sh"
BENCH_DEPLOY="$BENCH_DIR/bench-deploy-apk.sh"
ROUTER_TOUCHER="$HARNESS_DIR/router-touching-script.sh"

TESTS_RUN=0; TESTS_FAILED=0; TESTS_SKIPPED=0
_CUR="(no test)"

t_begin() { _CUR="$1"; TESTS_RUN=$((TESTS_RUN + 1)); printf '\n== %s %s\n' "$(printf '%02d' "$TESTS_RUN")" "$1"; }
pass() { printf '   ok   - %s\n' "$1"; }
fail() { TESTS_FAILED=$((TESTS_FAILED + 1)); printf '   FAIL - %s\n' "$1"; }
skip() { TESTS_SKIPPED=$((TESTS_SKIPPED + 1)); printf '   SKIP - %s\n' "$1"; }

check_rc() {   # $1 desc, $2 expected rc, $3 actual rc
  if [ "$2" = "$3" ]; then pass "$1 (rc=$3)"; else fail "$1: expected rc=$2, got rc=$3"; fi
}

check_contains() {   # $1 desc, $2 needle, $3 haystack
  if printf '%s' "$3" | grep -qF -- "$2"; then pass "$1"; else
    fail "$1: output does not contain '$2'"
    printf '        --- output was ---\n%s\n        ------------------\n' "$3"
  fi
}

check_not_contains() {   # $1 desc, $2 needle, $3 haystack
  if printf '%s' "$3" | grep -qF -- "$2"; then
    fail "$1: output unexpectedly contains '$2'"
    printf '        --- output was ---\n%s\n        ------------------\n' "$3"
  else pass "$1"; fi
}

check_eq() {   # $1 desc, $2 expected, $3 actual
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1: expected '$2', got '$3'"; fi
}

run_cmd() {   # OUT=..., RC=...
  OUT="$("$@" 2>&1)"; RC=$?
}

# ---------------------------------------------------------------- harness "router"

harness_root_new() {   # $1=dir [$2=installed payload] [$3=pkg version]
  local d="$1" payload="${2:-}" ver="${3:-tollgate-wrt-0.6.0_alpha9-r0}"
  rm -rf "$d"
  mkdir -p "$d/tmp" "$d/usr/bin" "$d/etc/tollgate" "$d/var/log" "$d/var/lib/apk" "$d/payload-map" "$d/bin"
  cp -f "$HARNESS_DIR/bin/apk" "$d/bin/apk"; chmod +x "$d/bin/apk"
  if [ -n "$payload" ]; then install -D -m 0755 "$payload" "$d/usr/bin/tollgate-wrt"; fi
  printf '%s\n' "$ver" > "$d/var/lib/apk/installed"
  : > "$d/var/log/apk.log"
  printf '{\n  "package_path": "false",\n  "version": "1"\n}\n' > "$d/etc/tollgate/install.json"
}

harness_payload_map() {   # $1=apk  $2=payload file  -> teach the stand-in apk the mapping
  local h; h="$(sha256sum "$1" | cut -d' ' -f1)"
  printf '%s\n' "$2" > "$BENCH_HARNESS_ROOT/payload-map/$h"
}

harness_installed_sha() { sha256sum "$BENCH_HARNESS_ROOT/usr/bin/tollgate-wrt" 2>/dev/null | cut -d' ' -f1; }
harness_apk_installs() { grep -c 'Running .apk add' "$BENCH_HARNESS_ROOT/var/log/apk.log" 2>/dev/null | head -1; }
harness_installed_pkg() { head -1 "$BENCH_HARNESS_ROOT/var/lib/apk/installed" 2>/dev/null; }

apk_static_find() {
  local c
  for c in "${BENCH_APK_STATIC:-}" "$HOME/.cache/apk-v3/apk.static" "$(command -v apk.static 2>/dev/null || true)"; do
    [ -n "$c" ] && [ -x "$c" ] && { printf '%s' "$c"; return 0; }
  done
  return 1
}

payload_extract() {   # $1=apk  $2=out file
  local apk="$1" out="$2" static dest
  static="$(apk_static_find)" || return 1
  dest="$(mktemp -d)"
  "$static" extract --allow-untrusted --destination "$dest" "$apk" >/dev/null 2>&1 || { rm -rf "$dest"; return 1; }
  [ -f "$dest/usr/bin/tollgate-wrt" ] || { rm -rf "$dest"; return 1; }
  cp -f "$dest/usr/bin/tollgate-wrt" "$out"
  rm -rf "$dest"
  return 0
}

lock_holder_line() { head -1 "$BENCH_LOCK_PATH" 2>/dev/null; }
lock_kill_all() {   # end any holder this suite started
  BENCH_PROFILE="$BENCH_PROFILE" "$BENCH_LOCK" release --force >/dev/null 2>&1 || true
  BENCH_PROFILE="$BENCH_PROFILE" "$BENCH_LOCK" release --force >/dev/null 2>&1 || true
}

summary() {
  printf '\n==== %s: tests=%s failed=%s skipped=%s ====\n' "$(basename "$0")" "$TESTS_RUN" "$TESTS_FAILED" "$TESTS_SKIPPED"
  [ "$TESTS_FAILED" -eq 0 ] || return 1
  return 0
}
