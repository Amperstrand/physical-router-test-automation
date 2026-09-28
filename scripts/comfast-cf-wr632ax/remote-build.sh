#!/usr/bin/env bash
# remote-build.sh — runs ON the build host (never on a small-/tmp workstation).
#
# Downloaded, hash-verified and executed by scripts/comfast-cf-wr632ax/build-image.sh
# over SSH.  It is deliberately self-contained: it downloads the OpenWrt
# ImageBuilder and the TollGate module apk, verifies BOTH against their
# published checksums, stages files/ from a pre-staged tarball, builds the
# sysupgrade image and prints the artifact path, sha256 and size.
#
# No secrets are handled here.  No credentials, no device access.
#
# Usage (invoked by build-image.sh, not by hand):
#   remote-build.sh <openwrt-release> <tollgate-release> <tollgate-asset> \
#                   <expected-apk-sha256> <files-tarball> <work-dir>
set -euo pipefail

OPENWRT_RELEASE="${1:?openwrt release}"
TOLLGATE_RELEASE="${2:?tollgate release}"
TOLLGATE_ASSET="${3:?tollgate asset filename}"
EXPECTED_APK_SHA="${4:?expected tollgate apk sha256}"
FILES_TARBALL="${5:?staged files tarball (absolute path)}"
WORK_DIR="${6:?work dir (absolute path)}"

TARGET_PATH="mediatek/filogic"
PROFILE="comfast_cf-wr632ax"

IB_BASE="openwrt-imagebuilder-${OPENWRT_RELEASE}-mediatek-filogic.Linux-x86_64"
IB_URL="https://downloads.openwrt.org/releases/${OPENWRT_RELEASE}/targets/${TARGET_PATH}/${IB_BASE}.tar.zst"
SUMS_URL="https://downloads.openwrt.org/releases/${OPENWRT_RELEASE}/targets/${TARGET_PATH}/sha256sums"

TG_REPO="${TOLLGATE_REPO:-FreedomTechFeed/packages}"
TG_ASSET_URL="https://github.com/${TG_REPO}/releases/download/${TOLLGATE_RELEASE}/${TOLLGATE_ASSET}"
TG_SUMS_URL="https://github.com/${TG_REPO}/releases/download/${TOLLGATE_RELEASE}/SHA256SUMS"

# Packages resolvable from the OFFICIAL release feeds.  tollgate-wrt is NOT one
# of them (it ships from FreedomTechFeed/packages, not downloads.openwrt.org),
# so it is baked in through FILES= + a one-shot first-boot offline install.
PACKAGES="nodogsplash jq libmicrohttpd-no-ssl luci"

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

log "host: $(hostname) — $(nproc) cores, $(df -h "$WORK_DIR" 2>/dev/null | awk 'NR==2{print $4" free on "$6}')"
mkdir -p "$WORK_DIR/dl/tollgate"

# ─── 0. Host prereq preflight ───────────────────────────────────────────────
# The ImageBuilder's own prereq check needs GNU awk SPECIFICALLY.  Debian ships
# mawk as /usr/bin/awk by default and the failure surfaces as a bare
# "Checking 'awk'... failed." / "Please install GNU 'awk'" with NO hint that the
# fix is `apt-get install gawk`.  Name it here instead of failing mysteriously.
for t in make gcc g++ wget curl tar zstd sha256sum file; do
    command -v "$t" >/dev/null 2>&1 || die "build host is missing a required tool: $t"
done
if ! awk --version 2>&1 | grep -qi 'GNU Awk'; then
    die "build host awk is not GNU awk (install gawk: sudo apt-get install -y gawk)"
fi
log "host prereqs ok (GNU awk: $(awk --version 2>&1 | head -n1))"

# ─── 1. ImageBuilder download + sha256 verification against release sums ─────
SUMS_FILE="$WORK_DIR/dl/sha256sums-${OPENWRT_RELEASE}.txt"
if [ ! -s "$SUMS_FILE" ]; then
    log "fetching release checksums: $SUMS_URL"
    curl -fsSL -o "$SUMS_FILE.part" "$SUMS_URL" && mv "$SUMS_FILE.part" "$SUMS_FILE"
fi
# sums are "<sha> *<filename>" (binary marker) or "<sha>  <filename>".
EXPECTED_IB_SHA="$(awk -v f="$IB_BASE.tar.zst" '{n=$2; sub(/^\*/,"",n); if (n==f) print $1}' "$SUMS_FILE" | head -n1)"
[ -n "$EXPECTED_IB_SHA" ] || die "no sha256 entry for ${IB_BASE}.tar.zst in $SUMS_FILE"

IB_TAR="$WORK_DIR/dl/${IB_BASE}.tar.zst"
if [ ! -s "$IB_TAR" ] || ! echo "$EXPECTED_IB_SHA  $IB_TAR" | sha256sum -c - >/dev/null 2>&1; then
    log "downloading ImageBuilder (~513 MB, resumable): $IB_URL"
    curl -fL -C - -o "$IB_TAR" "$IB_URL"
fi
log "verifying ImageBuilder sha256"
echo "$EXPECTED_IB_SHA  $IB_TAR" | sha256sum -c - || die "ImageBuilder sha256 MISMATCH"
log "ImageBuilder OK: $EXPECTED_IB_SHA"

# ─── 2. TollGate module apk download + sha256 verification ───────────────────
APK_FILE="$WORK_DIR/dl/tollgate/$TOLLGATE_ASSET"
if [ -f "$APK_FILE" ] && [ "$(sha256sum "$APK_FILE" | awk '{print $1}')" = "$EXPECTED_APK_SHA" ]; then
    log "tollgate apk already present and hash-correct"
else
    log "downloading $TOLLGATE_ASSET from $TG_REPO@$TOLLGATE_RELEASE"
    curl -fL -o "$APK_FILE.part" "$TG_ASSET_URL" && mv "$APK_FILE.part" "$APK_FILE"
    # cross-check against the release's own SHA256SUMS asset when reachable.
    if curl -fsSL -o "$WORK_DIR/dl/tollgate/SHA256SUMS" "$TG_SUMS_URL"; then
        RELEASE_SHA="$(awk -v f="$TOLLGATE_ASSET" '{n=$2; sub(/^\*/,"",n); if (n==f) print $1}' "$WORK_DIR/dl/tollgate/SHA256SUMS" | head -n1)"
        [ -n "$RELEASE_SHA" ] || die "no sha256 entry for $TOLLGATE_ASSET in the release SHA256SUMS"
        [ "$RELEASE_SHA" = "$EXPECTED_APK_SHA" ] || die "release SHA256SUMS disagrees with the pinned sha256 ($RELEASE_SHA != $EXPECTED_APK_SHA)"
        log "release SHA256SUMS agrees: $RELEASE_SHA"
    else
        log "WARN: could not fetch the release SHA256SUMS asset; relying on the pinned sha256"
    fi
fi
ACTUAL_APK_SHA="$(sha256sum "$APK_FILE" | awk '{print $1}')"
[ "$ACTUAL_APK_SHA" = "$EXPECTED_APK_SHA" ] || die "tollgate apk sha256 MISMATCH ($ACTUAL_APK_SHA != $EXPECTED_APK_SHA)"
log "tollgate apk OK: $ACTUAL_APK_SHA"

# ─── 3. Extract the ImageBuilder ────────────────────────────────────────────
IB_DIR="$WORK_DIR/$IB_BASE"
if [ ! -d "$IB_DIR" ]; then
    log "extracting ImageBuilder"
    zstd -t "$IB_TAR"
    tar --zstd -xf "$IB_TAR" -C "$WORK_DIR"
fi
[ -f "$IB_DIR/Makefile" ] || die "ImageBuilder tree looks wrong (no Makefile): $IB_DIR"
grep -q "comfast_cf-wr632ax" "$IB_DIR/.targetinfo" 2>/dev/null \
    || log "note: could not confirm the profile in .targetinfo (will fail loudly at make if absent)"

# ─── 4. Stage files/ (repo skeleton + the verified apk) ─────────────────────
log "staging files/ from $FILES_TARBALL"
rm -rf "$WORK_DIR/files"
mkdir -p "$WORK_DIR/files"
tar -xf "$FILES_TARBALL" -C "$WORK_DIR"
mkdir -p "$WORK_DIR/files/root"
install -m 0644 "$APK_FILE" "$WORK_DIR/files/root/tollgate-wrt.apk"
chmod 0755 "$WORK_DIR/files/etc/uci-defaults/99-tollgate-firstboot"
# sanity: the baked-in apk inside files/ must be byte-identical to the verified one
[ "$(sha256sum "$WORK_DIR/files/root/tollgate-wrt.apk" | awk '{print $1}')" = "$EXPECTED_APK_SHA" ] \
    || die "staged apk inside files/ does not match the verified apk"

# ─── 5. Build ───────────────────────────────────────────────────────────────
log "building: PROFILE=$PROFILE PACKAGES=$PACKAGES"
BUILD_LOG="$WORK_DIR/dl/build.log"
(
  cd "$IB_DIR"
  make image \
      PROFILE="$PROFILE" \
      PACKAGES="$PACKAGES" \
      FILES="$WORK_DIR/files" \
      TMPDIR="$WORK_DIR/tmp"
) >"$BUILD_LOG" 2>&1 || {
    log "BUILD FAILED — last 40 lines of $BUILD_LOG:"
    tail -n 40 "$BUILD_LOG" >&2
    die "make image returned non-zero (full log: $BUILD_LOG)"
}
log "build log: $BUILD_LOG ($(wc -l < "$BUILD_LOG") lines)"
# The per-package install lines are the evidence that PACKAGES= actually resolved.
log "packages installed by the ImageBuilder:"
grep -E '^Installing |^Checking .*\.\.\. ok' "$BUILD_LOG" | sed 's/^/    /' || true

# ─── 6. Locate the artifact ─────────────────────────────────────────────────
# OpenWrt 25.12.x ImageBuilder writes bin/targets/<target>/ directly (flat), not
# bin/targets/<target>/<profile>/ as older releases did.  Accept both layouts.
TARGET_OUT_DIR="$IB_DIR/bin/targets/${TARGET_PATH}"
PROFILE_OUT_DIR="$TARGET_OUT_DIR/${PROFILE}"
if [ -d "$PROFILE_OUT_DIR" ]; then
    OUT_DIR_REMOTE="$PROFILE_OUT_DIR"
elif [ -d "$TARGET_OUT_DIR" ]; then
    OUT_DIR_REMOTE="$TARGET_OUT_DIR"
else
    die "no output dir under $TARGET_OUT_DIR"
fi
log "image output dir: $OUT_DIR_REMOTE"
SRC_BIN="$(find "$OUT_DIR_REMOTE" -maxdepth 1 -name "*${PROFILE}*-squashfs-sysupgrade.bin" | sort | head -n1)"
[ -n "$SRC_BIN" ] && [ -s "$SRC_BIN" ] || die "no sysupgrade image produced under $OUT_DIR_REMOTE"

echo "ARTIFACT_PATH=$SRC_BIN"
echo "ARTIFACT_SHA256=$(sha256sum "$SRC_BIN" | awk '{print $1}')"
echo "ARTIFACT_SIZE=$(wc -c < "$SRC_BIN")"
# Package inventory inside the image: the ImageBuilder writes a .manifest beside
# the image listing every package it baked in.
MANIFEST="$(find "$OUT_DIR_REMOTE" -maxdepth 1 -name '*.manifest' | sort | head -n1)"
if [ -n "$MANIFEST" ]; then
    echo "MANIFEST_PATH=$MANIFEST"
fi

# ─── 7. Prove the baked-in files really landed in the image tree ────────────
# Stronger than reading the build log: read the assembled rootfs and the
# squashfs that is embedded in the image.
ROOTFS_DIR="$(find "$IB_DIR/build_dir" -maxdepth 2 -type d -name 'root-mediatek' 2>/dev/null | head -n1)"
if [ -n "$ROOTFS_DIR" ]; then
    log "rootfs staging dir: $ROOTFS_DIR"
    for f in root/tollgate-wrt.apk etc/uci-defaults/99-tollgate-firstboot; do
        if [ -f "$ROOTFS_DIR/$f" ]; then
            echo "ROOTFS_FILE=$f sha256=$(sha256sum "$ROOTFS_DIR/$f" | awk '{print $1}') mode=$(stat -c %a "$ROOTFS_DIR/$f")"
        else
            die "baked file MISSING from the image rootfs: $f"
        fi
    done
    [ "$(sha256sum "$ROOTFS_DIR/root/tollgate-wrt.apk" | awk '{print $1}')" = "$EXPECTED_APK_SHA" ] \
        || die "the apk inside the image rootfs is NOT the verified apk"
    log "baked apk inside the image rootfs is byte-identical to the verified apk"
fi

SQFS="$(find "$IB_DIR/build_dir" -maxdepth 3 -name 'root.squashfs' 2>/dev/null | head -n1)"
echo "SQFS_PATH=${SQFS:-<none>}"
if [ -n "$SQFS" ] && command -v unsquashfs >/dev/null 2>&1; then
    log "files inside the built squashfs (grep for the baked ones):"
    unsquashfs -l "$SQFS" 2>/dev/null | grep -E 'tollgate-wrt\.apk|99-tollgate-firstboot' | sed 's/^/    /' \
        || log "    (neither baked file listed — investigate before publishing)"
fi
log "remote build complete"
