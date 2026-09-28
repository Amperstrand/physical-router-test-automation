#!/bin/sh
# =============================================================================
# scripts/offline/install-router.sh
#
# The ROUTER-side half of the WAN-less (offline) install path.  It is a bundle
# member: install-offline.sh (the laptop-side driver) stages it to the router
# through an `ssh 'cat > …'` stdin redirect — never scp, because `scp -O` fails
# on a fresh dropbear (no sftp-server) — and runs it with the router's own shell.
#
# ORDER IS THE DELIVERABLE.  Steps, in this order and no other:
#
#   1. seed + APPLY /etc/uci-defaults/99z-mgmt-keepalive (workstation MAC →
#      nodogsplash trustedmac, plus `allow tcp port 22`) and assert the trust is
#      LIVE — before anything can start enforcement.  This ordering is not
#      optional and the script fails closed without it: nodogsplash turns
#      `users_to_router` into its pre-auth ruleset once, at start-up, so a
#      package install/upgrade with no pre-auth SSH path locks the wired
#      management workstation out.  That wedge bricked the bench twice
#      (2026-08-16/17).
#   2. `apk add --no-network --allow-untrusted --force-missing-repositories` on the
#      dependency files BY PATH.  --force-missing-repositories is required, not
#      cosmetic: without it apk aborts on every missing feed index even with
#      --no-network (a WAN-less router has no indexes at all).
#   3. `apk add` the tollgate-wrt package (its postinst applies the policy).
#   4. assert, fail closed, and print a machine-readable report.
#
# Every check is a gate with a state (pass/fail/info) recorded in the report, and
# every refusal names what is wrong and what to do about it.
#
# The br-lan-client refusal of :8090/:8443 is NOT asserted here — a router cannot
# test its own br-lan drops (locally generated traffic to a local address never
# traverses the input hook with iifname br-lan).  It is asserted by the driver,
# from the workstation that is actually on br-lan, and merged into the final
# report.
#
# ACCEPTANCE IS CONDITIONAL ON THE UPLINK.  A fresh-flash box has no WAN, so a
# BOLT11 quote is impossible by construction; the quote gate therefore asserts
# GRACEFUL DEGRADATION there (the /ln-invoice route must answer, in a documented
# degraded shape, without wedging) and says so explicitly in the report
# (`gate_bolt11_quote=info`, `fact_bolt11_assertable=no`) instead of failing a
# correct install.  With an uplink the original strict contract applies, unchanged.
# Surface probes use the path each service ACTUALLY serves (see SURFACE_CONTRACT):
# the bare `/` is not the portal's page.
#
# Exit codes (identical in install-offline.sh):
#   0  pass
#   2  usage
#   4  a declared dependency is missing from the bundle
#   5  keepalive/lockout risk — refusing to install
#   6  staged file does not match its manifest entry (name/substitution)
#   7  an apk install failed
#   8  a dependency has no runtime payload (empty stub) — nodogsplash would crash-loop
#   9  a post-install assertion failed
# =============================================================================

TGOFFLINE_VERSION="1.0.0"

# Test seam (never set on a real router): a harness root that stands in for the
# router's filesystem.  Empty ⇒ real absolute paths.  Path doubles in
# tests/offline-install/harness/bin export the same variable.
R="${TGOFFLINE_ROOT:-}"

STAGE=/tmp/tgoffline
WORK=""
MANIFEST=""
TRUST_MAC=""
KEEPALIVE=""
ROUTER_MODEL=""
ROUTER_VERSION=""

GUARD_PATH="/etc/nftables.d/31-admin-board-not-guest-reachable.nft"
GUARD_CHAIN="admin_board_input_guard"
BACKEND_PORT=2121

# --- dependency contract -------------------------------------------------------
# What the package under test needs, and how the bundle is allowed to satisfy it.
#   REQUIRED_DEPS  : the bundle MUST carry a real package for each of these.
#                    (feed recipe `DEPENDS:=+nodogsplash +jq` plus nodogsplash's own
#                    non-base-image dependency; a WAN-less router has no feed
#                    indexes, so nothing can be resolved remotely.)
#   STUB_OK_DEPS   : stale VIRTUAL deps that MAY be satisfied by an empty spec-only
#                    package.  OpenWrt 25.12 folded pthreads into musl libc while
#                    nodogsplash's feed metadata still demands libpthread, so a stub
#                    is the correct answer here and an absent one is not an error.
#   RUNTIME_FILES  : deps' payloads that must actually LAND on the router.  An empty
#                    stub that satisfies the resolver leaves these missing.
REQUIRED_DEPS="nodogsplash jq libmicrohttpd-no-ssl"
STUB_OK_DEPS="libpthread"

runtime_required() {
    # dep|kind|spec   (kind=file supports a glob; kind=cmd must exit 0)
    cat <<'EOF'
nodogsplash|file|/usr/bin/nodogsplash
jq|file|/usr/bin/jq
libmicrohttpd-no-ssl|file|/usr/lib/libmicrohttpd.so*
iptables-nft|cmd|iptables --version
EOF
}

# --- gate bookkeeping ----------------------------------------------------------
GATES=""
FACTS=""

gate() { # gate <state> <name> <detail>
    printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$GATES"
}
gate_pass() { gate pass "$1" "$2"; }
gate_fail() { gate fail "$1" "$2"; }
gate_info() { gate info "$1" "$2"; }
fact() { # fact <key> <value>
    printf '%s\t%s\n' "$1" "$2" >> "$FACTS"
}
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

usage() {
    cat <<'EOF'
usage: sh install-router.sh --staging-dir DIR --trust-mac MAC [options]

Runs ON the router (staged there by install-offline.sh).  See the file header for
the ordered, no-brick sequence and the exit codes.

options:
  --staging-dir DIR     where the driver staged the bundle (pkgs/, MANIFEST.sha256,
                        99z-mgmt-keepalive, install-router.sh)
  --trust-mac MAC       management workstation MAC to trust pre-auth
  --keepalive PATH      staged keepalive seed (default: <staging-dir>/99z-mgmt-keepalive)
  --manifest PATH       staged MANIFEST.sha256 (default: <staging-dir>/MANIFEST.sha256)
  --pkg-dir DIR         staged apks (default: <staging-dir>/pkgs)
  --report PATH         write the machine-readable report here too
  --version             print the script version and exit
EOF
}

report_and_exit() { # report_and_exit <exit-code>
    rc="$1"
    result=FAIL
    [ "$rc" = 0 ] && result=PASS
    echo ""
    echo "=== offline install report (router side) — v$TGOFFLINE_VERSION ==="
    if [ -f "$GATES" ]; then
        while IFS="$(printf '\t')" read -r state name detail; do
            case "$state" in
                pass) tag=PASS ;;
                fail) tag=FAIL ;;
                *) tag=INFO ;;
            esac
            printf 'gate %-30s %-4s %s\n' "$name" "$tag" "$detail"
        done < "$GATES"
    fi
    echo "TGOFFLINE-REPORT-BEGIN"
    printf '{\n'
    printf ' "installer_version": "%s",\n' "$TGOFFLINE_VERSION"
    printf ' "side": "router",\n'
    if [ -f "$FACTS" ]; then
        while IFS="$(printf '\t')" read -r key value; do
            printf ' "fact_%s": "%s",\n' "$key" "$(json_escape "$value")"
        done < "$FACTS"
    fi
    if [ -f "$GATES" ]; then
        while IFS="$(printf '\t')" read -r state name detail; do
            printf ' "gate_%s": "%s",\n' "$name" "$state"
        done < "$GATES"
    fi
    printf ' "result": "%s"\n}\n' "$result"
    echo "TGOFFLINE-REPORT-END"
    echo "TGOFFLINE-RESULT $result"
    if [ -n "$REPORT" ]; then
        {
            echo "TGOFFLINE-REPORT-BEGIN"
            printf '{\n'
            printf ' "installer_version": "%s",\n' "$TGOFFLINE_VERSION"
            printf ' "side": "router",\n'
            if [ -f "$FACTS" ]; then
                while IFS="$(printf '\t')" read -r key value; do
                    printf ' "fact_%s": "%s",\n' "$key" "$(json_escape "$value")"
                done < "$FACTS"
            fi
            if [ -f "$GATES" ]; then
                while IFS="$(printf '\t')" read -r state name detail; do
                    printf ' "gate_%s": "%s",\n' "$name" "$state"
                done < "$GATES"
            fi
            printf ' "result": "%s"\n}\n' "$result"
            echo "TGOFFLINE-REPORT-END"
        } > "$REPORT"
    fi
    exit "$rc"
}

fail_now() { # fail_now <exit-code> <message...>
    rc="$1"
    shift
    echo "REFUSED($rc): $*" >&2
    echo "REFUSED($rc): $*"
    report_and_exit "$rc"
}

sha256_of() { # sha256_of <path>  -> hex
    sha256sum "$1" 2>/dev/null | awk '{print $1}' | head -1
}

manifest_lookup() { # manifest_lookup <basename> -> sha256 or empty
    [ -f "$MANIFEST" ] || return 1
    awk -v want="$1" '
        { f=$2; sub(/^\*/, "", f); n=f; sub(/^.*\//, "", n) }
        n == want { print $1; exit }
    ' "$MANIFEST"
}

installed_version() { apk info -v 2>/dev/null | grep -i '^tollgate-wrt' | head -1; }
ssh_listening() {
    port_hex=$(printf '%04X' 22)
    [ -f "$R/proc/net/tcp" ] || return 1
    awk -v p=":$port_hex" 'NR > 1 && $2 ~ p"$" && $4 == "0A" { found = 1 } END { exit !found }' \
        "$R/proc/net/tcp"
}

# --- WAN availability: the ONE input that decides which payment contract applies -----
# A BOLT11 quote is minted by a REMOTE mint that is addressed by NAME, so it needs an
# uplink.  A fresh-flash WAN-less GL-MT3000 has none, and the 2026-09-28 hardware run is
# the evidence: `logread` showed the mint (kashu.me) failing DNS and `merchant is still
# initializing`, and the endpoint never produced a quote.  Demanding one unconditionally
# made a CORRECT WAN-less install fail its own acceptance gates (exit 9).
#
# The probe is router-local, does no package/HTTP I/O, and mirrors the failure the
# hardware actually showed (`kashu.me` had no address): a quote needs the mint's NAME to
# resolve, so it needs BOTH
#   * a default route — the kernel's own answer to "is there an uplink"
#     (/proc/net/route; busybox `ip route` is not guaranteed on a minimal image), and
#   * a nameserver to resolve with — read as FILES, deliberately: BusyBox ash resolves
#     `nslookup`/`ping` as INTERNAL applets and ignores PATH, so a command-based DNS probe
#     is neither harness-drivable nor safe to read as evidence.
# A route with no resolver is NOT an uplink for quoting purposes.
#
# AMBIGUITY IS NOT AN ESCAPE HATCH.  The WAN-LESS branch is taken only on POSITIVE
# evidence — the route table was read and holds no default route, or it holds one and the
# box has no resolver.  If the route table cannot be read at all the box is judged
# WAN-PRESENT, so the STRICT quote contract applies: you cannot make the payment
# assertion disappear by blinding the probe (T28 pins exactly that).
# Sets WAN_ROUTE (yes|no|unknown) / WAN_RESOLVER (yes|no|n/a) and WAN_DIAG;
# returns 0 iff a quote is reachable.
resolvers_configured() {
    for f in "$R/tmp/resolv.conf.d/resolv.conf.auto" "$R/tmp/resolv.conf" "$R/etc/resolv.conf"; do
        [ -f "$f" ] || continue
        grep -q '^[[:space:]]*nameserver[[:space:]]' "$f" 2>/dev/null && return 0
    done
    return 1
}

wan_probe() {
    WAN_ROUTE=unknown
    WAN_RESOLVER=n/a
    WAN_DIAG=""
    if [ -f "$R/proc/net/route" ]; then
        if awk '$2 == "00000000" && $1 != "lo" { found = 1 } END { exit !found }' "$R/proc/net/route"; then
            WAN_ROUTE=yes
        else
            WAN_ROUTE=no
            WAN_DIAG="no default route in /proc/net/route"
        fi
    else
        WAN_DIAG="/proc/net/route is unreadable — cannot prove the box is WAN-less"
    fi
    if [ "$WAN_ROUTE" = yes ]; then
        if resolvers_configured; then
            WAN_RESOLVER=yes
        else
            WAN_RESOLVER=no
            WAN_DIAG="a default route exists but no nameserver is configured (the mint is addressed by name)"
        fi
    fi
    # strict unless positively disproven
    [ "$WAN_ROUTE" = yes ] || [ "$WAN_ROUTE" = unknown ] || return 1
    [ "$WAN_RESOLVER" != no ] || return 1
    return 0
}

# --- the surface contract ------------------------------------------------------------
# The path each surface ACTUALLY serves, and the answers that are CORRECT for it.  Every
# entry here is evidence, not taste (2026-09-28 WAN-less GL-MT3000 run + the repo's own
# scripts/tollgate-port-sweep.sh and lib/install_paths.py):
#   :2121 /            200       tollgate backend API
#   :2050 /splash.html 200       nodogsplash serves its splash at /splash.html; the bare
#                                `/` the gate used to probe is 404 on this artifact.
#                                `/` 200 is accepted as the FALLBACK portal shape (two
#                                in-repo sources document it), but only when the splash
#                                path itself is not 200 — so a box serving NEITHER still
#                                fails, which is the control T24 pins.
#   :2051 /            200|403   uhttpd captive-portal site: no index -> 403 (the
#                                documented answer in scripts/tollgate-port-sweep.sh);
#                                200 accepted for a site that ships an index
#   :8080 /            307|000   LuCI with redirect_https: the plain-HTTP listener either
#                                answers the redirect (307) or nothing at all (000) — it
#                                was 000 on the hardware run, so a plain 307 may NOT be
#                                required.  Any OTHER answer (e.g. 200) means the admin UI
#                                is served over plain HTTP without its TLS redirect, and
#                                that still FAILS.
SURFACE_CONTRACT='2121|/|200|tollgate backend API
2050|/splash.html|200|nodogsplash portal splash
2051|/|200 403|uhttpd captive-portal site
8080|/|307 000|LuCI admin over plain HTTP (redirect_https)'

# The portal's documented alternates: path -> accepted code.  Consulted ONLY when the
# primary probe found neither an accepted code nor a refusal, so a box that serves none of
# them still fails.  Both shapes are documented in this repo (`scripts/tollgate-port-sweep.sh`
# and `lib/install_paths.py` say `:2050 /` -> 200) and one is what the hardware served
# (`/splash.html` -> 200).
SURFACE_ALTERNATES='2050|/|200'

# --- the BOLT11 contract's degraded class -------------------------------------------
# The HTTP answers that mean "the payment path is unavailable/not ready" rather than
# "the endpoint is misrouted or broken".  Anything outside this set on a WAN-less box
# (000 no answer, 3xx redirect, 400/405/415/422 a rejected or absent route) still FAILS.
DEGRADED_INVOICE_CODES="200 202 402 500 501 502 503 504"

word_in() { # word_in <word> <space-separated list>
    for w in $2; do [ "$w" = "$1" ] && return 0; done
    return 1
}

# --- bounded wait for a surface that is still coming up ----------------------------
# A WAN-less install starts the portal and the backend as part of the install, and the
# 2026-09-28 hardware run probed them before they had bound: every surface answered 000
# and the run failed "wrong status" on a box that was merely still starting (a re-probe
# of the SAME box minutes later was 200/200/403/200).  So a probe now waits BOUNDEDLY for
# a port that has not answered yet, while still failing a port that answers WRONG at once
# — a wrong answer is a defect, not startup lag — and never waiting on a port whose
# correct answer IS 000 (the admin-board pair must stay refused, and LuCI may answer no
# plain HTTP at all).  TGOFFLINE_PROBE_DEADLINE=0 disables the wait; the no-hardware
# suite pins it to 0 so its negative controls stay instant.
SURFACE_PROBE_DEADLINE="${TGOFFLINE_PROBE_DEADLINE:-45}"
SURFACE_PROBE_INTERVAL=2

probe_code() { # probe_code <accepted-codes> <curl args...>  -> prints the HTTP code
    probe_accept="$1"; shift
    probe_waited=0
    while :; do
        probe_out="$(curl -s -o /dev/null -w '%{http_code}' "$@" 2>/dev/null)"
        [ -n "$probe_out" ] || probe_out=000
        if word_in "$probe_out" "$probe_accept" || [ "$probe_out" != 000 ]; then
            printf '%s' "$probe_out"
            return 0
        fi
        if [ "$probe_waited" -ge "$SURFACE_PROBE_DEADLINE" ]; then
            printf '%s' "$probe_out"
            return 0
        fi
        sleep "$SURFACE_PROBE_INTERVAL"
        probe_waited=$((probe_waited + SURFACE_PROBE_INTERVAL))
    done
}

# ------------------------------------------------------------------ argument parsing
while [ $# -gt 0 ]; do
    case "$1" in
        --staging-dir) STAGE="$2"; shift 2 ;;
        --work-dir) WORK="$2"; shift 2 ;;
        --trust-mac) TRUST_MAC="$2"; shift 2 ;;
        --keepalive) KEEPALIVE="$2"; shift 2 ;;
        --manifest) MANIFEST="$2"; shift 2 ;;
        --pkg-dir) PKG_DIR="$2"; shift 2 ;;
        --report) REPORT="$2"; shift 2 ;;
        --version) echo "$TGOFFLINE_VERSION"; exit 0 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ -n "$STAGE" ] || { usage >&2; echo "--staging-dir is required" >&2; exit 2; }
[ -n "$TRUST_MAC" ] || { usage >&2; echo "--trust-mac is required" >&2; exit 2; }
# The driver passes router-side absolute paths (/tmp/tgoffline).  Prefix them with the
# harness root when one is set, so the whole sequence can run against a throw-away
# router root under tests/offline-install (TGOFFLINE_ROOT is never set on a router).
rpath() { case "$1" in /*) printf '%s%s' "$R" "$1" ;; *) printf '%s' "$1" ;; esac; }
STAGE="$(rpath "$STAGE")"
[ -n "$KEEPALIVE" ] && KEEPALIVE="$(rpath "$KEEPALIVE")"
[ -n "$MANIFEST" ] && MANIFEST="$(rpath "$MANIFEST")"
[ -n "${PKG_DIR:-}" ] && PKG_DIR="$(rpath "$PKG_DIR")"
[ -n "${REPORT:-}" ] && REPORT="$(rpath "$REPORT")"
[ -n "$KEEPALIVE" ] || KEEPALIVE="$STAGE/99z-mgmt-keepalive"
[ -n "$MANIFEST" ] || MANIFEST="$STAGE/MANIFEST.sha256"
[ -n "${PKG_DIR:-}" ] || PKG_DIR="$STAGE/pkgs"
[ -n "$WORK" ] || WORK="$STAGE/work"
REPORT="${REPORT:-$STAGE/report.json}"

mkdir -p "$WORK" 2>/dev/null || true
GATES="$WORK/gates"; : > "$GATES"
FACTS="$WORK/facts"; : > "$FACTS"

echo "=== offline install (router side) v$TGOFFLINE_VERSION ==="
echo "staging-dir=$STAGE  pkg-dir=$PKG_DIR  trust-mac=$TRUST_MAC"

# ------------------------------------------------------------------ environment
if [ -f "$R/etc/openwrt_release" ]; then
    # shellcheck disable=SC1091
    . "$R/etc/openwrt_release" 2>/dev/null || true
    ROUTER_MODEL="${DISTRIB_DESCRIPTION:-unknown}"
    ROUTER_VERSION="${DISTRIB_RELEASE:-unknown}"
fi
fact router_model "${ROUTER_MODEL:-unknown}"
fact router_openwrt "${ROUTER_VERSION:-unknown}"
fact trust_mac "$TRUST_MAC"

if ! command -v apk >/dev/null 2>&1; then
    gate_fail apk_tool "no apk on PATH — this is the OpenWrt >= 25.x (apk) lane"
    fail_now 7 "no apk on PATH: the offline bundle lane is apk-only (OpenWrt >= 25.0). The .ipk/opkg lane is explicitly out of scope."
fi
fact apk_version "$(apk --version 2>&1 | head -1)"

if [ ! -d "$PKG_DIR" ]; then
    gate_fail bundle_pkgs "no staged package directory at $PKG_DIR"
    fail_now 6 "no staged package directory at $PKG_DIR (the driver stages pkgs/ there)"
fi
if [ ! -f "$MANIFEST" ]; then
    gate_fail manifest_present "no MANIFEST.sha256 staged at $MANIFEST"
    fail_now 6 "no MANIFEST.sha256 staged at $MANIFEST — refusing to install bytes that are not covered by the bundle's manifest"
fi

# =============================================================== 1. KEEPALIVE FIRST
echo ""
echo "=== (1) management keepalive — seed + APPLY before anything can enforce ==="
if [ ! -f "$KEEPALIVE" ]; then
    gate_fail keepalive_seeded "not staged at $KEEPALIVE"
    fail_now 5 "refusing to install: the management keepalive seed is missing from the bundle ($KEEPALIVE). Without it the wired management workstation loses SSH the moment nodogsplash enforces on br-lan — that wedge bricked the bench on 2026-08-16/17. Rebuild the bundle with scripts/offline/templates/99z-mgmt-keepalive and re-run."
fi
if ! grep -q 'trustedmac' "$KEEPALIVE" || ! grep -q 'port 22' "$KEEPALIVE"; then
    gate_fail keepalive_seeded "seed does not carry both the trustedmac and the 'allow tcp port 22' fragments"
    fail_now 5 "refusing to install: $KEEPALIVE does not add both the workstation MAC to nodogsplash trustedmac and 'allow tcp port 22' to users_to_router. A partial keepalive still locks the operator out — the Aug 16/17 wedge."
fi
if grep -q '__TRUST_MAC__' "$KEEPALIVE"; then
    gate_fail keepalive_seeded "seed still carries the __TRUST_MAC__ placeholder"
    fail_now 5 "refusing to install: the keepalive seed still has the __TRUST_MAC__ placeholder, so no MAC would be trusted and the operator would be locked out."
fi
if ! grep -q "$TRUST_MAC" "$KEEPALIVE"; then
    gate_fail keepalive_seeded "seed does not carry the workstation MAC $TRUST_MAC"
    fail_now 5 "refusing to install: the keepalive seed does not carry $TRUST_MAC (the workstation on br-lan), so applying it would not bypass enforcement for the operator."
fi

mkdir -p "$R/etc/uci-defaults" 2>/dev/null || true
cp "$KEEPALIVE" "$R/etc/uci-defaults/99z-mgmt-keepalive" || \
    fail_now 5 "could not seed $R/etc/uci-defaults/99z-mgmt-keepalive"
chmod +x "$R/etc/uci-defaults/99z-mgmt-keepalive" 2>/dev/null || true
gate_pass keepalive_seeded "seeded $R/etc/uci-defaults/99z-mgmt-keepalive (re-applies every boot, after 99-tollgate-setup)"
fact keepalive_seed "$KEEPALIVE"

assert_keepalive_live() { # returns 0 when the trust is actually in the running config
    tm="$(uci -q get nodogsplash.@nodogsplash[0].trustedmac 2>/dev/null || echo "")"
    utr="$(uci -q get nodogsplash.@nodogsplash[0].users_to_router 2>/dev/null || echo "")"
    echo "$tm" | grep -q "$TRUST_MAC" || return 1
    echo "$utr" | grep -q 'port 22' || return 1
    return 0
}

sh "$R/etc/uci-defaults/99z-mgmt-keepalive" || true
if ! assert_keepalive_live; then
    gate_fail keepalive_applied "trustedmac / 'allow tcp port 22' not visible in the nodogsplash config after applying the seed"
    fail_now 5 "refusing to install: the keepalive seed applied but the pre-auth trust is NOT live (nodogsplash trustedmac lacks $TRUST_MAC, or users_to_router lacks 'allow tcp port 22'). Installing now can lock the operator out of SSH the moment nodogsplash enforces on br-lan — the Aug 16/17 wedge. Fix the seed, then re-run."
fi
gate_pass keepalive_applied "trustedmac carries $TRUST_MAC; users_to_router allows tcp port 22 pre-auth"
fact keepalive_uci "$(uci -q show nodogsplash 2>/dev/null | grep -E 'trustedmac|users_to_router' | tr '\n' ' ')"

# =============================================================== name + closure gates
echo ""
echo "=== (1b) bundle closure + staged-file binding ==="
STAGED_APKS="$(find "$PKG_DIR" -name '*.apk' -type f 2>/dev/null | sort)"
if [ -z "$STAGED_APKS" ]; then
    gate_fail bundle_closure "no .apk files staged under $PKG_DIR"
    fail_now 4 "no .apk files staged under $PKG_DIR — the bundle carries no packages."
fi

# (a) the required dependency closure must be present by name.
#     Evaluated FIRST, and used as the exit code when both gates fail: a renamed or
#     swapped dependency surfaces here as the thing that actually matters (the router
#     would come up without it, and nothing on a WAN-less box can install it later),
#     with the binding gate's "not named in the manifest" as the supporting detail.
missing_deps=""
for dep in $REQUIRED_DEPS; do
    found=""
    for f in $STAGED_APKS; do
        case "$(basename "$f")" in
            "$dep-"*) found="$(basename "$f")" ;;
        esac
        [ -n "$found" ] && break
    done
    if [ -z "$found" ]; then
        missing_deps="$missing_deps $dep"
    fi
done
if [ -n "$missing_deps" ]; then
    gate_fail bundle_closure "missing dependency package(s):$missing_deps"
    echo "REFUSED(4): the bundle does not carry a package for the declared dependency:$missing_deps — the package declares DEPENDS:=+nodogsplash +jq and a WAN-less router has no feed indexes to resolve them from. The bundle must carry the full closure (rebuild it with the feed's build-offline-bundle)."
fi

# (e) every staged apk must be named by the manifest, with matching bytes
binding_ok=1
for f in $STAGED_APKS; do
    n="$(basename "$f")"
    want="$(manifest_lookup "$n")"
    if [ -z "$want" ]; then
        binding_ok=0
        gate_fail staged_binding "$n is not named in MANIFEST.sha256"
        echo "REFUSED(6): staged apk '$n' is not named in $MANIFEST. The installer installs only what the bundle's manifest covers, by name — a package staged under a different name is not the package the manifest (and the signed SHA256SUMS) attests to."
    else
        got="$(sha256_of "$f")"
        if [ "$got" != "$want" ]; then
            binding_ok=0
            gate_fail staged_binding "$n sha256=$got != manifest=$want"
            echo "REFUSED(6): staged apk '$n' does not match its manifest entry (staged sha256=$got, manifest=$want). Substituted bytes are refused before anything is installed."
        fi
    fi
done
[ "$binding_ok" = 1 ] && gate_pass staged_binding "every staged .apk is named in MANIFEST.sha256 with matching bytes"

# One refusal, built from whichever gate(s) fired. The closure gap wins the exit code
# (4 over 6) because it names the consequence; both diagnostics are already printed.
if [ -n "$missing_deps" ]; then
    fail_now 4 "refusing to install: no package in the bundle satisfies the declared dependency:$missing_deps, so the router would come up without it and a WAN-less box cannot fetch it afterwards."
fi
if [ "$binding_ok" != 1 ]; then
    refusals="$(grep -cE '^fail[[:space:]]+staged_binding' "$GATES" 2>/dev/null)"
    [ -n "$refusals" ] || refusals=0
    fail_now 6 "refusing to install: $refusals staged package(s) do not match MANIFEST.sha256 (see the gate lines above for the names)."
fi
for dep in $STUB_OK_DEPS; do
    present="no"
    for f in $STAGED_APKS; do
        case "$(basename "$f")" in "$dep-"*) present="yes" ;; esac
    done
    gate_info stub_dep "$dep: present=$present (stale virtual dep — may be an empty stub, absence is not fatal)"
done
# shellcheck disable=SC2086  # deliberate word splitting: one path per line, no spaces
staged_count="$(printf '%s\n' $STAGED_APKS | wc -l | tr -d ' ')"
gate_pass bundle_closure "required deps present:$REQUIRED_DEPS ($staged_count staged package(s))"
fact staged_apks "$(for f in $STAGED_APKS; do basename "$f"; done | tr '\n' ' ')"

# pick the package under test
PKG_APK=""
for f in $STAGED_APKS; do
    case "$(basename "$f")" in tollgate-wrt_*.apk) PKG_APK="$f" ;; esac
done
if [ -z "$PKG_APK" ]; then
    gate_fail package_present "no tollgate-wrt_*.apk staged"
    fail_now 7 "no tollgate-wrt_*.apk staged under $PKG_DIR — nothing to install."
fi
APK_NAME="$(basename "$PKG_APK")"
APK_SHA="$(sha256_of "$PKG_APK")"
APK_STEM="$(printf '%s' "$APK_NAME" | sed -E 's/^tollgate-wrt_//; s/_(aarch64|arm|mips64|mipsel|mips|x86_64|x86|i386|riscv64|powerpc)[a-z0-9_.-]*\.apk$//')"
fact artifact "$APK_NAME"
fact artifact_sha256 "$APK_SHA"
fact artifact_version_stem "$APK_STEM"
echo "package under test: $APK_NAME (sha256=$APK_SHA)"

# =============================================================== 2. deps by path
echo ""
echo "=== (2) dependency packages (BY PATH, --no-network --allow-untrusted --force-missing-repositories) ==="
# --- full-closure offer (added by the offline bundle builder) ----------------
# apk-tools 3 resolves a transaction from the files NAMED here plus the installed
# DB, and from nothing else. With --no-network there is no feed index to fall
# back on, so a dependency of a named package that is NOT itself named is
# `(no such package)` and apk refuses the whole transaction — which is how a
# fresh box got `REFUSED(7): the offline dependency install failed.` while the
# bundle carried every package it needed. Naming only REQUIRED_DEPS+STUB_OK_DEPS
# requires each of THEM to be base-image-complete, and nodogsplash is not
# (iptables-nft, iptables-mod-conntrack-extra, iptables-mod-ipopt,
# iptables-mod-nat-extra). An upgrade box already had those installed, so only a
# fresh flash ever saw it. Offer the WHOLE staged closure in one transaction; the
# package under test is excluded on purpose because stage (3) installs it on its
# own, after the keepalive assertion, so the no-brick ordering is unchanged.
dep_files=""
for f in $STAGED_APKS; do
    if [ "$f" != "$PKG_APK" ]; then
        dep_files="$dep_files $f"
    fi
done
# shellcheck disable=SC2086
apk_deps_cmd="apk add --no-network --allow-untrusted --force-missing-repositories$dep_files"
fact apk_deps_cmd "$apk_deps_cmd"
# shellcheck disable=SC2086  # the dependency files must be passed BY PATH, one arg each
echo "+ $apk_deps_cmd"
# `$?` read inside `if ! cmd; then` is the NEGATION's status (0), so this gate
# once reported a REFUSED(7) as "failed rc=0". Capture apk's own status and
# report that; the verdict and the fail-closed behaviour are unchanged.
apk_deps_rc=0
apk add --no-network --allow-untrusted --force-missing-repositories $dep_files || apk_deps_rc=$?
if [ "$apk_deps_rc" != 0 ]; then
    gate_fail deps_installed "apk add of the dependency files failed rc=$apk_deps_rc"
    fail_now 7 "the offline dependency install failed. On a WAN-less router this is usually a missing --force-missing-repositories, a package missing from the bundle's closure, or a package built for another arch."
fi
gate_pass deps_installed "installed:$REQUIRED_DEPS (stubs:$STUB_OK_DEPS)"

# =============================================================== 2b. runtime payloads
echo ""
echo "=== (2b) runtime payload gate (an empty stub must not pass for a runtime dep) ==="
runtime_bad=""
runtime_required | while IFS='|' read -r dep kind spec; do
    [ -n "$dep" ] || continue
    case "$kind" in
        file)
            # shellcheck disable=SC2086
            if ! ls $R$spec >/dev/null 2>&1; then
                printf 'fail\truntime_payload\t%s: no payload at %s\n' "$dep" "$spec" >> "$GATES"
            fi
            ;;
        cmd)
            if ! ( eval "$spec" ) >/dev/null 2>&1; then
                printf 'fail\truntime_payload\t%s: `%s` does not run (exit != 0)\n' "$dep" "$spec" >> "$GATES"
            fi
            ;;
    esac
done
runtime_bad="$(grep -cE '^fail[[:space:]]+runtime_payload' "$GATES" 2>/dev/null)"
[ -n "$runtime_bad" ] || runtime_bad=0
if [ "$runtime_bad" != 0 ]; then
    detail="$(
        while IFS="$(printf '\t')" read -r state name rest; do
            [ "$state" = fail ] && [ "$name" = runtime_payload ] && printf '%s; ' "$rest"
        done < "$GATES"
    )"
    fail_now 8 "a dependency was satisfied by an empty stub and its runtime payload is missing: $detail. nodogsplash 5.0.2 execs \`iptables --version\` at startup and links libmicrohttpd, so an empty spec-only package makes it crash-loop (\"Cannot get iptables version.\"). Only stale VIRTUAL deps (libpthread) may be stubbed — the bundle must carry the real .apk for every runtime dependency."
fi
gate_pass runtime_payload "all runtime payloads present: $(runtime_required | awk -F'|' '{print $1"->"$3}' | tr '\n' ' ')"

# =============================================================== 3. package
echo ""
echo "=== (3) tollgate-wrt package (postinst applies the policy) ==="
assert_keepalive_live || {
    sh "$R/etc/uci-defaults/99z-mgmt-keepalive" || true
    assert_keepalive_live || {
        gate_fail keepalive_live "trust lost after the dependency install"
        fail_now 5 "refusing to install the package: the pre-auth keepalive was lost while installing the dependencies and re-applying it did not restore it. nodogsplash trustedmac / users_to_router would not trust this workstation — installing now risks the Aug 16/17 lockout."
    }
    gate_info keepalive_live "re-applied after the dependency install"
}
gate_pass keepalive_live "trust live immediately before the package install"

# same `$?`-inside-`if !` accounting as the dependency stage above.
apk_pkg_rc=0
apk add --no-network --allow-untrusted --force-missing-repositories "$PKG_APK" || apk_pkg_rc=$?
if [ "$apk_pkg_rc" != 0 ]; then
    gate_fail package_installed "apk add $APK_NAME failed rc=$apk_pkg_rc"
    fail_now 7 "installing $APK_NAME failed."
fi
gate_pass package_installed "installed $APK_NAME"

# =============================================================== 4. assertions
echo ""
echo "=== (4) post-install assertions (fail closed) ==="

# payload identity — the installed binary must be the artifact's own payload
installed_bin="$R/usr/bin/tollgate-wrt"
installed_sha="$(sha256_of "$installed_bin")"
extract_dir="$WORK/extract"
mkdir -p "$extract_dir" 2>/dev/null || true
artifact_sha=""
if apk extract --allow-untrusted --destination "$extract_dir" "$PKG_APK" >/dev/null 2>&1; then
    artifact_sha="$(sha256_of "$extract_dir/usr/bin/tollgate-wrt")"
fi
fact installed_payload_sha256 "$installed_sha"
fact artifact_payload_sha256 "$artifact_sha"
if [ -z "$installed_sha" ]; then
    gate_fail payload_sha256 "$installed_bin is missing"
elif [ -z "$artifact_sha" ]; then
    gate_fail payload_sha256 "could not derive the payload hash from $APK_NAME (apk extract failed on the router)"
elif [ "$installed_sha" != "$artifact_sha" ]; then
    gate_fail payload_sha256 "installed=$installed_sha artifact=$artifact_sha"
else
    gate_pass payload_sha256 "installed $installed_sha == payload of $APK_NAME ($APK_SHA per MANIFEST.sha256)"
fi

ver="$(installed_version)"
fact installed_version "$ver"
case "$ver" in
    *"$APK_STEM"*) gate_pass package_version "$ver (artifact stem $APK_STEM)" ;;
    *) gate_fail package_version "installed '$ver' does not carry the artifact stem $APK_STEM" ;;
esac

# surfaces (router-local; the br-lan client view is asserted by the driver)
# Probes the path each surface actually serves (see SURFACE_CONTRACT above) instead of
# the bare `/` the gate used to guess at — that guess is what made a correct install
# report `:2050=404(want 200)`, `:2051=403(want 200)` and `:8080=(want 307)`.
surf_bad=""
surf_ok=""
while IFS="|" read -r p path accept what; do
    [ -n "$p" ] || continue
    got="$(probe_code "$accept" -m 6 "http://127.0.0.1:$p$path")"
    if ! word_in "$got" "$accept"; then
        # Not the primary shape.  Try the portal's documented alternates before calling it
        # broken: a refusal (000) is not an alternate, and neither is a path that answers
        # NOTHING anywhere — a box that serves none of the documented shapes still fails.
        alt_ok=""
        alt_accept=""
        while IFS="|" read -r ap apath aaccept; do
            [ -n "$ap" ] || continue
            [ "$ap" = "$p" ] || continue
            alt="$(probe_code "$aaccept" -m 6 "http://127.0.0.1:$ap$apath")"
            if word_in "$alt" "$aaccept"; then
                alt_ok="$apath=$alt"
                alt_accept="$aaccept"
                got="$alt"
                fact "surface_${p}_alt_path" "$apath"
            fi
        done <<EOF
$SURFACE_ALTERNATES
EOF
        if [ -n "$alt_ok" ]; then
            path="$alt_ok"
            accept="$alt_accept"
        fi
    fi
    fact "surface_$p" "$got"
    if word_in "$got" "$accept"; then
        if [ "$p" = 8080 ] && [ "$got" = 000 ]; then
            surf_ok="$surf_ok :8080=no-plain-HTTP-answer(redirect_https: not assertable over plain HTTP)"
        else
            surf_ok="$surf_ok :$p=$got($what)"
        fi
    else
        surf_bad="$surf_bad :$p$path=$got(want $(printf '%s' "$accept" | tr ' ' '/'))"
    fi
done <<EOF
$SURFACE_CONTRACT
EOF
if [ -n "$surf_bad" ]; then
    gate_fail surfaces "wrong status:$surf_bad"
else
    gate_pass surfaces "$surf_ok"
fi

# guard fragment present AND loaded with no manual reload
if [ -f "$R$GUARD_PATH" ]; then
    gate_pass guard_fragment "present: $GUARD_PATH"
else
    gate_fail guard_fragment "missing: $GUARD_PATH (the #566 admin-board guard does not ship in this artifact)"
fi
guard_loaded=0
if nft list chain inet fw4 "$GUARD_CHAIN" 2>/dev/null | grep -q "$GUARD_CHAIN"; then
    guard_chain="$(nft list chain inet fw4 "$GUARD_CHAIN" 2>/dev/null)"
    if echo "$guard_chain" | grep -q 'br-lan' && echo "$guard_chain" | grep -q '8090' && echo "$guard_chain" | grep -q '8443'; then
        guard_loaded=1
        gate_pass guard_loaded "chain $GUARD_CHAIN loaded (br-lan -> drop 8090/8443) with NO manual reload"
    else
        gate_fail guard_loaded "chain $GUARD_CHAIN is loaded but does not drop 8090/8443 from br-lan"
    fi
else
    gate_fail guard_loaded "chain $GUARD_CHAIN is NOT loaded after the install (no manual reload was performed — the postinst must load it)"
fi

if ssh_listening; then
    gate_pass ssh_listening "port 22 is LISTENing"
else
    gate_fail ssh_listening "no listener on port 22 in /proc/net/tcp"
fi
if assert_keepalive_live; then
    gate_pass ssh_preauth "trustedmac + 'allow tcp port 22' still in the nodogsplash config"
else
    gate_fail ssh_preauth "pre-auth trust gone after the package install"
fi

# --- BOLT11 quote: CONDITIONAL on the uplink, and never silently skipped -------------
# With no uplink a quote is impossible by construction (the mint is addressed by name).
# The check is therefore not deleted and not silently skipped: on a WAN-less box it
# becomes an assertion about GRACEFUL DEGRADATION — the /ln-invoice route must still
# answer, in a documented degraded shape, without wedging, and must not hand back a bogus
# quote.  A missing/misrouted route (404/405), a rejected request (400/422) or no answer
# at all (000) still FAILS: "WAN-less" is not a licence to ship a dead payment surface.
# The report says which contract was applied, machine-readably: fact_wan and
# fact_bolt11_assertable, plus gate_bolt11_quote (info == not assertable, never a pass).
wan_state=no
wan_probe && wan_state=yes
fact wan "$wan_state"
fact wan_route "$WAN_ROUTE"
fact wan_resolver "$WAN_RESOLVER"
[ -n "$WAN_DIAG" ] && fact wan_diag "$WAN_DIAG"

# Same bounded-wait rule as the surfaces: a 000 here means the backend has not bound
# yet (the same startup lag), so retry it to the deadline.  Any real answer — including
# a documented degraded one (503 while the mint is unreachable WAN-less) — is taken
# as-is, so a misrouted or rejected route still fails without waiting.
invoice_waited=0
while :; do
    invoice_code="$(curl -s -o "$WORK/ln-invoice.body" -w '%{http_code}' -m 10 -X POST \
        -H 'Content-Type: application/json' -d '{"amount":21}' \
        "http://127.0.0.1:$BACKEND_PORT/ln-invoice" 2>/dev/null)"
    [ -n "$invoice_code" ] || invoice_code=000
    [ "$invoice_code" != 000 ] && break
    [ "$invoice_waited" -ge "$SURFACE_PROBE_DEADLINE" ] && break
    sleep "$SURFACE_PROBE_INTERVAL"
    invoice_waited=$((invoice_waited + SURFACE_PROBE_INTERVAL))
done
invoice_body="$(cat "$WORK/ln-invoice.body" 2>/dev/null)"
fact ln_invoice_code "$invoice_code"
invoice_pfx="$(printf '%s' "$invoice_body" | grep -o 'lnbc[a-z0-9]*' | head -1 | cut -c1-24)"

if printf '%s' "$invoice_body" | grep -q 'lnbc'; then
    fact bolt11_assertable yes
    if [ "$wan_state" = yes ]; then
        gate_pass bolt11_quote "POST /ln-invoice issued a BOLT11 invoice: $invoice_pfx…"
    else
        gate_pass bolt11_quote "WAN unavailable, yet /ln-invoice issued a BOLT11 invoice ($invoice_pfx…) — asserted anyway"
    fi
    gate_info bolt11_degraded "not applicable: a BOLT11 quote was issued and asserted"
elif [ "$wan_state" = yes ]; then
    fact bolt11_assertable yes
    gate_fail bolt11_quote "WAN present but no BOLT11 invoice in the /ln-invoice response (HTTP $invoice_code): $(printf '%s' "$invoice_body" | cut -c1-120)"
    gate_info bolt11_degraded "not applicable: an uplink is present, so the quote is asserted directly"
elif word_in "$invoice_code" "$DEGRADED_INVOICE_CODES"; then
    fact bolt11_assertable no
    gate_info bolt11_quote "WAN unavailable: quote not assertable — ${WAN_DIAG:-no uplink}"
    gate_pass bolt11_degraded "/ln-invoice answered HTTP $invoice_code in a documented degraded shape, with no quote and no wedge: $(printf '%s' "$invoice_body" | cut -c1-100)"
else
    fact bolt11_assertable no
    gate_info bolt11_quote "WAN unavailable: quote not assertable — ${WAN_DIAG:-no uplink}"
    gate_fail bolt11_degraded "/ln-invoice did not degrade gracefully: HTTP $invoice_code is not a documented degraded shape (route missing/misrouted, request rejected, or no answer): $(printf '%s' "$invoice_body" | cut -c1-120)"
fi

# The overall verdict is the gates: any failing gate is a failed install, and the
# report is emitted either way.
if grep -qE '^fail[[:space:]]' "$GATES" 2>/dev/null; then
    report_and_exit 9
fi
report_and_exit 0
