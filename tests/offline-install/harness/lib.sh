#!/usr/bin/env bash
# Shared helpers for tests/offline-install/run-tests.sh.
#
# NOTHING here touches a router or a network.  The "router" is a throw-away
# directory ($TGOFFLINE_HARNESS_ROOT) and ssh/scp/apk/uci/nft/curl are PATH doubles
# in harness/bin/.  The production script TEXT runs unmodified:
#   scripts/offline/install-offline.sh  (laptop driver, shipped as the bundle entry point)
#   scripts/offline/install-router.sh   (router-side ordered sequence)
# The doubles only translate the router-side filesystem/commands into that directory.
#
# Precedent: tests/mt3000-bench/run-tests.sh (the bench-lock suite in
# physical-router-test-automation) runs production script text against a fake root
# with PATH doubles, exactly like this.
set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HARNESS_DIR/../../.." && pwd)"
SCRIPTS_DIR="${TGOFFLINE_HARNESS_SCRIPTS:-$REPO_ROOT/scripts/offline}"
BUNDLE_MAKER="$HARNESS_DIR/make-bundle.sh"

TESTS_RUN=0
TESTS_FAILED=0
ONLY="${TGOFFLINE_HARNESS_ONLY:-}"
_CUR="(no test)"

t_begin() { _CUR="$1"; TESTS_RUN=$((TESTS_RUN + 1)); printf '\n== %s %s\n' "$(printf '%02d' "$TESTS_RUN")" "$1"; }
pass() { printf '   ok   - %s\n' "$1"; }
fail() { TESTS_FAILED=$((TESTS_FAILED + 1)); printf '   FAIL - %s\n' "$1"; }

check_rc() { # $1 desc, $2 expected rc, $3 actual rc
    if [ "$2" = "$3" ]; then pass "$1 (rc=$3)"; else
        fail "$1: expected rc=$2, got rc=$3"
    fi
}
check_contains() { # $1 desc, $2 needle, $3 haystack
    if printf '%s' "$3" | grep -qF -- "$2"; then pass "$1"; else
        fail "$1: output does not contain '$2'"
        printf '        --- output was ---\n%s\n        ------------------\n' "$3"
    fi
}
check_not_contains() { # $1 desc, $2 needle, $3 haystack
    if printf '%s' "$3" | grep -qF -- "$2"; then
        fail "$1: output unexpectedly contains '$2'"
        printf '        --- output was ---\n%s\n        ------------------\n' "$3"
    else pass "$1"; fi
}
check_eq() { # $1 desc, $2 expected, $3 actual
    if [ "$2" = "$3" ]; then pass "$1"; else fail "$1: expected '$2', got '$3'"; fi
}

# ------------------------------------------------------------------ fixture router
router_root_new() { # [$1=feature...]  features: no-iptables ssh-down
    local root="$WORK/router" feat
    rm -rf "$root"
    mkdir -p "$root/tmp" "$root/etc/apk/repositories.d" "$root/etc/config" \
             "$root/etc/tollgate" "$root/etc/uci-defaults" "$root/etc/nftables.d" \
             "$root/var/lib/apk" "$root/var/log" "$root/var/lib/tgoffline-harness" \
             "$root/usr/bin" "$root/usr/sbin" "$root/usr/lib" "$root/proc/net" "$root/bin"
    cat > "$root/etc/openwrt_release" <<'EOF'
DISTRIB_ID='OpenWrt'
DISTRIB_RELEASE='25.12.5'
DISTRIB_DESCRIPTION='OpenWrt 25.12.5 r00000-0000000000'
DISTRIB_ARCH='aarch64_cortex-a53'
DISTRIB_TARGET='mediatek/filogic'
EOF
    # the feeds a stock 25.x image configures; their index caches are ABSENT (a
    # WAN-less router has never run `apk update`) — this is what makes
    # --force-missing-repositories load-bearing, and the apk double models exactly it
    cat > "$root/etc/apk/repositories.d/packages.list" <<'EOF'
https://downloads.openwrt.org/releases/25.12.5/packages/aarch64_cortex-a53/packages/packages.adb
https://downloads.openwrt.org/releases/25.12.5/packages/aarch64_cortex-a53/routing/packages.adb
EOF
    # the WAN-less precondition the card asks to PROVE rather than assume
    : > "$root/var/lib/tgoffline-harness/no-uplink"
    # /proc/net/tcp with :22 (0x0016) LISTENing — what `ssh_listening` parses
    cat > "$root/proc/net/tcp" <<'EOF'
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 12345 1 0000000000000000 100 0 0 10 0
   1: 00000000:0055 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 12346 1 0000000000000000 100 0 0 10 0
EOF
    # iptables comes from the BASE IMAGE (fw4's dependency chain), not the bundle;
    # nodogsplash execs `iptables --version` at start-up
    cat > "$root/usr/sbin/iptables" <<'EOF'
#!/bin/sh
echo "iptables v1.8.11 (nf_tables)"
EOF
    chmod +x "$root/usr/sbin/iptables"
    # the installed DB of a freshly flashed image: the base image's own packages and
    # NOTHING the bundle ships.  apk resolves a transaction from the files NAMED on the
    # command line plus this DB (apk-tools 3, --no-network, no feed index to fall back
    # on) — so what is in here is exactly what decides whether a WAN-less install can
    # resolve.  A fresh flash has libc/libgcc/kernel; it has no nodogsplash, no jq and
    # no iptables-nft, which is why the dependency stage has to be handed the closure.
    cat > "$root/var/lib/apk/installed" <<'EOF'
libc 1.2.5-r5
libgcc 14.3.0-r4
kernel 6.12.60-r1
EOF
    # the HTTP surface model the curl double reads (port → code).  8090/8443 are
    # derived from the guard state on purpose (see harness/bin/curl).
    cat > "$root/var/lib/tgoffline-harness/http_codes" <<'EOF'
2051 200
2050 200
2121 200
8080 307
EOF
    for feat in "$@"; do
        case "$feat" in
            no-iptables) rm -f "$root/usr/sbin/iptables" ;;
            ssh-down) grep -v ':0016' "$root/proc/net/tcp" > "$root/proc/net/tcp.new" && mv "$root/proc/net/tcp.new" "$root/proc/net/tcp" ;;
        esac
    done
    export TGOFFLINE_HARNESS_ROOT="$root"
}

router_feature() { # $1=feature  — flip a feature on the CURRENT root
    case "$1" in
        no-bolt11) : > "$TGOFFLINE_HARNESS_ROOT/var/lib/tgoffline-harness/no-bolt11" ;;
        uplink) rm -f "$TGOFFLINE_HARNESS_ROOT/var/lib/tgoffline-harness/no-uplink" ;;
        8080-200) sed -i 's/^8080 307$/8080 200/' "$TGOFFLINE_HARNESS_ROOT/var/lib/tgoffline-harness/http_codes" ;;
        no-guard-state) rm -f "$TGOFFLINE_HARNESS_ROOT/var/lib/tgoffline-harness/nft_admin_board_input_guard" ;;
        # the SAME box after a previous install: the packages the dependency stage does
        # not offer (nodogsplash's iptables closure) are already in the DB, so the
        # transaction resolves from here.  This is the shape that hid the fresh-box
        # defect for three waves of bench installs (T20's upgrade-box control).
        upgrade-box)
            cat > "$TGOFFLINE_HARNESS_ROOT/var/lib/apk/installed" <<'EOF'
libc 1.2.5-r5
libgcc 14.3.0-r4
kernel 6.12.60-r1
iptables-nft 1.8.10-r3
xtables-nft 1.8.10-r3
libxtables 1.8.10-r3
iptables-mod-conntrack-extra 1.8.10-r3
iptables-mod-ipopt 1.8.10-r3
iptables-mod-nat-extra 1.8.10-r3
EOF
            ;;
        # make `apk add` fail with a KNOWN non-zero rc, so the rc the installer reports
        # can be compared with the rc apk actually returned (3 unless overridden)
        apk-add-fails) printf '3\n' > "$TGOFFLINE_HARNESS_ROOT/var/lib/tgoffline-harness/apk_add_fail" ;;
        apk-add-fails=*) printf '%s\n' "${1#apk-add-fails=}" > "$TGOFFLINE_HARNESS_ROOT/var/lib/tgoffline-harness/apk_add_fail" ;;
    esac
}

# ------------------------------------------------------------------ router state probes
installed_pkgs() { sed -e 's/^/ /' "$TGOFFLINE_HARNESS_ROOT/var/lib/apk/installed" 2>/dev/null; }
installed_names() { awk '{print $1}' "$TGOFFLINE_HARNESS_ROOT/var/lib/apk/installed" 2>/dev/null | sort | tr '\n' ' '; }
apk_add_count() {
    local log="$TGOFFLINE_HARNESS_ROOT/var/log/apk.log"
    if [ -f "$log" ]; then grep -c 'Running .apk add' "$log" 2>/dev/null | head -1; else echo 0; fi
}
apk_log() { cat "$TGOFFLINE_HARNESS_ROOT/var/log/apk.log" 2>/dev/null; }
# the .apk basenames the FIRST `apk add` invocation was handed — the dependency stage.
# The double logs every invocation (`apk add: … files: <path> <path> …`) BEFORE it
# resolves, so a REFUSED transaction is still recorded and "what was offered" is
# assertable.  The dependency stage is always invocation #1 (stage 2 runs before the
# package stage), and apk_add_count() counts only COMPLETED invocations.
deps_offered_apks() {
    sed -n 's/^apk add: .* files: //p' "$TGOFFLINE_HARNESS_ROOT/var/log/apk.log" 2>/dev/null \
        | head -1 | tr ' ' '\n' | while read -r p; do [ -n "$p" ] && basename "$p"; done | sort
}
installed_names_sorted() {
    awk '{print $1}' "$TGOFFLINE_HARNESS_ROOT/var/lib/apk/installed" 2>/dev/null | sort | tr '\n' ' '
}
nft_log() { cat "$TGOFFLINE_HARNESS_ROOT/var/log/nft.log" 2>/dev/null; }
ssh_log() { cat "${TGOFFLINE_HARNESS_SSH_LOG:-}" 2>/dev/null; }
scp_log() { cat "${TGOFFLINE_HARNESS_SCP_LOG:-}" 2>/dev/null; }

# ------------------------------------------------------------------ bundle fixture
bundle_build() { # $1=destdir  rest=variants (see make-bundle.sh)
    local dest="$1"; shift
    sh "$BUNDLE_MAKER" "$dest" "$@" >/dev/null || { echo "make-bundle failed" >&2; return 1; }
    printf '%s' "$dest"
}

# ------------------------------------------------------------------ run the driver
run_install() { # $1=bundle  rest=extra args ; sets OUT RC
    local bundle="$1"; shift
    OUT="$( "$bundle/install-offline.sh" 192.168.1.1 'harness-router-pw' \
            --trust-mac 'AA:BB:CC:DD:EE:FF' --report "$WORK/report.json" "$@" 2>&1 )"
    RC=$?
    return 0
}

run_remote_script() { # $1=staging dir (HOST path) ; rest=extra args  (direct router-side test)
    local stage="$1" rstage; shift
    # The script runs ON the router, so the path it is *handed* must be the router's
    # own.  The caller builds the fixture under $TGOFFLINE_HARNESS_ROOT, so strip that
    # root here — the driver stages /tmp/tgoffline and passes exactly that, and
    # install-router.sh prepends its own root.  (Passing the host-absolute path makes
    # the prefix land twice and every path gate fail for the wrong reason.)
    rstage="$stage"
    case "$stage" in
        "$TGOFFLINE_HARNESS_ROOT"/*) rstage="${stage#"$TGOFFLINE_HARNESS_ROOT"}" ;;
    esac
    # shellcheck disable=SC2086  # SH_BIN may be "busybox ash"
    OUT="$( TGOFFLINE_ROOT="$TGOFFLINE_HARNESS_ROOT" \
            PATH="$TGOFFLINE_HARNESS_ROOT/usr/sbin:$TGOFFLINE_HARNESS_ROOT/usr/bin:$HARNESS_DIR/bin:/usr/bin:/bin" \
            $SH_BIN "$SCRIPTS_DIR/install-router.sh" --staging-dir "$rstage" \
            --trust-mac 'AA:BB:CC:DD:EE:FF' "$@" 2>&1 )"
    RC=$?
    return 0
}

report_json() { cat "$WORK/report.json" 2>/dev/null; }

# the report file carries the JSON between TGOFFLINE-REPORT-BEGIN/END markers (so a
# consumer can stream it); report.py parses exactly that block
report_field() { # $1=json key
    python3 "$HARNESS_DIR/report.py" "$WORK/report.json" get "$1" 2>/dev/null
}
report_remote_field() { # $1=json key of the embedded router-side report
    python3 "$HARNESS_DIR/report.py" "$WORK/report.json" remote-get "$1" 2>/dev/null
}

summary() {
    printf '\n==== %s: tests=%s failed=%s ====\n' "$(basename "$0")" "$TESTS_RUN" "$TESTS_FAILED"
    [ "$TESTS_FAILED" -eq 0 ] || return 1
    return 0
}
