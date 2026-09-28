#!/bin/sh
# =============================================================================
# scripts/offline/install-offline.sh
#
# The LAPTOP-side driver for the WAN-less (offline) install path, and the entry
# point of the `*-offline.tar.gz` release bundle.  One command, no internet on the
# router:
#
#     curl -fsSL <BUNDLE_URL> | tar -xz
#     cd tollgate-wrt-<version>-<arch>-offline
#     ./install-offline.sh 192.168.1.1 '<router-password>'
#
# The bundle is fetched where there IS internet (the operator's laptop); the router
# never needs an uplink.  Everything the router needs travels in the bundle:
# pkgs/ (the tollgate-wrt package plus the full dependency closure), MANIFEST.sha256,
# install-router.sh (the ordered router-side sequence), templates/99z-mgmt-keepalive
# and README.md.
#
# WHAT THIS DRIVER DOES
#   0. laptop-side preflight: the bundle's own sha256 manifest must verify, whole
#      and untouched, BEFORE a single byte is pushed to the router.
#   1. stage + APPLY the management keepalive seed — first, always (see
#      install-router.sh; skipping it bricked the bench on 2026-08-16/17).
#   2. stage the package closure through `ssh 'cat > …'` stdin redirects.  Never
#      scp: `scp -O` fails on a fresh dropbear because there is no sftp-server.
#   3. run the ordered router-side sequence and pass its exit code through.
#   4. probe the br-lan client view with the same surface contract the router side
#      uses (`/splash.html` on the portal, :2051 200|403, :8080 307|no plain-HTTP
#      answer — a plain 307 may not be required with redirect_https — and
#      :8090/:8443 refused) — a router cannot test its own br-lan drops, so this
#      half of the verification belongs to the workstation that is actually on
#      br-lan — then merge both halves into one machine-readable report.
#      The BOLT11/quote assertion is uplink-conditional and lives on the router side.
#
# Exit codes (identical to install-router.sh, plus the two laptop-side ones):
#   0 pass · 2 usage · 3 the bundle failed its own manifest check · 4 missing
#   dependency · 5 keepalive/lockout risk · 6 staged name/hash binding · 7 apk failed
#   · 8 runtime payload missing · 9 a post-install assertion failed ·
#   10 cannot reach the router · 11 router image unsupported for this lane
# =============================================================================

TGOFFLINE_VERSION="1.0.0"

# Test seam (never set in production; the harness double exports it): a harness root
# standing in for the router's filesystem, and PATH doubles for ssh/curl.
HARNESS_ROOT="${TGOFFLINE_HARNESS_ROOT:-}"

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLE_DIR="$SELF_DIR"
ROUTER=""
PASSWORD=""
PASSWORD_FILE=""
ROUTER_USER="root"
SSH_PORT=22
STAGE=/tmp/tgoffline
TRUST_MAC=""
IFACE=""
REPORT_OUT=""
DRY_RUN=0
REMOTE_SCRIPT=""
KEEPALIVE_TEMPLATE=""
MANIFEST=""
PKG_DIR=""
TMP=""

GATES=""
FACTS=""

gate() { printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$GATES"; }
gate_pass() { gate pass "$1" "$2"; }
gate_fail() { gate fail "$1" "$2"; }
fact() { printf '%s\t%s\n' "$1" "$2" >> "$FACTS"; }
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
word_in() { # word_in <word> <space-separated list>
    for w in $2; do [ "$w" = "$1" ] && return 0; done
    return 1
}
RP() { printf '%s%s' "$HARNESS_ROOT" "$1"; }   # router-side path, harness-prefixed

# shellcheck disable=SC2329  # invoked indirectly, from the EXIT/INT/TERM trap
cleanup() {
    [ -n "$TMP" ] && [ -d "$TMP" ] && rm -rf "$TMP"
    return 0
}
trap 'cleanup' EXIT INT TERM

usage() {
    cat <<'EOF'
usage: install-offline.sh <router> <password> [options]

Installs the TollGate package + its dependency closure on a router with NO uplink,
from an unpacked offline bundle.  Run it from the unpacked bundle directory.

options:
  --bundle DIR         bundle root (default: the directory of this script)
  --user USER          ssh user (default root)
  --port PORT          ssh port (default 22)
  --staging-dir PATH   staging directory on the router (default /tmp/tgoffline)
  --trust-mac MAC      management workstation MAC to trust pre-auth, in
                       AA:BB:CC:DD:EE:FF form (default: the --iface MAC, else the
                       MAC of the interface that routes to the router)
  --iface IFACE        interface whose MAC is trusted (default: autodetect)
  --password-file FILE read the router password from FILE instead of argv
  --report FILE        write the machine-readable report here
                       (default: ./offline-install-report.json)
  --dry-run            laptop-side checks + print the ordered plan; touches nothing
  --version            print the installer version and exit
EOF
}

# ------------------------------------------------------------------ argument parsing
while [ $# -gt 0 ]; do
    case "$1" in
        --bundle) BUNDLE_DIR="$2"; shift 2 ;;
        --user) ROUTER_USER="$2"; shift 2 ;;
        --port) SSH_PORT="$2"; shift 2 ;;
        --staging-dir) STAGE="$2"; shift 2 ;;
        --trust-mac) TRUST_MAC="$2"; shift 2 ;;
        --iface) IFACE="$2"; shift 2 ;;
        --password-file) PASSWORD_FILE="$2"; shift 2 ;;
        --report) REPORT_OUT="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --version) echo "install-offline.sh $TGOFFLINE_VERSION"; exit 0 ;;
        -h|--help) usage; exit 0 ;;
        -*) usage >&2; echo "unknown option: $1" >&2; exit 2 ;;
        *)
            if [ -z "$ROUTER" ]; then ROUTER="$1"
            elif [ -z "$PASSWORD" ]; then PASSWORD="$1"
            else usage >&2; echo "unexpected argument: $1" >&2; exit 2
            fi
            shift ;;
    esac
done

[ -n "$ROUTER" ] || { usage >&2; echo "the router address is required" >&2; exit 2; }
if [ -n "$PASSWORD_FILE" ]; then
    [ -r "$PASSWORD_FILE" ] || { echo "password file not readable: $PASSWORD_FILE" >&2; exit 2; }
    PASSWORD="$(cat "$PASSWORD_FILE")"
fi
[ -n "$PASSWORD" ] || [ -n "$HARNESS_ROOT" ] || {
    usage >&2; echo "the router password is required (argv or --password-file)" >&2; exit 2
}
BUNDLE_DIR="$(cd "$BUNDLE_DIR" && pwd)" || { echo "no such bundle directory" >&2; exit 2; }
TMP="$(mktemp -d "${TMPDIR:-/tmp}/tgoffline.XXXXXX")" || exit 2
GATES="$TMP/gates"; : > "$GATES"
FACTS="$TMP/facts"; : > "$FACTS"
[ -n "$REPORT_OUT" ] || REPORT_OUT="$PWD/offline-install-report.json"

echo "=== offline install driver v$TGOFFLINE_VERSION ==="
echo "router=$ROUTER user=$ROUTER_USER bundle=$BUNDLE_DIR"

# ------------------------------------------------------------------ locate bundle members
find_member() { # find_member <basename> [extra dirs...]
    _fm_name="$1"; shift
    for _fm_dir in "$BUNDLE_DIR" "$BUNDLE_DIR/scripts/offline" "$BUNDLE_DIR/offline" "$@"; do
        [ -f "$_fm_dir/$_fm_name" ] && { printf '%s' "$_fm_dir/$_fm_name"; return 0; }
    done
    return 1
}
[ -n "$REMOTE_SCRIPT" ] || REMOTE_SCRIPT="$(find_member install-router.sh)"
[ -n "$KEEPALIVE_TEMPLATE" ] || KEEPALIVE_TEMPLATE="$(
    find_member 99z-mgmt-keepalive "$BUNDLE_DIR/templates" "$BUNDLE_DIR/scripts/offline/templates"
)"
[ -n "$MANIFEST" ] || MANIFEST="$(find_member MANIFEST.sha256)"
[ -d "$PKG_DIR" ] || for d in "$BUNDLE_DIR/pkgs" "$BUNDLE_DIR/scripts/offline/pkgs"; do
    [ -d "$d" ] && PKG_DIR="$d" && break
done

# ------------------------------------------------------------------ 0. laptop preflight
echo ""
echo "=== (0) laptop-side preflight: the bundle must verify before anything is staged ==="

# The keepalive seed is checked FIRST and on its own: its absence is not "an
# incomplete bundle", it is a lockout risk, and it has to fail with that reason.
if [ -z "$KEEPALIVE_TEMPLATE" ]; then
    gate_fail keepalive_template "the bundle carries no templates/99z-mgmt-keepalive"
    echo "REFUSED(5): refusing to install: this bundle has no management keepalive seed (templates/99z-mgmt-keepalive). Without it the wired management workstation loses SSH the moment nodogsplash enforces on br-lan — that wedge bricked the bench on 2026-08-16/17. Rebuild the bundle with the seed included."
    exit 5
fi
if ! grep -q 'trustedmac' "$KEEPALIVE_TEMPLATE" || ! grep -q 'port 22' "$KEEPALIVE_TEMPLATE"; then
    gate_fail keepalive_template "$KEEPALIVE_TEMPLATE lacks the trustedmac / port-22 fragments"
    echo "REFUSED(5): refusing to install: the bundle's keepalive seed does not add both the workstation MAC to nodogsplash trustedmac and 'allow tcp port 22'. Installing without it can lock the operator out of SSH the moment nodogsplash enforces on br-lan (the 2026-08-16/17 wedge)."
    exit 5
fi
gate_pass keepalive_template "seed carries trustedmac + 'allow tcp port 22' ($KEEPALIVE_TEMPLATE)"

missing_members=""
[ -n "$REMOTE_SCRIPT" ] || missing_members="$missing_members install-router.sh"
[ -n "$MANIFEST" ] || missing_members="$missing_members MANIFEST.sha256"
[ -n "$PKG_DIR" ] || missing_members="$missing_members pkgs/"
if [ -n "$missing_members" ]; then
    gate_fail bundle_members "bundle is missing:$missing_members"
    echo "REFUSED(3): this is not a complete offline bundle — missing:$missing_members" >&2
    echo "REFUSED(3): this is not a complete offline bundle — missing:$missing_members"
    exit 3
fi
fact bundle_dir "$BUNDLE_DIR"
fact remote_script "$REMOTE_SCRIPT"
fact manifest "$MANIFEST"
fact pkg_dir "$PKG_DIR"

# the manifest covers EVERY bundle member: a tampered byte is refused before staging
manifest_out="$(cd "$BUNDLE_DIR" && sha256sum -c "$MANIFEST" 2>&1)"
manifest_rc=$?
manifest_bad="$(printf '%s' "$manifest_out" | grep -cE 'FAILED|No such file|ERROR')"
manifest_ok="$(printf '%s' "$manifest_out" | grep -c ': OK$')"
fact manifest_entries_ok "$manifest_ok"
if [ "$manifest_rc" != 0 ] || [ "$manifest_bad" != 0 ] || [ "$manifest_ok" = 0 ]; then
    gate_fail manifest_verified "sha256sum -c MANIFEST.sha256 failed ($manifest_bad bad entr(ies), rc=$manifest_rc)"
    printf '%s\n' "$manifest_out" | sed 's/^/    /'
    echo "REFUSED(3): the bundle does not match its own MANIFEST.sha256 — refusing before staging anything (the bundle's signed provenance is what makes the install auditable)."
    printf '%s\n' "$manifest_out" | grep -E 'FAILED|No such file' | sed 's/^/    /'
    exit 3
fi
gate_pass manifest_verified "$manifest_ok entr(ies) verified against MANIFEST.sha256"

# every package staged is a package the manifest attests to — refuse BEFORE pushing bytes
unnamed=""
for _apk in $(find "$PKG_DIR" -name '*.apk' -type f | sort); do
    _base="$(basename "$_apk")"
    if ! awk -v want="$_base" '
            { f = $2; sub(/^\*/, "", f); n = f; sub(/^.*\//, "", n) }
            n == want { found = 1 } END { exit !found }
        ' "$MANIFEST"; then
        unnamed="$unnamed $_base"
    fi
done
if [ -n "$unnamed" ]; then
    gate_fail bundle_package_names "not named in MANIFEST.sha256:$unnamed"
    echo "REFUSED(6): $PKG_DIR carries package(s) that MANIFEST.sha256 does not name:$unnamed — the installer installs only what the manifest attests to, so nothing was staged."
    exit 6
fi
gate_pass bundle_package_names "every package under pkgs/ is named in MANIFEST.sha256"

# the workstation MAC that will be trusted
if [ -z "$TRUST_MAC" ] && [ -n "$IFACE" ]; then
    TRUST_MAC="$(cat "/sys/class/net/$IFACE/address" 2>/dev/null)"
fi
if [ -z "$TRUST_MAC" ] && [ -z "$HARNESS_ROOT" ]; then
    if IFACE_AUTO="$(ip -o route get "$ROUTER" 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)"; then
        [ -n "$IFACE_AUTO" ] && TRUST_MAC="$(cat "/sys/class/net/$IFACE_AUTO/address" 2>/dev/null)"
    fi
fi
if [ -z "$TRUST_MAC" ] && [ -z "$HARNESS_ROOT" ]; then
    gate_fail trust_mac "could not determine the management workstation MAC"
    echo "REFUSED(5): could not determine the management workstation MAC (pass --trust-mac AA:BB:CC:DD:EE:FF). Without it the keepalive cannot trust this workstation pre-auth."
    exit 5
fi
[ -n "$TRUST_MAC" ] || TRUST_MAC="AA:BB:CC:DD:EE:FF"
fact trust_mac "$TRUST_MAC"
gate_pass trust_mac "will trust $TRUST_MAC in nodogsplash (pre-auth bypass + tcp/22)"
echo "trust-mac=$TRUST_MAC  staging-dir=$STAGE"

if [ "$DRY_RUN" = 1 ]; then
    echo ""
    echo "=== --dry-run: the ordered plan (nothing was staged, nothing was installed) ==="
    cat <<EOF
 1. laptop: sha256sum -c MANIFEST.sha256                       [done above]
 2. laptop: inject $TRUST_MAC into templates/99z-mgmt-keepalive
 3. router: ssh 'cat > $STAGE/99z-mgmt-keepalive' <seed   (stdin redirect; no scp)
 4. router: cp seed /etc/uci-defaults/99z-mgmt-keepalive && sh <seed>
 5. router: assert nodogsplash trustedmac + 'allow tcp port 22' are LIVE  (else REFUSE 5)
 6. router: ssh 'cat > $STAGE/pkgs/<member>' for every bundle package    (no scp)
 7. router: verify every staged apk's name+sha256 against MANIFEST.sha256 (else REFUSE 6)
 8. router: verify the required dependency closure is present             (else REFUSE 4)
 9. router: apk add --no-network --allow-untrusted --force-missing-repositories <deps by path>
10. router: assert every runtime dep actually landed a payload           (else REFUSE 8)
11. router: apk add ... <tollgate-wrt_*.apk>   (its postinst reloads the firewall and
            restarts nodogsplash — that is what compiles the pre-auth rules from the
            keepalive seeded in step 4. The installer never reloads fw4 itself.)
12. router: assert payload sha256 == the artifact's payload, version, the surfaces at the
            paths they actually serve (:2051 200|403, :2050 /splash.html 200, :2121 200,
            :8080 307|no plain-HTTP answer), guard fragment present AND loaded with NO
            manual reload, SSH 22 alive, and the /ln-invoice contract that fits the
            uplink: a real BOLT11 quote when a WAN is present, and a documented
            graceful-degradation answer (explicitly "not assertable") when there is none
13. laptop: probe the br-lan client view: :8090/:8443 refused, and merge one report
EOF
    echo ""
    echo "TGOFFLINE-RESULT DRY-RUN (bundle verified; the router was not touched)"
    exit 0
fi

# ------------------------------------------------------------------ 3. ssh toolchain
TMP_SSH="$TMP/ssh"; mkdir -p "$TMP_SSH"
SSH_OPTS="-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$TMP_SSH/known_hosts -o LogLevel=ERROR -p $SSH_PORT"
if command -v sshpass >/dev/null 2>&1; then
    printf '%s\n' "$PASSWORD" > "$TMP_SSH/pw"; chmod 600 "$TMP_SSH/pw"
    # shellcheck disable=SC2086
    SSH="sshpass -f $TMP_SSH/pw ssh $SSH_OPTS $ROUTER_USER@$ROUTER"
    fact ssh_auth "sshpass"
elif command -v ssh >/dev/null 2>&1; then
    # the generated askpass script must carry a literal $TGOFFLINE_PASSWORD
    # shellcheck disable=SC2016
    printf '#!/bin/sh\nprintf "%%s\\n" "$TGOFFLINE_PASSWORD"\n' > "$TMP_SSH/askpass"
    chmod 700 "$TMP_SSH/askpass"
    export TGOFFLINE_PASSWORD="$PASSWORD"
    export SSH_ASKPASS="$TMP_SSH/askpass"
    export SSH_ASKPASS_REQUIRE=force
    # shellcheck disable=SC2086
    SSH="ssh $SSH_OPTS $ROUTER_USER@$ROUTER"
    fact ssh_auth "ssh-askpass"
else
    echo "REFUSED(10): neither sshpass nor ssh is available on this laptop." >&2
    echo "REFUSED(10): neither sshpass nor ssh is available on this laptop."
    exit 10
fi

rssh() { # rssh <remote command>  — output on stdout, rc preserved
    # shellcheck disable=SC2086
    $SSH "$1"
}
rssh_stdin() { # rssh_stdin <local file> <remote command>
    # shellcheck disable=SC2086
    $SSH "$2" < "$1"
}

echo ""
echo "=== (0b) router reachability + identity ==="
if ! probe_out="$(rssh 'echo tgoffline-ssh-ok' 2>&1)"; then
    gate_fail router_reachable "ssh to $ROUTER_USER@$ROUTER:$SSH_PORT failed: $probe_out"
    echo "REFUSED(10): cannot ssh to $ROUTER_USER@$ROUTER:$SSH_PORT — $probe_out" >&2
    echo "REFUSED(10): cannot ssh to $ROUTER_USER@$ROUTER:$SSH_PORT — $probe_out"
    exit 10
fi
release_info="$(rssh 'cat /etc/openwrt_release 2>/dev/null' 2>/dev/null)"
rel_desc="$(printf '%s' "$release_info" | sed -n "s/^DISTRIB_DESCRIPTION='\(.*\)'$/\1/p")"
rel_ver="$(printf '%s' "$release_info" | sed -n "s/^DISTRIB_RELEASE='\(.*\)'$/\1/p")"
fact router_model "${rel_desc:-unknown}"
fact router_openwrt "${rel_ver:-unknown}"
gate_pass router_reachable "$rel_desc (OpenWrt ${rel_ver:-?})"
echo "$rel_desc / OpenWrt ${rel_ver:-unknown}"

pm="$(rssh 'command -v apk >/dev/null 2>&1 && echo apk || { command -v opkg >/dev/null 2>&1 && echo opkg || echo none; }' 2>/dev/null | tr -d '\r')"
fact package_manager "${pm:-unknown}"
case "$pm" in
    apk) gate_pass package_manager "apk (OpenWrt >= 25.x lane)" ;;
    opkg)
        gate_fail package_manager "opkg image — the offline bundle lane is apk-only"
        echo "REFUSED(11): this router image uses opkg (OpenWrt <= 24.10). The offline bundle lane is apk-only (OpenWrt >= 25.0): the .ipk dependency story differs and is explicitly out of scope for this bundle. Use the tollgate-installer for an opkg image."
        exit 11 ;;
    *)
        gate_fail package_manager "neither apk nor opkg on PATH"
        echo "REFUSED(11): no package manager (apk/opkg) on the router — is this an OpenWrt image?"
        exit 11 ;;
esac

# ------------------------------------------------------------------ 1. keepalive FIRST
echo ""
echo "=== (1) seed the management keepalive FIRST (before anything can enforce) ==="
mkdir -p "$TMP/seeded" 2>/dev/null || true
sed "s/__TRUST_MAC__/$TRUST_MAC/" "$KEEPALIVE_TEMPLATE" > "$TMP/seeded/99z-mgmt-keepalive"
if ! sh -n "$TMP/seeded/99z-mgmt-keepalive"; then
    gate_fail keepalive_template "the MAC-injected seed is not valid shell"
    echo "REFUSED(5): the MAC-injected keepalive seed is not valid shell — refusing to install." >&2
    exit 5
fi
if ! grep -q "$TRUST_MAC" "$TMP/seeded/99z-mgmt-keepalive"; then
    gate_fail keepalive_template "the MAC-injected seed does not carry $TRUST_MAC"
    echo "REFUSED(5): the MAC-injected keepalive seed does not carry $TRUST_MAC — refusing to install." >&2
    exit 5
fi
rssh "mkdir -p '$(RP "$STAGE")'/pkgs" || {
    gate_fail staging_dir "could not create $STAGE on the router"
    echo "REFUSED(10): could not create the staging directory $STAGE on the router." >&2
    exit 10
}
# stdin redirect, never scp: scp -O fails on a fresh dropbear (no sftp-server)
if ! rssh_stdin "$TMP/seeded/99z-mgmt-keepalive" "cat > '$(RP "$STAGE")'/99z-mgmt-keepalive"; then
    gate_fail keepalive_staged "ssh stdin redirect failed for the keepalive seed"
    echo "REFUSED(10): staging the keepalive seed over ssh stdin failed (scp is not used: scp -O fails on a fresh dropbear)." >&2
    exit 10
fi
gate_pass keepalive_staged "staged $STAGE/99z-mgmt-keepalive over an ssh stdin redirect (no scp)"

# ------------------------------------------------------------------ 2. stage the closure
echo ""
echo "=== (2) stage the bundle contents (ssh stdin redirect) ==="
stage_copy() { # stage_copy <local file> <remote relative path>
    if ! rssh_stdin "$1" "cat > '$(RP "$STAGE")'/$2"; then
        gate_fail staged_copy "$2 failed"
        echo "REFUSED(10): staging $2 over ssh stdin failed." >&2
        exit 10
    fi
    echo "  staged $2"
}
stage_copy "$MANIFEST" "MANIFEST.sha256"
stage_copy "$REMOTE_SCRIPT" "install-router.sh"
staged_n=0
for f in $(find "$PKG_DIR" -name '*.apk' -type f | sort); do
    stage_copy "$f" "pkgs/$(basename "$f")" || exit 10
    staged_n=$((staged_n + 1))
done
if [ "$staged_n" = 0 ]; then
    gate_fail staged_copy "no .apk files under $PKG_DIR"
    echo "REFUSED(3): the bundle carries no packages under $PKG_DIR." >&2
    exit 3
fi
fact staged_packages "$staged_n"
gate_pass staged_copy "$staged_n package(s) + MANIFEST.sha256 + install-router.sh staged"

# ------------------------------------------------------------------ 3. run the sequence
echo ""
echo "=== (3) the ordered router-side sequence ==="
remote_out="$(rssh "sh '$(RP "$STAGE")'/install-router.sh --staging-dir '$STAGE' --trust-mac '$TRUST_MAC' --report '$STAGE/report.json'" 2>&1)"
remote_rc=$?
printf '%s\n' "$remote_out"
fact remote_exit_code "$remote_rc"

remote_report="$(rssh "cat '$(RP "$STAGE")'/report.json" 2>/dev/null)"
if ! printf '%s' "$remote_report" | grep -q 'TGOFFLINE-REPORT'; then
    gate_fail remote_report "the router-side report could not be read back from $STAGE/report.json"
    remote_report=""
else
    gate_pass remote_report "read back $STAGE/report.json from the router"
fi

# ------------------------------------------------------------------ 4. br-lan client view
echo ""
echo "=== (4) br-lan client view (the client half of the surface assertions) ==="
# The SAME surface contract the router side asserts, observed from a br-lan client.
# Each entry is `scheme|port|path|accepted-codes`; the path is the one the surface
# actually serves and the codes are the correct answers for it:
#   :2051 /          200|403  uhttpd captive-portal site (403 = directory listing denied,
#                             the documented answer in scripts/tollgate-port-sweep.sh)
#   :2050 /splash.html   200  nodogsplash serves its splash there; the bare `/` is 404
#   :2121 /              200  tollgate backend API
#   :8080 /          307|000  LuCI with redirect_https: the plain-HTTP listener answers the
#                             redirect or nothing at all — a plain 307 may NOT be required.
#                             Anything else (e.g. 200) means the admin UI is served over
#                             plain HTTP without its TLS redirect, which still FAILS.
#   :8090/:8443          000  the admin-board pair must stay REFUSED from br-lan
CLIENT_SURFACE_CONTRACT='http|2051|/|200 403
http|2050|/splash.html|200
http|2121|/|200
http|8080|/|307 000
http|8090|/|000
https|8443|/|000'

# The portal's documented alternate shape, same rule as the router side: consulted only
# when the primary probe is neither accepted nor refused, so a box serving NEITHER the
# splash path nor `/` still fails.
#
# Expressed as three scalars and probed EXPLICITLY, not as a nested `while read … done
# <<EOF` inside the outer `while read … done <<EOF` loop: a here-doc read loop nested
# inside another here-doc read loop is a portability footgun under BusyBox ash (the
# router's own shell), and the router side needs no such construct here because there is
# exactly one documented alternate shape.
CLIENT_ALT_PORT=2050
CLIENT_ALT_PATH=/
CLIENT_ALT_ACCEPT='200'

client_bad=""
client_ok=""
while IFS="|" read -r scheme port path accept; do
    [ -n "$port" ] || continue
    got="$(curl -sk -o /dev/null -w '%{http_code}' -m 8 "$scheme://$ROUTER:$port$path" 2>/dev/null)"
    [ -n "$got" ] || got=000
    if ! word_in "$got" "$accept"; then
        # the primary answer is neither accepted nor refused: consult the ONE documented
        # alternate path for this port (if there is one) before declaring a failure.
        if [ "$port" = "$CLIENT_ALT_PORT" ]; then
            alt="$(curl -sk -o /dev/null -w '%{http_code}' -m 8 "$scheme://$ROUTER:$CLIENT_ALT_PORT$CLIENT_ALT_PATH" 2>/dev/null)"
            [ -n "$alt" ] || alt=000
            if word_in "$alt" "$CLIENT_ALT_ACCEPT"; then
                got="$alt"
                path="$CLIENT_ALT_PATH"
                accept="$CLIENT_ALT_ACCEPT"
                fact "client_${port}_alt_path" "$CLIENT_ALT_PATH"
            fi
        fi
    fi
    fact "client_$port" "$got"
    if word_in "$got" "$accept"; then
        case "$port" in
            8080)
                [ "$got" = 000 ] \
                    && client_ok="$client_ok :8080=no-plain-HTTP-answer" \
                    || client_ok="$client_ok :8080=$got" ;;
            8090|8443) client_ok="$client_ok :$port=refused" ;;
            *) client_ok="$client_ok :$port=$got" ;;
        esac
    else
        client_bad="$client_bad :$port$path=$got(want $(printf '%s' "$accept" | tr ' ' '/'))"
    fi
done <<EOF
$CLIENT_SURFACE_CONTRACT
EOF
if [ -n "$client_bad" ]; then
    gate_fail client_surfaces "wrong status from a br-lan client:$client_bad"
else
    gate_pass client_surfaces "from br-lan:$client_ok"
fi

# ------------------------------------------------------------------ report
result=FAIL
if [ "$remote_rc" = 0 ] && [ -z "$client_bad" ]; then
    result=PASS
fi
echo ""
echo "=== offline install report (laptop side) — v$TGOFFLINE_VERSION ==="
while IFS="$(printf '\t')" read -r state name detail; do
    case "$state" in pass) tag=PASS ;; fail) tag=FAIL ;; *) tag=INFO ;; esac
    printf 'gate %-30s %-4s %s\n' "$name" "$tag" "$detail"
done < "$GATES"

{
    echo "TGOFFLINE-REPORT-BEGIN"
    printf '{\n'
    printf ' "installer_version": "%s",\n' "$TGOFFLINE_VERSION"
    printf ' "side": "laptop",\n'
    printf ' "router": "%s",\n' "$(json_escape "$ROUTER")"
    printf ' "result": "%s",\n' "$result"
    printf ' "remote_result": "%s",\n' "$([ "$remote_rc" = 0 ] && echo PASS || echo "FAIL(exit $remote_rc)")"
    while IFS="$(printf '\t')" read -r key value; do
        printf ' "fact_%s": "%s",\n' "$key" "$(json_escape "$value")"
    done < "$FACTS"
    while IFS="$(printf '\t')" read -r state name detail; do
        printf ' "gate_%s": "%s",\n' "$name" "$state"
    done < "$GATES"
    printf ' "remote_report": '
    if [ -n "$remote_report" ]; then
        printf '%s' "$remote_report" | sed -n '/TGOFFLINE-REPORT-BEGIN/,/TGOFFLINE-REPORT-END/p' | sed '1d;$d'
    else
        printf 'null'
    fi
    printf '\n}\n'
    echo "TGOFFLINE-REPORT-END"
} > "$REPORT_OUT.$$" 2>/dev/null && mv "$REPORT_OUT.$$" "$REPORT_OUT"

echo "report written: $REPORT_OUT"
echo "TGOFFLINE-RESULT $result"
if [ "$result" != PASS ]; then
    [ "$remote_rc" != 0 ] && exit "$remote_rc"
    exit 9
fi
exit 0
