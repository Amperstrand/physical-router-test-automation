#!/bin/sh
# =============================================================================
# tests/offline-install/harness/make-bundle.sh — build a fixture offline bundle.
#
# Produces the SAME layout the feed's build-offline-bundle produces, so the harness
# exercises the real bundle contract:
#
#   <dest>/install-offline.sh                      (the production laptop driver)
#   <dest>/install-router.sh                       (the production router-side script)
#   <dest>/templates/99z-mgmt-keepalive            (the production seed template)
#   <dest>/pkgs/<the tollgate-wrt package + the dependency closure>
#   <dest>/MANIFEST.sha256
#   <dest>/README.md
#
# The fixture packages are tar.gz stand-ins for apk v3 artifacts: the production
# scripts never parse the package format themselves, they hand the files to apk — and
# the harness's apk double understands the fixture format (PKGINFO + postinst +
# payload).  That keeps the suite dependency-free (no apk-tools, no network) and
# therefore skip-free.
#
# usage: make-bundle.sh <destdir> [variant...]
#
# variants (each one is a negative control from the card):
#   drop-dep=NAME        omit that dependency's package from pkgs/
#   stub-dep=NAME        ship that dependency as an EMPTY (payload-less) package
#   no-keepalive         omit templates/99z-mgmt-keepalive
#   no-guard             the package payload ships no 31-admin-board-*.nft fragment
#   no-reload-postinst   the package postinst does not reload fw4
#   foreign-apk=NAME     ship an extra package the manifest does not name
#   tamper[=FILE]        flip a byte of FILE (default: the first .apk) AFTER the
#                        manifest is written, so the bundle stops matching it
#   no-readme            omit README.md (an incomplete bundle)
# =============================================================================
set -eu

DEST="${1:?usage: make-bundle.sh <destdir> [variant...]}"
shift || true

SCRIPTS_DIR="${TGOFFLINE_HARNESS_SCRIPTS:-$(cd "$(dirname "$0")/../../../scripts/offline" && pwd)}"
ARCH="aarch64_cortex-a53"
PKG_VERSION="0.6.0_alpha4_pre17"
TOLLGATE_APK="tollgate-wrt_${PKG_VERSION}_${ARCH}.apk"

DROP_DEP=""
STUB_DEP=""
NO_KEEPALIVE=0
NO_GUARD=0
NO_RELOAD=0
FOREIGN_APK=""
TAMPER=""
NO_README=0

for v in "$@"; do
    case "$v" in
        drop-dep=*) DROP_DEP="${v#drop-dep=}" ;;
        stub-dep=*) STUB_DEP="${v#stub-dep=}" ;;
        no-keepalive) NO_KEEPALIVE=1 ;;
        no-guard) NO_GUARD=1 ;;
        no-reload-postinst) NO_RELOAD=1 ;;
        foreign-apk=*) FOREIGN_APK="${v#foreign-apk=}" ;;
        tamper) TAMPER="__first_apk__" ;;
        tamper=*) TAMPER="${v#tamper=}" ;;
        no-readme) NO_README=1 ;;
        *) echo "make-bundle: unknown variant '$v'" >&2; exit 2 ;;
    esac
done

rm -rf "$DEST"
mkdir -p "$DEST/pkgs" "$DEST/templates"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/offline-bundle.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT INT TERM

# ------------------------------------------------------------------ production members
cp "$SCRIPTS_DIR/install-offline.sh" "$DEST/install-offline.sh"
chmod +x "$DEST/install-offline.sh"
cp "$SCRIPTS_DIR/install-router.sh" "$DEST/install-router.sh"
chmod +x "$DEST/install-router.sh"
[ "$NO_KEEPALIVE" = 1 ] || cp "$SCRIPTS_DIR/templates/99z-mgmt-keepalive" "$DEST/templates/99z-mgmt-keepalive"
if [ "$NO_README" = 0 ]; then
    cat > "$DEST/README.md" <<EOF
# tollgate-wrt ${PKG_VERSION} — offline (WAN-less) install bundle

    ./install-offline.sh 192.168.1.1 '<router-password>'

The router needs no uplink: every package it needs is in \`pkgs/\`, and
\`install-offline.sh\` seeds the management keepalive before anything can enforce.
See docs/offline-install.md in the repository.
EOF
fi

# ------------------------------------------------------------------ fixture package builder
apk_build() { # apk_build <out> <name> <version> <depends> <postinst-file|-> <payload-dir|->
    out="$1"; name="$2"; ver="$3"; dep="$4"; post="$5"; pay="$6"
    tree="$SCRATCH/tree.$$.$name"
    rm -rf "$tree"; mkdir -p "$tree"
    {
        echo "name=$name"
        echo "version=$ver"
        echo "arch=$ARCH"
        echo "depends=$dep"
        echo "provides=$name-any"
        echo "description=offline-harness fixture for $name"
    } > "$tree/PKGINFO"
    if [ "$post" != "-" ] && [ -f "$post" ]; then
        cp "$post" "$tree/postinst"
    fi
    if [ "$pay" != "-" ] && [ -d "$pay" ]; then
        ( cd "$pay" && tar cf - . ) | ( cd "$tree" && tar xf - )
    fi
    ( cd "$tree" && tar czf "$out" . )
    rm -rf "$tree"
}

payload_nodogsplash="$SCRATCH/pay_nodogsplash"
mkdir -p "$payload_nodogsplash/usr/bin" "$payload_nodogsplash/etc/init.d"
cat > "$payload_nodogsplash/usr/bin/nodogsplash" <<'EOF'
#!/bin/sh
# fixture payload: the real nodogsplash also execs `iptables --version` at start-up
echo "nodogsplash 5.0.2 (harness fixture)"
EOF
chmod 755 "$payload_nodogsplash/usr/bin/nodogsplash"
cat > "$payload_nodogsplash/etc/init.d/nodogsplash" <<'EOF'
#!/bin/sh
echo "nodogsplash init (harness fixture)"
EOF
chmod 755 "$payload_nodogsplash/etc/init.d/nodogsplash"

payload_jq="$SCRATCH/pay_jq"
mkdir -p "$payload_jq/usr/bin"
cat > "$payload_jq/usr/bin/jq" <<'EOF'
#!/bin/sh
echo '{}'
EOF
chmod 755 "$payload_jq/usr/bin/jq"

payload_mhd="$SCRATCH/pay_mhd"
mkdir -p "$payload_mhd/usr/lib"
printf 'libmicrohttpd fixture payload\n' > "$payload_mhd/usr/lib/libmicrohttpd.so.12"

# The iptables closure nodogsplash needs ON TOP of the feed's DEPENDS+= line — the
# packages the released pre19 bundle stages, and the ones the dependency stage never
# offered to apk.  Their payloads deliberately do NOT include /usr/sbin/iptables: on
# this fixture router that binary belongs to the BASE IMAGE (see harness/lib.sh), which
# is exactly what the `no-iptables` variant removes to prove the runtime-payload gate
# fires.  A shortcut here would make T07 vacuous.
payload_iptables_nft="$SCRATCH/pay_iptables_nft"
mkdir -p "$payload_iptables_nft/usr/sbin"
cat > "$payload_iptables_nft/usr/sbin/iptables-nft-multi" <<'EOF'
#!/bin/sh
echo "iptables v1.8.10 (nf_tables) [harness fixture]"
EOF
chmod 755 "$payload_iptables_nft/usr/sbin/iptables-nft-multi"

payload_xtables_nft="$SCRATCH/pay_xtables_nft"
mkdir -p "$payload_xtables_nft/usr/lib/xtables"
printf 'xtables-nft fixture payload\n' > "$payload_xtables_nft/usr/lib/xtables/libxt_standard.so"

payload_libxtables="$SCRATCH/pay_libxtables"
mkdir -p "$payload_libxtables/usr/lib"
printf 'libxtables fixture payload\n' > "$payload_libxtables/usr/lib/libxtables.so.12"

payload_mod_conntrack="$SCRATCH/pay_mod_conntrack"
payload_mod_ipopt="$SCRATCH/pay_mod_ipopt"
payload_mod_nat="$SCRATCH/pay_mod_nat"
for d in "$payload_mod_conntrack" "$payload_mod_ipopt" "$payload_mod_nat"; do
    mkdir -p "$d/usr/lib/iptables"
    printf '%s fixture payload\n' "$(basename "$d")" > "$d/usr/lib/iptables/$(basename "$d")"
done

payload_tollgate="$SCRATCH/pay_tollgate"
mkdir -p "$payload_tollgate/usr/bin" "$payload_tollgate/etc/init.d" \
         "$payload_tollgate/etc/uci-defaults" "$payload_tollgate/lib/upgrade/keep.d"
# a deterministic, non-trivial payload (the identity gate compares its sha256)
{
    echo "#!/bin/sh"
    echo "# harness fixture standing in for the tollgate-wrt payload binary"
    i=0
    while [ $i -lt 64 ]; do
        echo "# deterministic fixture payload line $i: tollgate-wrt ${PKG_VERSION} ${ARCH}"
        i=$((i + 1))
    done
    echo 'echo "tollgate-wrt fixture"'
} > "$payload_tollgate/usr/bin/tollgate-wrt"
chmod 755 "$payload_tollgate/usr/bin/tollgate-wrt"
if [ "$NO_GUARD" = 0 ]; then
    mkdir -p "$payload_tollgate/etc/nftables.d"
    cat > "$payload_tollgate/etc/nftables.d/31-admin-board-not-guest-reachable.nft" <<'EOF'
#!/usr/sbin/nft -f
# Keep the admin board off the captive bridge (fixture copy of the real #566 guard).
table inet fw4 {
    chain admin_board_input_guard {
        type filter hook input priority filter - 1; policy accept;
        meta nfproto ipv4 iifname "br-lan" tcp dport { 8090, 8443 } counter packets 0 bytes 0 drop
        meta nfproto ipv6 iifname "br-lan" tcp dport { 8090, 8443 } counter packets 0 bytes 0 drop
    }
}
EOF
fi

postinst_dir="$SCRATCH/postinst"
mkdir -p "$postinst_dir"
cat > "$postinst_dir/nodogsplash" <<'EOF'
#!/bin/sh
/etc/init.d/nodogsplash enable
EOF
if [ "$NO_RELOAD" = 1 ]; then
    cat > "$postinst_dir/tollgate" <<'EOF'
#!/bin/sh
# the pre17 feed-recipe shape: uci-defaults run, the service is restarted, and NOBODY
# reloads the firewall — so /etc/nftables.d/31-admin-board-*.nft stays unloaded
/etc/init.d/tollgate-wrt enable
EOF
else
    cat > "$postinst_dir/tollgate" <<'EOF'
#!/bin/sh
/etc/init.d/network restart
/etc/init.d/firewall reload
/etc/init.d/nodogsplash restart

for s in 90-tollgate-captive-portal-symlink 99-tollgate-setup 92-tollgate-admin-setup; do
    [ -x "/etc/uci-defaults/$s" ] && sh "/etc/uci-defaults/$s"
done

/etc/init.d/tollgate-wrt enable
/etc/init.d/tollgate-wrt restart
EOF
fi

# ------------------------------------------------------------------ pkgs/
build_dep() { # build_dep <name> <version> <depends> <payload-dir>
    n="$1"; v="$2"; d="$3"; p="$4"
    if [ "$DROP_DEP" = "$n" ]; then
        return 0
    fi
    if [ "$STUB_DEP" = "$n" ]; then
        apk_build "$DEST/pkgs/$n-$v.apk" "$n" "$v" "$d" "-" "-"
    else
        apk_build "$DEST/pkgs/$n-$v.apk" "$n" "$v" "$d" "-" "$p"
    fi
}
build_dep nodogsplash "5.0.2-r1" "libmicrohttpd-no-ssl libpthread iptables-nft iptables-mod-conntrack-extra iptables-mod-ipopt iptables-mod-nat-extra" "$payload_nodogsplash"
build_dep jq "1.8.1-r2" "libc" "$payload_jq"
build_dep libmicrohttpd-no-ssl "1.0.2-r1" "libc" "$payload_mhd"
# the stale virtual dep: an empty stub is the CORRECT answer for these
build_dep libpthread "1.0" "" "-"
# nodogsplash's iptables closure: NOT reachable from REQUIRED_DEPS, and the reason the
# dependency stage has to offer everything staged rather than the top-level deps only.
# `kernel` is deliberately NOT built here — the base image provides it, as it does on a
# real router.
build_dep iptables-nft "1.8.10-r3" "libxtables xtables-nft kernel" "$payload_iptables_nft"
build_dep xtables-nft "1.8.10-r3" "libxtables" "$payload_xtables_nft"
build_dep libxtables "1.8.10-r3" "libc" "$payload_libxtables"
build_dep iptables-mod-conntrack-extra "1.8.10-r3" "libxtables" "$payload_mod_conntrack"
build_dep iptables-mod-ipopt "1.8.10-r3" "libxtables" "$payload_mod_ipopt"
build_dep iptables-mod-nat-extra "1.8.10-r3" "libxtables" "$payload_mod_nat"

apk_build "$DEST/pkgs/$TOLLGATE_APK" "tollgate-wrt" "${PKG_VERSION}-r1" \
    "jq libc nodogsplash" "$postinst_dir/tollgate" "$payload_tollgate"

# ------------------------------------------------------------------ MANIFEST.sha256
( cd "$DEST" && find . -type f ! -name 'MANIFEST.sha256' | sed 's|^\./||' | sort | xargs sha256sum ) \
    > "$DEST/MANIFEST.sha256"

# a package the manifest does not name, dropped in AFTER the manifest is written (the
# `foreign-apk` control): the bundle still verifies with `sha256sum -c`, but the
# installer must refuse to install a package the manifest does not attest to
if [ -n "$FOREIGN_APK" ]; then
    printf 'not a package the manifest names\n' > "$DEST/pkgs/$FOREIGN_APK"
fi

# ------------------------------------------------------------------ tamper (after signing)
if [ -n "$TAMPER" ]; then
    target="$TAMPER"
    if [ "$target" = "__first_apk__" ]; then
        target="pkgs/$(ls "$DEST/pkgs" | head -1)"
    fi
    # flip the first byte of the file, preserving its length
    python3 - "$DEST/$target" <<'PY'
import sys
path = sys.argv[1]
with open(path, "r+b") as fh:
    first = fh.read(1)
    fh.seek(0)
    fh.write(bytes([first[0] ^ 0x01]))
PY
fi

printf 'bundle: %s (%s packages, %s)\n' "$DEST" \
    "$(ls "$DEST/pkgs" | wc -l | tr -d ' ')" \
    "$(wc -l < "$DEST/MANIFEST.sha256" | tr -d ' ')"
