#!/usr/bin/env bash
# =============================================================================
# tests/offline-install/run-tests.sh — the NO-HARDWARE suite for the WAN-less
# (offline) install path.
#
# What it proves, without a router and without a network:
#
#   * the ordered, no-brick sequence really is ordered — the management keepalive is
#     seeded AND applied before anything can start enforcement, and the install refuses
#     to continue when it is missing or when the trust did not take (the 2026-08-16/17
#     bench wedge);
#   * the dependency install passes --no-network --allow-untrusted
#     --force-missing-repositories BY PATH, and that the last flag is load-bearing
#     (the apk double aborts without it, exactly as a real WAN-less router's apk does);
#   * the dependency install offers apk the WHOLE staged closure — and the AS-SHIPPED
#     stage (only REQUIRED_DEPS + STUB_OK_DEPS) REFUSES on a fresh box, which is the
#     wave-3 defect this suite exists to keep fixed (T20: apk refuses the whole
#     transaction, `REFUSED(7)`, and the negation's rc=0 was misreported).  The control
#     runs the same as-shipped stage on an UPGRADE box, where it passes — the defect was
#     fresh-box only, which is why it survived three waves of bench installs;
#   * the negative controls from the card, one test each:
#       (a) a bundle missing a dependency            -> refuses, naming it
#       (b) the keepalive step removed               -> refuses with the lockout reason
#       (c) a runtime dep satisfied by an empty stub -> refuses (NDS would crash-loop)
#       (d) a manifest mismatch                      -> refuses before installing anything
#       (e) an apk named differently than the manifest -> refuses
#     plus: no admin-board guard in the payload, no firewall reload in the postinst,
#     a wrong surface code, no BOLT11 quote, SSH not listening;
#   * the green path end to end: dependencies, package, payload identity, version,
#     surfaces, the guard loaded with NO manual reload, SSH alive, a BOLT11 quote, the
#     br-lan client view, and one machine-readable report with zero failing gates.
#
# The router is a throw-away directory; ssh/scp/apk/uci/nft/curl are PATH doubles in
# harness/bin/.  The production script text is what runs — including under the shell
# OpenWrt actually ships (BusyBox ash), which is why nothing here is a skip.
#
# usage: tests/offline-install/run-tests.sh [--only Tnn]
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=harness/lib.sh
. "$HERE/harness/lib.sh"

WORK="${TGOFFLINE_TEST_WORKDIR:-$(mktemp -d "${TMPDIR:-/tmp}/offline-install-tests.XXXXXX")}"
mkdir -p "$WORK"
export TGOFFLINE_HARNESS_SSH_LOG="$WORK/ssh-invocations.log"
export TGOFFLINE_HARNESS_SCP_LOG="$WORK/scp-invocations.log"
: > "$TGOFFLINE_HARNESS_SSH_LOG"
: > "$TGOFFLINE_HARNESS_SCP_LOG"
export PATH="$HERE/harness/bin:$PATH"

# the shell that stands in for the router's shell
if command -v busybox >/dev/null 2>&1 && busybox ash -c 'true' >/dev/null 2>&1; then
    SH_BIN="busybox ash"; SH_WHAT="busybox ash"
elif command -v dash >/dev/null 2>&1; then
    SH_BIN="dash"; SH_WHAT="dash"
else
    SH_BIN="sh"; SH_WHAT="$(command -v sh)"
fi
export SH_BIN
export TGOFFLINE_HARNESS_SH="$SH_BIN"

echo "offline-install suite"
echo "  workdir      : $WORK"
echo "  scripts under test: $SCRIPTS_DIR"
echo "  router shell : $SH_WHAT"
echo "  doubles      : $HERE/harness/bin"

if [ -x /usr/bin/iptables ] || [ -x /bin/iptables ]; then
    echo "FATAL: this host has an iptables in /usr/bin or /bin, which the harness PATH cannot" >&2
    echo "       exclude; T07 (missing base-image iptables) would be meaningless here." >&2
    exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "FATAL: python3 is required (the report is JSON and the suite parses it)" >&2
    exit 2
fi

# the payload hash of the fixture package, computed independently of the installer
fixture_payload_sha() { # $1=bundle ; the fixture apk's usr/bin/tollgate-wrt
    local apk
    apk="$(ls "$1"/pkgs/tollgate-wrt_*.apk | head -1)"
    local tmp; tmp="$(mktemp -d)"
    tar xzf "$apk" -C "$tmp" usr/bin/tollgate-wrt 2>/dev/null || tar xzf "$apk" -C "$tmp"
    sha256sum "$tmp/usr/bin/tollgate-wrt" | awk '{print $1}'
    rm -rf "$tmp"
}

truthy_gate() { # $1=report json key  -> prints pass/fail/'' from the report
    report_field "$1"
}

# ------------------------------------------------------------------ keepalive seed
# Run the PRODUCTION keepalive seed against the harness router, exactly the way
# install-router.sh applies it (step 1: `sh <seed>` with TGOFFLINE_ROOT standing in for
# the router's filesystem, and the uci double on PATH reading the same root).  The
# placeholder is injected first, as install-offline.sh does on the laptop side.
#   $1 = seed template to run (default: the shipped one)
run_keepalive_seed() {
    local seed="${1:-}" seeded
    [ -n "$seed" ] || seed="$SCRIPTS_DIR/templates/99z-mgmt-keepalive"
    seeded="$TGOFFLINE_HARNESS_ROOT/tmp/seed-applied/99z-mgmt-keepalive"
    mkdir -p "$TGOFFLINE_HARNESS_ROOT/tmp/seed-applied"
    sed 's/__TRUST_MAC__/AA:BB:CC:DD:EE:FF/' "$seed" > "$seeded"
    # shellcheck disable=SC2086  # SH_BIN may be "busybox ash"
    OUT="$( TGOFFLINE_ROOT="$TGOFFLINE_HARNESS_ROOT" \
            PATH="$TGOFFLINE_HARNESS_ROOT/usr/sbin:$TGOFFLINE_HARNESS_ROOT/usr/bin:$HERE/harness/bin:/usr/bin:/bin" \
            $SH_BIN "$seeded" 2>&1 )"
    RC=$?
    return 0
}

# The fresh-box pre-auth trust, asserted exactly the way install-router.sh's
# assert_keepalive_live() reads it — plus the committed config FILE the router keeps.
# $1 = description prefix (so the same assertions can be run as a labelled control)
keepalive_freshbox_asserts() {
    local p="$1" conf="$TGOFFLINE_HARNESS_ROOT/etc/config/nodogsplash"
    # (a) the config FILE the seed's FIRST uci call needs (absent on a fresh flash:
    #     nodogsplash is one of the packages the bundle DELIVERS, not yet installed)
    check_eq "$p the nodogsplash config FILE is created" "yes" \
        "$([ -f "$conf" ] && echo yes || echo no)"
    # (b) the anonymous section resolves — the target of every add_list below
    check_eq "$p the anonymous nodogsplash section exists" "nodogsplash" \
        "$(uci -q get 'nodogsplash.@nodogsplash[0]' 2>/dev/null)"
    # (c) the pre-auth trust, through uci (what the gate reads) …
    check_contains "$p trustedmac carries the workstation MAC" "AA:BB:CC:DD:EE:FF" \
        "$(uci -q get 'nodogsplash.@nodogsplash[0].trustedmac' 2>/dev/null)"
    check_contains "$p users_to_router carries 'allow tcp port 22'" "allow tcp port 22" \
        "$(uci -q get 'nodogsplash.@nodogsplash[0].users_to_router' 2>/dev/null)"
    # … and in the committed config FILE itself
    check_contains "$p the committed config carries the MAC" "AA:BB:CC:DD:EE:FF" \
        "$(cat "$conf" 2>/dev/null)"
    check_contains "$p the committed config carries the allow rule" "allow tcp port 22" \
        "$(cat "$conf" 2>/dev/null)"
}

# =============================================================== T01 dry-run
test_T01() {
    router_root_new
    local b; b="$(bundle_build "$WORK/b01")"
    : > "$TGOFFLINE_HARNESS_SSH_LOG"
    run_install "$b" --dry-run
    check_rc "dry-run exits 0" 0 "$RC"
    check_contains "the ordered plan is printed" "apk add --no-network --allow-untrusted --force-missing-repositories" "$OUT"
    check_contains "the plan names the keepalive step first" "keepalive" "$OUT"
    check_contains "the plan says the router was not touched" "TGOFFLINE-RESULT DRY-RUN" "$OUT"
    check_eq "no ssh invocation at all" "" "$(ssh_log)"
    check_eq "nothing was installed" "0" "$(apk_add_count)"
}

# =============================================================== T02 green path
test_T02() {
    router_root_new
    local b; b="$(bundle_build "$WORK/b02")"
    run_install "$b"
    check_rc "green install exits 0" 0 "$RC"
    check_contains "the result is PASS" "TGOFFLINE-RESULT PASS" "$OUT"
    check_contains "keepalive seeded gate" "keepalive_seeded" "$OUT"
    check_contains "keepalive applied gate" "keepalive_applied" "$OUT"
    check_contains "payload identity gate" "payload_sha256" "$OUT"
    check_contains "guard loaded gate" "guard_loaded" "$OUT"
    check_contains "guard loaded with NO manual reload" "NO manual reload" "$OUT"
    check_contains "BOLT11 quote gate" "bolt11_quote" "$OUT"

    # the seed really landed on the router, with the MAC injected, first
    local seed="$TGOFFLINE_HARNESS_ROOT/etc/uci-defaults/99z-mgmt-keepalive"
    if [ -f "$seed" ]; then
        pass "the seed is in /etc/uci-defaults/99z-mgmt-keepalive"
        check_contains "the seed carries the injected MAC" "AA:BB:CC:DD:EE:FF" "$(cat "$seed")"
    else
        fail "the seed is NOT in /etc/uci-defaults/99z-mgmt-keepalive"
    fi
    check_contains "nodogsplash trustedmac is committed" "AA:BB:CC:DD:EE:FF" \
        "$(cat "$TGOFFLINE_HARNESS_ROOT/etc/config/nodogsplash" 2>/dev/null)"
    check_contains "nodogsplash users_to_router allows tcp/22 pre-auth" "allow tcp port 22" \
        "$(cat "$TGOFFLINE_HARNESS_ROOT/etc/config/nodogsplash" 2>/dev/null)"

    # every bundle package landed
    local names; names="$(installed_names)"
    check_contains "nodogsplash installed" "nodogsplash" "$names"
    check_contains "jq installed" "jq" "$names"
    check_contains "libmicrohttpd-no-ssl installed" "libmicrohttpd-no-ssl" "$names"
    check_contains "the libpthread stub installed" "libpthread" "$names"
    check_contains "tollgate-wrt installed" "tollgate-wrt" "$names"
    check_contains "the installed version is the artifact's" "tollgate-wrt 0.6.0_alpha4_pre17-r1" \
        "$(cat "$TGOFFLINE_HARNESS_ROOT/var/lib/apk/installed")"

    # the dependency install used all three flags, by path
    local addline; addline="$(grep 'apk add:' "$TGOFFLINE_HARNESS_ROOT/var/log/apk.log" | head -1)"
    check_contains "--no-network was passed" "no-network=1" "$addline"
    check_contains "--allow-untrusted was passed" "allow-untrusted=1" "$addline"
    check_contains "--force-missing-repositories was passed" "force-missing-repositories=1" "$addline"
    check_eq "two apk add invocations (deps, then the package)" "2" "$(apk_add_count)"
    check_contains "the dependency files were passed BY PATH" "pkgs/nodogsplash-5.0.2-r1.apk" "$(ssh_log)"

    # payload identity: the installed binary IS the artifact's payload
    local want; want="$(fixture_payload_sha "$b")"
    check_eq "the installed payload hash equals the artifact payload" "$want" \
        "$(sha256sum "$TGOFFLINE_HARNESS_ROOT/usr/bin/tollgate-wrt" | awk '{print $1}')"
    # the router's own report carries it too (the driver embeds the router half)
    check_eq "the report carries that same payload hash" "$want" \
        "$(report_remote_field fact_installed_payload_sha256)"

    # the guard was loaded by the PACKAGE, not by the installer
    check_contains "the package postinst reloaded fw4" "fw4 reload" \
        "$(cat "$TGOFFLINE_HARNESS_ROOT/var/log/postinst.log" 2>/dev/null)"
    check_contains "the installer only ever listed the chain" "list chain inet fw4 admin_board_input_guard" "$(nft_log)"
    check_not_contains "the installer NEVER reloaded the firewall" "RELOAD-ISSUED" "$(nft_log)"

    # the client half of the verification
    check_eq "the report's :8090 client probe is refused" "000" "$(truthy_gate fact_client_8090)"
    check_eq "the report's :8443 client probe is refused" "000" "$(truthy_gate fact_client_8443)"
    check_eq "the report's :8080 client probe is 307" "307" "$(truthy_gate fact_client_8080)"
    check_eq "the report says PASS" "PASS" "$(truthy_gate result)"
    check_eq "no failing gate in the report" "0" \
        "$(python3 "$HERE/harness/report.py" "$WORK/report.json" fails 2>/dev/null)"
    check_eq "no gate in the remote report failed either" "0" \
        "$(python3 "$HERE/harness/report.py" "$WORK/report.json" remote-fails 2>/dev/null)"
    # the first five fields are `apk add` + the three flags; the rest is the file list
    check_eq "the router-side report names the dependency command" \
        "apk add --no-network --allow-untrusted --force-missing-repositories" \
        "$(report_remote_field fact_apk_deps_cmd | awk '{print $1, $2, $3, $4, $5}')"
}

# =============================================================== T03 (a) missing dep
test_T03() {
    router_root_new
    local b; b="$(bundle_build "$WORK/b03" drop-dep=jq)"
    run_install "$b"
    check_rc "a bundle missing jq is refused (4)" 4 "$RC"
    check_contains "the refusal NAMES the missing dependency" "declared dependency: jq" "$OUT"
    check_contains "the refusal explains the WAN-less cause" "no feed indexes" "$OUT"
    check_eq "nothing was installed" "0" "$(apk_add_count)"
}

# =============================================================== T04 (b) no keepalive
test_T04() {
    router_root_new
    local b; b="$(bundle_build "$WORK/b04" no-keepalive)"
    : > "$TGOFFLINE_HARNESS_SSH_LOG"
    run_install "$b"
    check_rc "a bundle without the keepalive seed is refused (5)" 5 "$RC"
    check_contains "the refusal gives the LOCKOUT reason" "loses SSH the moment nodogsplash enforces on br-lan" "$OUT"
    check_contains "the refusal names the wedge" "2026-08-16/17" "$OUT"
    check_eq "the router was never touched" "" "$(ssh_log)"
    check_eq "nothing was installed" "0" "$(apk_add_count)"
}

# =============================================================== T05 (b2) trust not live
test_T05() {
    router_root_new
    local b; b="$(bundle_build "$WORK/b05")"
    export TGOFFLINE_HARNESS_UCI_IGNORE=1
    run_install "$b"
    unset TGOFFLINE_HARNESS_UCI_IGNORE
    check_rc "the install refuses when the trust did not take (5)" 5 "$RC"
    check_contains "the refusal is the lockout reason" "pre-auth trust is NOT live" "$OUT"
    check_contains "the refusal names the consequence" "lock the operator out of SSH" "$OUT"
    check_eq "the dependency install never ran" "0" "$(apk_add_count)"
}

# =============================================================== T06 (c1) empty stub
test_T06() {
    router_root_new
    local b; b="$(bundle_build "$WORK/b06" stub-dep=nodogsplash)"
    run_install "$b"
    check_rc "a runtime dep satisfied by an empty stub is refused (8)" 8 "$RC"
    check_contains "the refusal names the dependency" "nodogsplash" "$OUT"
    check_contains "the refusal names the missing payload" "/usr/bin/nodogsplash" "$OUT"
    check_contains "the refusal explains the crash-loop" "crash-loop" "$OUT"
    check_contains "the refusal says only virtual deps may be stubbed" "Only stale VIRTUAL deps" "$OUT"
}

# =============================================================== T07 (c2) no iptables
test_T07() {
    router_root_new no-iptables
    local b; b="$(bundle_build "$WORK/b07")"
    run_install "$b"
    check_rc "a router without the iptables payload is refused (8)" 8 "$RC"
    check_contains "the refusal names iptables-nft" "iptables-nft" "$OUT"
    check_contains "the refusal quotes what NDS execs at start-up" "iptables --version" "$OUT"
}

# =============================================================== T08 (d) tamper
test_T08() {
    router_root_new
    local b; b="$(bundle_build "$WORK/b08" tamper)"
    : > "$TGOFFLINE_HARNESS_SSH_LOG"
    run_install "$b"
    check_rc "a tampered bundle is refused (3)" 3 "$RC"
    check_contains "the refusal names the manifest" "MANIFEST.sha256" "$OUT"
    check_contains "the refusal happens before anything is staged" "refusing before staging anything" "$OUT"
    check_eq "not a single byte was pushed" "" "$(ssh_log)"
    check_eq "nothing was installed" "0" "$(apk_add_count)"
}

# =============================================================== T09 (e) unnamed apk
test_T09() {
    router_root_new
    local b; b="$(bundle_build "$WORK/b09" foreign-apk=zzz-not-manifested-9.9-r9.apk)"
    : > "$TGOFFLINE_HARNESS_SSH_LOG"
    run_install "$b"
    check_rc "an apk the manifest does not name is refused (6)" 6 "$RC"
    check_contains "the refusal names the file" "zzz-not-manifested-9.9-r9.apk" "$OUT"
    check_contains "the refusal says it is not named in the manifest" "MANIFEST.sha256 does not name" "$OUT"
    check_eq "nothing was staged" "" "$(ssh_log)"
}

# =============================================================== T10 (e2) router-side binding
test_T10() {
    router_root_new
    local b stage; b="$(bundle_build "$WORK/b10")"
    stage="$TGOFFLINE_HARNESS_ROOT/tmp/tgoffline"
    mkdir -p "$stage/pkgs"
    cp "$b"/pkgs/*.apk "$stage/pkgs/"
    cp "$b/MANIFEST.sha256" "$stage/MANIFEST.sha256"
    cp "$b/templates/99z-mgmt-keepalive" "$stage/99z-mgmt-keepalive"
    sed -i 's/__TRUST_MAC__/AA:BB:CC:DD:EE:FF/' "$stage/99z-mgmt-keepalive"

    # (i) an extra package the manifest does not name, staged on the router
    printf 'not a package\n' > "$stage/pkgs/extra-staged-1.0-r1.apk"
    run_remote_script "$stage"
    check_rc "a foreign staged apk is refused by the router-side gate (6)" 6 "$RC"
    check_contains "the refusal names the staged file" "extra-staged-1.0-r1.apk" "$OUT"
    check_contains "the refusal names the manifest" "MANIFEST.sha256" "$OUT"
    check_eq "the package was not installed" "0" "$(apk_add_count)"

    # (ii) a dependency staged under a different name
    router_root_new
    stage="$TGOFFLINE_HARNESS_ROOT/tmp/tgoffline"
    mkdir -p "$stage/pkgs"
    cp "$b"/pkgs/*.apk "$stage/pkgs/"
    mv "$stage/pkgs/jq-1.8.1-r2.apk" "$stage/pkgs/jq.apk"
    cp "$b/MANIFEST.sha256" "$stage/MANIFEST.sha256"
    cp "$b/templates/99z-mgmt-keepalive" "$stage/99z-mgmt-keepalive"
    sed -i 's/__TRUST_MAC__/AA:BB:CC:DD:EE:FF/' "$stage/99z-mgmt-keepalive"
    run_remote_script "$stage"
    check_rc "a dependency staged under a different name is refused (4)" 4 "$RC"
    check_contains "the refusal names the dependency" "declared dependency: jq" "$OUT"
    check_eq "the package was not installed" "0" "$(apk_add_count)"
}

# =============================================================== T11 no guard fragment
test_T11() {
    router_root_new
    local b; b="$(bundle_build "$WORK/b11" no-guard)"
    run_install "$b"
    check_rc "an artifact without the guard fragment fails the gate (9)" 9 "$RC"
    check_contains "the guard fragment gate fails" "guard_fragment" "$OUT"
    check_contains "the refusal names the guard path" "31-admin-board-not-guest-reachable.nft" "$OUT"
    check_eq "the report says FAIL" "FAIL" "$(truthy_gate result)"
}

# =============================================================== T12 no reload in postinst
test_T12() {
    router_root_new
    local b; b="$(bundle_build "$WORK/b12" no-reload-postinst)"
    run_install "$b"
    check_rc "a package whose postinst never reloads fw4 fails the gate (9)" 9 "$RC"
    check_contains "the guard-loaded gate fails" "guard_loaded" "$OUT"
    check_contains "the failure explains the missing reload" "NOT loaded after the install" "$OUT"
    check_not_contains "the installer did NOT reload the firewall itself" "RELOAD-ISSUED" "$(nft_log)"
    check_eq "and the br-lan client still reaches :8090 (guard absent)" "200" "$(truthy_gate fact_client_8090)"
    check_eq "so the client surface gate fails too" "fail" "$(truthy_gate gate_client_surfaces)"
}

# =============================================================== T13 wrong surface
test_T13() {
    router_root_new
    local b; b="$(bundle_build "$WORK/b13")"
    router_feature 8080-200
    run_install "$b"
    check_rc "a wrong surface code fails the gates (9)" 9 "$RC"
    check_contains "the surfaces gate fails and names the port" ":8080=200(want 307)" "$OUT"
}

# =============================================================== T14 no BOLT11 quote
test_T14() {
    router_root_new
    local b; b="$(bundle_build "$WORK/b14")"
    router_feature no-bolt11
    run_install "$b"
    check_rc "no BOLT11 quote fails the gate (9)" 9 "$RC"
    check_contains "the quote gate fails" "bolt11_quote" "$OUT"
    check_contains "the failure quotes the endpoint" "/ln-invoice" "$OUT"
}

# =============================================================== T15 SSH not listening
test_T15() {
    router_root_new ssh-down
    local b; b="$(bundle_build "$WORK/b15")"
    run_install "$b"
    check_rc "a router with no :22 listener fails the gate (9)" 9 "$RC"
    check_contains "the ssh gate fails" "ssh_listening" "$OUT"
    check_contains "the failure names /proc/net/tcp" "/proc/net/tcp" "$OUT"
}

# =============================================================== T16 the flag is load-bearing
test_T16() {
    router_root_new
    local b; b="$(bundle_build "$WORK/b16")"
    # (i) WITHOUT the flag the real apk aborts on the missing feed indexes — modelled
    #     directly here so the mechanism is proven, not asserted in prose
    OUT="$( PATH="$TGOFFLINE_HARNESS_ROOT/usr/sbin:$TGOFFLINE_HARNESS_ROOT/usr/bin:$HERE/harness/bin:$PATH" \
            TGOFFLINE_HARNESS_ROOT="$TGOFFLINE_HARNESS_ROOT" \
            apk add --no-network --allow-untrusted "$b/pkgs/jq-1.8.1-r2.apk" 2>&1 )"
    RC=$?
    check_rc "apk add WITHOUT --force-missing-repositories aborts" 1 "$RC"
    check_contains "the abort is the missing-index error" "No such file or directory" "$OUT"
    check_contains "the abort asks for the flag" "--force-missing-repositories" "$OUT"
    check_eq "nothing was installed by the aborted call" "0" "$(apk_add_count)"

    # (ii) WITH the flag, the very same call succeeds
    OUT="$( PATH="$TGOFFLINE_HARNESS_ROOT/usr/sbin:$TGOFFLINE_HARNESS_ROOT/usr/bin:$HERE/harness/bin:$PATH" \
            TGOFFLINE_HARNESS_ROOT="$TGOFFLINE_HARNESS_ROOT" \
            apk add --no-network --allow-untrusted --force-missing-repositories "$b/pkgs/jq-1.8.1-r2.apk" 2>&1 )"
    RC=$?
    check_rc "the same call WITH the flag succeeds" 0 "$RC"
    check_eq "and it installed jq" "1" "$(apk_add_count)"

    # (iii) the production script's own command carries the flag
    router_root_new
    run_install "$b"
    check_contains "the production command names the flag" \
        "apk add --no-network --allow-untrusted --force-missing-repositories" "$OUT"
}

# =============================================================== T17 the router's shell
test_T17() {
    router_root_new
    local b; b="$(bundle_build "$WORK/b17")"
    check_eq "the suite drives the router script under a non-bash shell" "1" \
        "$(case "$SH_WHAT" in busybox*|dash) echo 1 ;; *) echo 0 ;; esac)"
    # the production scripts must be accepted by that shell
    OUT="$( $SH_BIN -n "$SCRIPTS_DIR/install-router.sh" 2>&1 )"
    check_rc "install-router.sh parses under $SH_WHAT" 0 "$?"
    OUT="$( $SH_BIN -n "$SCRIPTS_DIR/install-offline.sh" 2>&1 )"
    check_rc "install-offline.sh parses under $SH_WHAT" 0 "$?"
    OUT="$( $SH_BIN -n "$SCRIPTS_DIR/templates/99z-mgmt-keepalive" 2>&1 )"
    check_rc "the keepalive seed parses under $SH_WHAT" 0 "$?"
    # and the whole green path runs under it
    run_install "$b"
    check_rc "the green path runs end to end under $SH_WHAT" 0 "$RC"
    check_contains "…and still passes" "TGOFFLINE-RESULT PASS" "$OUT"
}

# =============================================================== T18 no scp anywhere
test_T18() {
    router_root_new
    local b; b="$(bundle_build "$WORK/b18")"
    run_install "$b"
    check_rc "install ok" 0 "$RC"
    check_eq "scp was never invoked" "" "$(scp_log)"
    check_contains "staging went through ssh stdin redirects" "cat > " "$(ssh_log)"
}

# =============================================================== T19 report shape
test_T19() {
    router_root_new
    local b; b="$(bundle_build "$WORK/b19")"
    run_install "$b"

    check_rc "install ok" 0 "$RC"

    # The gates the LAPTOP side owns and the ones the ROUTER side owns. Both sets must
    # be present, and the router half must come back embedded as an OBJECT (the driver
    # nests it under remote_report), so a consumer can read either half.
    # The gates that are emitted on EVERY run (each side).  A gate that is emitted
    # only on its failure branch is NOT listed here — that is what the failure tests
    # T03-T15 cover; this test is about the green report's shape.
    LAPTOP_GATES="keepalive_template bundle_package_names manifest_verified package_manager trust_mac router_reachable keepalive_staged staged_copy remote_report client_surfaces"
    REMOTE_GATES="keepalive_seeded keepalive_applied keepalive_live staged_binding bundle_closure deps_installed stub_dep runtime_payload package_installed payload_sha256 package_version surfaces guard_fragment guard_loaded ssh_listening ssh_preauth bolt11_quote"
    TOP_KEYS="installer_version side router result remote_result fact_manifest_entries_ok fact_trust_mac fact_client_8090 fact_client_8443 fact_remote_exit_code"

    OUT="$(python3 - "$WORK/report.json" "$LAPTOP_GATES" "$REMOTE_GATES" "$TOP_KEYS" <<'PY' 2>&1
import json, sys
raw = open(sys.argv[1], encoding="utf-8").read()
begin, end = "TGOFFLINE-REPORT-BEGIN", "TGOFFLINE-REPORT-END"
# exactly one marker pair, and the block between them must be valid JSON on its own
assert raw.count(begin) == 1 and raw.count(end) == 1, "marker pair is not unique"
data = json.loads(raw[raw.index(begin) + len(begin):raw.index(end)])
remote = data.get("remote_report")
router = sys.argv[3].split()
print("missing_top=" + ",".join(k for k in sys.argv[4].split() if k not in data))
print("missing_laptop=" + ",".join(k for k in sys.argv[2].split() if "gate_" + k not in data))
print("missing_remote=" + ",".join(k for k in router if "gate_" + k not in (remote or {})))
print("version=" + str(data.get("installer_version")))
print("result=" + str(data.get("result")))
print("remote_result=" + str(data.get("remote_result")))
print("laptop_fails=" + str(sum(1 for k, v in data.items() if k.startswith("gate_") and v == "fail")))
print("remote_keys=" + str(len(remote) if isinstance(remote, dict) else -1))
print("remote_fails=" + str(sum(1 for k, v in (remote or {}).items() if k.startswith("gate_") and v == "fail")))
print("payload=" + str((remote or {}).get("fact_installed_payload_sha256")))
PY
)"
    field() { printf '%s' "$OUT" | sed -n "s/^$1=//p"; }

    check_contains "the report parses as JSON and carries the marker pair" "missing_top=" "$OUT"
    check_eq "no top-level key is missing" "" "$(field missing_top)"
    check_eq "every laptop-side gate is in the report" "" "$(field missing_laptop)"
    check_eq "every router-side gate is in the embedded report" "" "$(field missing_remote)"
    check_eq "the installer version is in the report" "1.0.0" "$(field version)"
    check_eq "the report says PASS" "PASS" "$(field result)"
    check_eq "the router half passed too" "PASS" "$(field remote_result)"
    check_eq "no laptop-side gate failed" "0" "$(field laptop_fails)"
    check_eq "the remote report is embedded as an object" "1" \
        "$(awk -v n="$(field remote_keys)" 'BEGIN { print (n > 10) ? 1 : 0 }')"
    check_eq "the remote report has no failing gate" "0" "$(field remote_fails)"
    # the report must carry the router's OWN payload hash, not a copy of the manifest's
    check_eq "the payload hash in the report is the installed binary's" \
        "$(sha256sum "$TGOFFLINE_HARNESS_ROOT/usr/bin/tollgate-wrt" | awk '{print $1}')" \
        "$(field payload)"
    # and the same facts must be readable through the shipped reader
    check_eq "report.py reads the embedded router facts" "$(field payload)" \
        "$(report_remote_field fact_installed_payload_sha256)"
    check_eq "report.py counts no top-level failure" "0" \
        "$(python3 "$HERE/harness/report.py" "$WORK/report.json" fails 2>/dev/null)"
}

# =============================================================== T20 the closure, fresh box
# The defect this group exists for: stage (2) of the router-side installer built its apk
# file list from REQUIRED_DEPS + STUB_OK_DEPS, so a FRESHLY FLASHED box was handed 4 of
# the bundle's staged packages.  apk-tools 3 resolves a transaction from the files NAMED
# on the command line plus the installed DB and from nothing else, so nodogsplash's
# iptables closure (`iptables-nft`, `iptables-mod-conntrack-extra`,
# `iptables-mod-ipopt`, `iptables-mod-nat-extra`) read `(no such package)` and apk
# refused the WHOLE transaction → `REFUSED(7)`, on a box the bundle carried every
# package for.  On an UPGRADE box those deps are already installed, so the transaction
# resolved from the DB and the stage looked correct — which is why three waves of bench
# installs never saw it.
#
# T20 runs the AS-SHIPPED stage (the pre-fix text, rebuilt by reversing the fix in the
# shipped file) as a CONTROL, so the green assertions cannot be vacuous:
#   (a) fresh box, as-shipped   -> REFUSED(7), only the top-level deps offered, and the
#                                  gate reports the NEGATION's rc (0), not apk's
#   (b) upgrade box, as-shipped -> PASS: the defect is fresh-box only
#   (c) fresh box, shipped      -> PASS, every staged package except the one under test
#   (d) apk's REAL rc reaches the gate, on both the shipped and the as-shipped stage
test_T20() {
    local as_shipped="$WORK/as-shipped-scripts"
    local bfix="" bctl="" staged_green expected_green expected_ship4

    check_gate() { # $1 desc, $2 gate name, $3 state, $4 haystack
        if printf '%s' "$4" | grep -qE "^gate $2 +$3"; then
            pass "$1"
        else
            fail "$1: no '$3' gate line for $2"
            printf '        --- output was ---\n%s\n        --- apk log was ---\n%s\n' "$4" "$(apk_log)"
        fi
    }

    # ---- rebuild the AS-SHIPPED stage by reversing the fix in the shipped file -------
    # The control has to run the PRE-FIX stage text, and the shipped file is the only
    # source of truth for what the fix replaced — so it is reversed here instead of being
    # paraphrased.  If the shipped stage stops matching, the reversal FAILS LOUDLY: that
    # is the drift guard (a control that silently tests something else is worse than no
    # control).
    rm -rf "$as_shipped"
    mkdir -p "$as_shipped"
    cp -R "$SCRIPTS_DIR/." "$as_shipped/"
    if OUT="$(python3 - "$as_shipped/install-router.sh" <<'PY' 2>&1
import re
import sys

path = sys.argv[1]
text = open(path, encoding="utf-8").read()

# the stage text AS SHIPPED (verbatim — the shape that refused on a fresh box)
AS_SHIPPED_FILES = '''dep_files=""
for dep in $REQUIRED_DEPS; do
    for f in $STAGED_APKS; do
        case "$(basename "$f")" in
            "$dep-"*) dep_files="$dep_files $f" ;;
        esac
    done
done
for dep in $STUB_OK_DEPS; do
    for f in $STAGED_APKS; do
        case "$(basename "$f")" in
            "$dep-"*) dep_files="$dep_files $f" ;;
        esac
    done
done
'''

AS_SHIPPED_DEPS_RC = '''if ! apk add --no-network --allow-untrusted --force-missing-repositories $dep_files; then
    gate_fail deps_installed "apk add of the dependency files failed rc=$?"
'''

# the two regions the fix rewrote, matched by their own shape (comments included)
FILES_FIXED = re.compile(
    r"(?ms)^# --- full-closure offer \(added by the offline bundle builder\) -+\n"
    r"(?:^#[^\n]*\n)*"
    r'^dep_files=""\n'
    r"^for f in \$STAGED_APKS; do\n"
    r'^    if \[ "\$f" != "\$PKG_APK" \]; then\n'
    r'^        dep_files="\$dep_files \$f"\n'
    r"^    fi\n"
    r"^done\n")
DEPS_RC_FIXED = re.compile(
    r"(?ms)^#[^\n]*NEGATION[^\n]*\n"
    r"(?:^#[^\n]*\n)*"
    r"^apk_deps_rc=0\n"
    r"^apk add --no-network[^\n]*\| apk_deps_rc=\$\?\n"
    r'^if \[ "\$apk_deps_rc" != 0 \]; then\n'
    r"^    gate_fail deps_installed[^\n]*\n")

for pattern, replacement, label in ((FILES_FIXED, AS_SHIPPED_FILES, "stage-2 file list"),
                                    (DEPS_RC_FIXED, AS_SHIPPED_DEPS_RC, "stage-2 rc accounting")):
    hits = len(pattern.findall(text))
    if hits != 1:
        print("CONTROL-FAILURE: the shipped %s is not the shape this control reverses "
              "(%d match(es)) — reconcile T20 with scripts/offline/install-router.sh"
              % (label, hits))
        sys.exit(1)
    text = pattern.sub(lambda m: replacement, text, count=1)

open(path, "w", encoding="utf-8").write(text)
print("reversed: %s, %s" % ("stage-2 file list", "stage-2 rc accounting"))
PY
)"; then
        pass "closure control: the shipped stage reverses to the as-shipped text ($(printf '%s' "$OUT" | tail -n1))"
    else
        fail "closure control: could not rebuild the as-shipped stage — $(printf '%s' "$OUT" | tail -n1)"
    fi

    bfix="$(bundle_build "$WORK/b20fix")" || bfix=""
    bctl="$(TGOFFLINE_HARNESS_SCRIPTS="$as_shipped" bundle_build "$WORK/b20ctl")" || bctl=""
    if [ -z "$bfix" ] || [ -z "$bctl" ]; then
        fail "closure: could not build the fixture bundles (shipped='$bfix' as-shipped='$bctl')"
        return 0
    fi
    staged_green="$(cd "$bfix/pkgs" && ls *.apk | grep -v '^tollgate-wrt_' | sort)"
    expected_green="$staged_green"
    expected_ship4="$(printf '%s\n' "$(cd "$bctl/pkgs" && ls *.apk)" \
        | grep -E '^(nodogsplash|jq|libmicrohttpd-no-ssl|libpthread)-' | sort)"

    # (a) CONTROL, fresh box, as-shipped stage: the refusal, and its rc accounting.
    router_root_new
    run_install "$bctl"
    check_rc "closure control: the as-shipped stage REFUSES the dependency install on a fresh box" 7 "$RC"
    check_contains "closure control: the refusal is the WAN-less dependency refusal" "REFUSED(7)" "$OUT"
    check_contains "closure control: the refusal names what to check (closure/arch/flag)" \
        "a package missing from the bundle's closure" "$OUT"
    check_contains "closure control: the gate reported the NEGATION's rc (0), not apk's" \
        "apk add of the dependency files failed rc=0" "$OUT"
    check_eq "closure control: the as-shipped stage offered $(printf '%s\n' "$expected_ship4" | grep -c .) of $(printf '%s\n' "$expected_green" | grep -c .) staged packages (the top-level deps only)" \
        "$expected_ship4" "$(deps_offered_apks)"
    check_eq "closure control: apk refused the WHOLE transaction — nothing was installed" \
        "0" "$(apk_add_count)"

    # (b) CONTROL, upgrade box (those deps already installed), same as-shipped stage.
    router_root_new
    router_feature upgrade-box
    run_install "$bctl"
    check_rc "closure control: the SAME as-shipped stage PASSES on an upgrade box — the defect is fresh-box only" 0 "$RC"
    check_contains "closure control: the upgrade-box install is a PASS" "TGOFFLINE-RESULT PASS" "$OUT"
    check_gate "closure control: deps_installed passed on the upgrade box" deps_installed PASS "$OUT"
    check_eq "closure control: on an upgrade box only the top-level deps were offered — and that was enough" \
        "$expected_ship4" "$(deps_offered_apks)"

    # (c) the SHIPPED stage, fresh box: the closure is offered, resolves, and lands.
    router_root_new
    run_install "$bfix"
    check_rc "closure: the shipped stage passes on a fresh box" 0 "$RC"
    check_contains "closure: the fresh-box install is a PASS" "TGOFFLINE-RESULT PASS" "$OUT"
    check_gate "closure: deps_installed passed" deps_installed PASS "$OUT"
    check_eq "closure: the shipped stage offers EVERY staged package except the one under test" \
        "$expected_green" "$(deps_offered_apks)"
    check_eq "closure: the fresh-box install still uses exactly two apk add invocations (closure, then the package)" \
        "2" "$(apk_add_count)"
    check_contains "closure: nodogsplash's iptables closure really landed (iptables-nft)" \
        "iptables-nft" "$(installed_names_sorted)"
    check_contains "closure: …and iptables-mod-nat-extra too" \
        "iptables-mod-nat-extra" "$(installed_names_sorted)"
    check_contains "closure: …and xtables-nft landed too" \
        "xtables-nft" "$(installed_names_sorted)"
    check_contains "closure: …and libxtables" \
        "libxtables" "$(installed_names_sorted)"
    check_contains "closure: …and the base image is still there (the closure did not replace it)" \
        "libc" "$(installed_names_sorted)"

    # (d) apk's OWN rc reaches the gate (the negation's 0 was the second half of the bug).
    router_root_new
    router_feature apk-add-fails
    run_install "$bfix"
    check_rc "closure: an apk that fails aborts the install (7)" 7 "$RC"
    check_contains "closure: the dependency gate reports apk's REAL rc (3), not the negation's 0" \
        "apk add of the dependency files failed rc=3" "$OUT"
    check_not_contains "closure: and the gate does not report rc=0 for a real failure" \
        "failed rc=0" "$OUT"
    router_root_new
    router_feature apk-add-fails
    run_install "$bctl"
    check_contains "closure control: the as-shipped accounting reported that same rc=3 failure as rc=0" \
        "apk add of the dependency files failed rc=0" "$OUT"

    # (e) STATIC: the shipped script hands apk the staged set and reads apk's own rc at
    #     BOTH invocations.  (The one `for dep in $REQUIRED_DEPS` loop that remains is
    #     the stage-1b closure/name gate, which is where it belongs.)
    check_eq "closure: the shipped script no longer builds the apk file list from REQUIRED_DEPS/STUB_OK_DEPS" \
        "1" "$(grep -cF 'for dep in $REQUIRED_DEPS' "$SCRIPTS_DIR/install-router.sh")"
    check_eq "closure: the dependency stage captures apk's own rc" "1" \
        "$(grep -cF '|| apk_deps_rc=$?' "$SCRIPTS_DIR/install-router.sh")"
    check_eq "closure: the package stage captures apk's own rc too" "1" \
        "$(grep -cF '|| apk_pkg_rc=$?' "$SCRIPTS_DIR/install-router.sh")"
    check_eq "closure: no apk invocation reads \$? inside \`if !\` any more" "0" \
        "$(grep -cF 'failed rc=$?' "$SCRIPTS_DIR/install-router.sh")"
}

# =============================================================== T21 keepalive, fresh box
# The defect this group exists for: on a freshly flashed WAN-less box /etc/config has
# NO nodogsplash — nodogsplash is one of the 38 packages the bundle DELIVERS, so it is
# not installed yet when the keepalive seed runs.  Real `uci` cannot create a section in
# a config file that does not exist: `uci add nodogsplash nodogsplash` prints
# "uci: Entry not found" and exits 3 (measured on a bench MT3000, fresh flash, WAN-less,
# 2026-09-28; with the file created first the very same call exits 0).  It cannot resolve
# the anonymous section @nodogsplash[0] without the file either, so the seed's add_list
# calls land NOTHING — and the seed ignores errors and exits 0 anyway.  install-router.sh's
# keepalive_applied gate then finds no trustedmac / no 'allow tcp port 22' and REFUSES
# with exit 5 on a first flash: a bundle refused by the gate for a package the bundle
# itself delivers.
#
# The fix is in the seed, so this group drives the SEED directly (the router's own
# shell, the uci double on PATH, TGOFFLINE_ROOT standing in for the filesystem):
#   (a) fresh box, shipped seed    -> the config file is created, the section exists,
#                                     the trust lands, and a RE-RUN is idempotent
#   (b) fresh box, file-ensure line removed (scratch copy of the seed INSIDE the
#       harness, never the repo file) -> the SAME assertions FAIL, and they fail for
#       the right reason: no config file is created and the trust never lands.  The
#       control also pins the mechanism itself (guard_rc=3 / uci: Entry not found
#       without the file, rc=0 with it), so the green assertions cannot be vacuous.
test_T21() {
    # the scratch seed copy lives in $WORK, NOT under the harness root: router_root_new
    # wipes the root between cases and would take the control seed with it
    local seeded_ctl="$WORK/keepalive-control"
    # the shipped template's hash, captured BEFORE the control runs, so the control can be
    # proven not to have edited the repo file back
    local seed_sha
    seed_sha="$(sha256sum "$SCRIPTS_DIR/templates/99z-mgmt-keepalive" | awk '{print $1}')"

    # ---------------------------------------------------------- (a) fresh box, SHIPPED
    router_root_new
    check_eq "fresh box: /etc/config/nodogsplash is absent BEFORE the seed runs" "no" \
        "$([ -f "$TGOFFLINE_HARNESS_ROOT/etc/config/nodogsplash" ] && echo yes || echo no)"

    run_keepalive_seed
    check_rc "fresh box: the seed itself still exits 0 (it ignores uci errors)" 0 "$RC"
    keepalive_freshbox_asserts "fresh box:"

    # idempotency: re-running the seed must not duplicate the list members, and the
    # committed config must be byte-identical (uci add_list is a set; the section-ensure
    # is guarded by a `uci -q get`; the file-ensure never clobbers an existing file)
    local conf="$TGOFFLINE_HARNESS_ROOT/etc/config/nodogsplash" sha1 sha2
    sha1="$(sha256sum "$conf" | awk '{print $1}')"
    run_keepalive_seed
    check_rc "fresh box: the re-run exits 0" 0 "$RC"
    sha2="$(sha256sum "$conf" | awk '{print $1}')"
    check_eq "fresh box: re-running the seed leaves the config byte-identical" "$sha1" "$sha2"
    check_eq "fresh box: the MAC is not duplicated by a re-run" "1" \
        "$(grep -c 'AA:BB:CC:DD:EE:FF' "$conf")"
    check_eq "fresh box: the allow rule is not duplicated by a re-run" "1" \
        "$(grep -c 'allow tcp port 22' "$conf")"
    check_eq "fresh box: the anonymous section is not duplicated by a re-run" "1" \
        "$(grep -c '^nodogsplash$' "$TGOFFLINE_HARNESS_ROOT/etc/config/.nodogsplash.sections")"

    # ---------------------------------------------------------- (b) RED CONTROL
    # Reverse the fix on a SCRATCH COPY inside the harness — the repo template is never
    # touched.  Removing the file-ensure line is the whole fix under test, and the
    # reversal is matched by the FEED BUILDER's own regex for that line, so this control
    # cannot silently test something else.
    router_root_new
    mkdir -p "$seeded_ctl"
    if OUT="$(python3 - "$SCRIPTS_DIR/templates/99z-mgmt-keepalive" \
                       "$seeded_ctl/99z-mgmt-keepalive" <<'PY' 2>&1
import re, sys
# the feed builder's KEEPALIVE_FILE_ENSURE_RE: the shape it recognises as "already
# fresh-box safe".  Reversing BY THAT SHAPE proves the control removes exactly the
# step the shipped seed (and the builder) rely on.
RE = (r'^\s*\[[^\]]*etc/config/nodogsplash"[^\]]*\]\s*\|\|\s*:\s*>\s*'
      r'\S*etc/config/nodogsplash"')
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
out, dropped = [], 0
for line in text.splitlines(keepends=True):
    if re.search(RE, line.rstrip("\n")):
        dropped += 1
        continue
    out.append(line)
open(dst, "w", encoding="utf-8").write("".join(out))
if dropped != 1:
    print("CONTROL-FAILURE: expected exactly one file-ensure line, removed %d" % dropped)
    sys.exit(1)
if re.search(RE, "".join(out), re.M):
    print("CONTROL-FAILURE: a file-ensure line survived the reversal")
    sys.exit(1)
print("removed %d file-ensure line from the scratch seed copy" % dropped)
PY
)"; then
        pass "keepalive control: the file-ensure line is removed from a scratch copy ($(printf '%s' "$OUT" | tail -n1))"
    else
        fail "keepalive control: could not build the no-file-ensure seed — $(printf '%s' "$OUT" | tail -n1)"
    fi

    # the mechanism, pinned directly: real uci refuses to add a section to a config file
    # that does not exist (rc 3, "uci: Entry not found"), and succeeds once it does
    check_eq "keepalive control: the config file is absent on the fresh control box" "no" \
        "$([ -f "$TGOFFLINE_HARNESS_ROOT/etc/config/nodogsplash" ] && echo yes || echo no)"
    OUT="$(uci add nodogsplash nodogsplash 2>&1)"; RC=$?
    check_rc "keepalive control: uci add REFUSES while the config file does not exist" 3 "$RC"
    check_contains "keepalive control: the refusal is 'uci: Entry not found'" "uci: Entry not found" "$OUT"
    if [ -f "$TGOFFLINE_HARNESS_ROOT/etc/config/nodogsplash" ]; then
        fail "keepalive control: uci add created the config file itself (it must not)"
    else
        pass "keepalive control: uci add did NOT create the config file"
    fi
    : > "$TGOFFLINE_HARNESS_ROOT/etc/config/nodogsplash"
    OUT="$(uci add nodogsplash nodogsplash 2>&1)"; RC=$?
    check_rc "keepalive control: the SAME uci add exits 0 once the file exists" 0 "$RC"

    # now run the reversed seed on a fresh box and require the trust assertions to FAIL
    router_root_new
    run_keepalive_seed "$seeded_ctl/99z-mgmt-keepalive"
    check_rc "keepalive control: the reversed seed still exits 0 (it ignores errors)" 0 "$RC"
    check_contains "keepalive control: the reverse really lands no trust (uci: Entry not found emitted)" \
        "uci: Entry not found" "$OUT"

    # the SAME assertions that passed in (a), run against the reversed seed.  They run in
    # a SUBSHELL so their (required) failures do not count against the suite: a non-vacuity
    # control that itself failed the suite would be indistinguishable from a regression.
    # The same fact is then asserted POSITIVELY below, the way T20's controls do, so the
    # suite's own counts record it.
    echo "   --- red control: the SAME fresh-box assertions, against the reversed seed ---"
    echo "   --- (the FAILs below are REQUIRED and are NOT counted against the suite) ---"
    local redfile="$WORK/keepalive-control-failures" redcount
    ( b="$TESTS_FAILED"
      keepalive_freshbox_asserts "keepalive control:"
      printf '%s' "$((TESTS_FAILED - b))" > "$redfile" )
    redcount="$(cat "$redfile" 2>/dev/null || echo 0)"
    check_eq "keepalive control: the required verdict is that the config file is absent before/after the reversed seed" "no" \
        "$([ -f "$TGOFFLINE_HARNESS_ROOT/etc/config/nodogsplash" ] && echo yes || echo no)"
    if [ "${redcount:-0}" -ge 1 ]; then
        pass "keepalive control: the fresh-box assertions FAIL without the file-ensure line ($redcount of them, as required)"
    else
        fail "keepalive control: the fresh-box assertions PASSED with the file-ensure line removed — the green test is VACUOUS"
    fi

    # …and the same fact asserted POSITIVELY, so the control is recorded in the suite's
    # own counts: with the file-ensure line removed there is no config file, no section
    # and no trust at all — exactly the fresh-flash state that made install-router.sh
    # REFUSE(5).
    check_eq "keepalive control: no config file is created by the reversed seed" "no" \
        "$([ -f "$TGOFFLINE_HARNESS_ROOT/etc/config/nodogsplash" ] && echo yes || echo no)"
    check_eq "keepalive control: no anonymous nodogsplash section resolves" "" \
        "$(uci -q get 'nodogsplash.@nodogsplash[0]' 2>/dev/null)"
    check_not_contains "keepalive control: no trustedmac landed (the MAC the gate looks for)" \
        "AA:BB:CC:DD:EE:FF" \
        "$(uci -q get 'nodogsplash.@nodogsplash[0].trustedmac' 2>/dev/null)"
    check_not_contains "keepalive control: no users_to_router rule landed" "allow tcp port 22" \
        "$(uci -q get 'nodogsplash.@nodogsplash[0].users_to_router' 2>/dev/null)"

    # ------------------------------------------------ the shipped seed is untouched…
    check_eq "the CONTROL did not modify the repo template (sha unchanged)" "$seed_sha" \
        "$(sha256sum "$SCRIPTS_DIR/templates/99z-mgmt-keepalive" | awk '{print $1}')"

    # …and it still carries the ONE line the FEED BUILDER recognises via its
    # KEEPALIVE_FILE_ENSURE_RE as "already fresh-box safe".  If this drifts, the builder
    # stops recognising a pinned seed and RE-APPLIES its own file-ensure step — the
    # double-apply class of bug the feed PR removed.  Same shape the control reverses,
    # so the two can never disagree.
    if OUT="$(python3 - "$SCRIPTS_DIR/templates/99z-mgmt-keepalive" <<'PY' 2>&1
import re, sys
RE = (r'^\s*\[[^\]]*etc/config/nodogsplash"[^\]]*\]\s*\|\|\s*:\s*>\s*'
      r'\S*etc/config/nodogsplash"')
text = open(sys.argv[1], encoding="utf-8").read()
n = len(re.findall(RE, text, re.M))
print("file-ensure lines matched by the feed builder's regex: %d" % n)
sys.exit(0 if n == 1 else 1)
PY
)"; then
        pass "the shipped seed carries exactly one line the FEED BUILDER's KEEPALIVE_FILE_ENSURE_RE recognises (so it is never re-applied) — $(printf '%s' "$OUT" | tail -n1)"
    else
        fail "the shipped seed no longer matches the feed builder's KEEPALIVE_FILE_ENSURE_RE — $(printf '%s' "$OUT" | tail -n1)"
    fi
}

# =============================================================== runner
TITLES="
T01|dry-run: verify the bundle, print the ordered plan, touch nothing
T02|green path: keepalive first, deps by path, package, all gates, one report
T03|(a) a bundle missing a dependency refuses, naming it
T04|(b) a bundle without the keepalive seed refuses with the lockout reason
T05|(b2) a keepalive whose trust did not take refuses before installing
T06|(c) a runtime dep satisfied by an empty stub refuses (NDS would crash-loop)
T07|(c2) a router with no iptables payload refuses
T08|(d) a manifest mismatch refuses before installing anything
T09|(e) an apk the manifest does not name is refused
T10|(e2) the router-side name/hash binding gate refuses a foreign or renamed package
T11|an artifact without the admin-board guard fragment fails the gate
T12|a postinst that never reloads fw4 fails the guard gate (no manual reload, ever)
T13|a wrong surface code fails the surface gates
T14|no BOLT11 quote fails the quote gate
T15|a router with no SSH listener fails the ssh gate
T16|--force-missing-repositories is load-bearing (behaviour changes without it)
T17|the production scripts run under the router's shell (BusyBox ash / dash)
T18|staging never uses scp (stdin redirect only)
T19|the machine-readable report has every gate, in JSON, with the remote half embedded
T20|the dependency closure on a FRESH box (as-shipped refusal + upgrade-box control)
T21|the keepalive seed on a FRESH box (config file + section ensured; idempotent; red control)
"
TESTS="T01 T02 T03 T04 T05 T06 T07 T08 T09 T10 T11 T12 T13 T14 T15 T16 T17 T18 T19 T20 T21"
if [ -n "${1:-}" ] && [ "${1:-}" = "--only" ]; then ONLY="${2:-}"; fi
if [ -n "${TGOFFLINE_HARNESS_ONLY:-}" ]; then ONLY="$TGOFFLINE_HARNESS_ONLY"; fi

for id in $TESTS; do
    if [ -n "$ONLY" ] && [ "$ONLY" != "$id" ]; then continue; fi
    title="$(printf '%s\n' "$TITLES" | sed -n "s/^$id|//p")"
    t_begin "$id — $title"
    "test_$id"
done

summary
rc=$?
printf '\nworkdir kept for inspection: %s\n' "$WORK"
exit "$rc"
