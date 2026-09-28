#!/usr/bin/env bash
# Build a flashable OpenWrt sysupgrade image for the COMFAST CF-WR632AX with the
# TollGate module baked in.
#
# Why ImageBuilder FILES + a first-boot install (and not PACKAGES=): tollgate-wrt
# is published from FreedomTechFeed/packages, NOT from the official OpenWrt
# package feeds, so it cannot be named in PACKAGES= — the ImageBuilder would try
# to resolve it from downloads.openwrt.org and fail.  Instead the ADB `.apk` is
# placed at files/root/tollgate-wrt.apk and a uci-defaults script installs it
# offline (apk-tools 3 direct-file install) on first boot, then deletes itself.
#
# Profile: comfast_cf-wr632ax — the STOCK OpenWrt layout.  The device also has a
# `comfast_cf-wr632ax-ubootmod` profile; it is deliberately NOT used here (the
# stock layout does not require replacing the vendor bootloader, and the U-Boot
# layout carried a memory-speed stability issue in 25.12.0–25.12.4).
#
# OpenWrt >= 25.12.5 is a hard requirement for this device (memory-speed
# stability fix, upstream PRs #22929 / #23416).
#
# Runs entirely on the build host: no SSH, no secrets, no device access.
# Re-runnable and idempotent: downloads and the extracted build tree are reused
# if they are already present and hash-correct.  Budget ~2 GB of disk (the
# ImageBuilder tarball is ~513 MB, ~1.6 GB unpacked); keep WORK_DIR on a
# filesystem with room, not on a small tmpfs.
#
# Usage:
#   scripts/comfast-cf-wr632ax/build-image.sh
# Env overrides:
#   OPENWRT_RELEASE   (default 25.12.5)
#   TOLLGATE_RELEASE  (default v0.6.0-alpha4-pre19)
#   TOLLGATE_ASSET    (default tollgate-wrt_0.6.0_alpha4_pre19_aarch64_cortex-a53.apk)
#   TOLLGATE_REPO     (default FreedomTechFeed/packages)
#   WORK_DIR          (default $HOME/artifacts/comfast-cf-wr632ax/build)
#   OUT_DIR           (default $HOME/artifacts/comfast-cf-wr632ax)
set -euo pipefail

OPENWRT_RELEASE="${OPENWRT_RELEASE:-25.12.5}"
TOLLGATE_RELEASE="${TOLLGATE_RELEASE:-v0.6.0-alpha4-pre19}"
TOLLGATE_ASSET="${TOLLGATE_ASSET:-tollgate-wrt_0.6.0_alpha4_pre19_aarch64_cortex-a53.apk}"
TOLLGATE_REPO="${TOLLGATE_REPO:-FreedomTechFeed/packages}"

TARGET_PATH="mediatek/filogic"
PROFILE="comfast_cf-wr632ax"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK_DIR="${WORK_DIR:-$HOME/artifacts/comfast-cf-wr632ax/build}"
OUT_DIR="${OUT_DIR:-$HOME/artifacts/comfast-cf-wr632ax}"
FILES_SRC="$REPO_ROOT/scripts/comfast-cf-wr632ax/files"

IB_BASE="openwrt-imagebuilder-${OPENWRT_RELEASE}-mediatek-filogic.Linux-x86_64"
IB_URL="https://downloads.openwrt.org/releases/${OPENWRT_RELEASE}/targets/${TARGET_PATH}/${IB_BASE}.tar.zst"
SUMS_URL="https://downloads.openwrt.org/releases/${OPENWRT_RELEASE}/targets/${TARGET_PATH}/sha256sums"

TOLLGATE_VERSION_SLUG="$(printf '%s' "$TOLLGATE_RELEASE" | sed 's/^v//')"
OUT_NAME="openwrt-${OPENWRT_RELEASE}-mediatek-filogic-${PROFILE}-squashfs-sysupgrade-tollgate-${TOLLGATE_VERSION_SLUG}.bin"

# Packages that ARE in the official feeds and therefore belong in PACKAGES=.
# tollgate-wrt's own runtime deps are jq + libc + nodogsplash; the ImageBuilder
# resolves nodogsplash's dependency closure (libmicrohttpd-no-ssl, the
# iptables/xtables/kmod set) from the release feeds automatically.
PACKAGES="nodogsplash jq libmicrohttpd-no-ssl"

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# OpenWrt's checksum files use the binary-mode marker (`*name`), so the name
# column must be stripped of a leading '*' before matching.
sum_for() { awk -v f="$1" '{ n=$2; sub(/^\*/,"",n); if (n == f) { print $1; exit } }' "$2"; }

for bin in wget zstd tar sha256sum make awk; do
    command -v "$bin" >/dev/null 2>&1 || die "required tool not found: $bin"
done

mkdir -p "$WORK_DIR" "$OUT_DIR" "$WORK_DIR/dl"

# ─── 1. ImageBuilder: download and verify against the release sha256sums ─────
SUMS_FILE="$WORK_DIR/dl/sha256sums-${OPENWRT_RELEASE}.txt"
if [ ! -s "$SUMS_FILE" ]; then
    log "fetching release checksums: $SUMS_URL"
    wget -q -O "$SUMS_FILE" "$SUMS_URL"
fi
EXPECTED_IB_SHA="$(sum_for "${IB_BASE}.tar.zst" "$SUMS_FILE")"
[ -n "$EXPECTED_IB_SHA" ] || die "no sha256 entry for ${IB_BASE}.tar.zst in $SUMS_FILE"

IB_TAR="$WORK_DIR/dl/${IB_BASE}.tar.zst"
if [ ! -s "$IB_TAR" ] || ! echo "$EXPECTED_IB_SHA  $IB_TAR" | sha256sum -c - >/dev/null 2>&1; then
    log "downloading ImageBuilder (~513 MB, resumable): $IB_URL"
    wget -c -O "$IB_TAR" "$IB_URL"
fi
log "verifying ImageBuilder sha256 ($EXPECTED_IB_SHA)"
echo "$EXPECTED_IB_SHA  $IB_TAR" | sha256sum -c - || die "ImageBuilder sha256 mismatch"

IB_DIR="$WORK_DIR/$IB_BASE"
if [ ! -f "$IB_DIR/Makefile" ]; then
    log "extracting ImageBuilder"
    zstd -t "$IB_TAR"
    tar --zstd -xf "$IB_TAR" -C "$WORK_DIR"
fi
[ -f "$IB_DIR/Makefile" ] || die "ImageBuilder tree looks wrong: $IB_DIR"

# ─── 2. TollGate module apk: download and verify against the release sums ────
APK_DIR="$WORK_DIR/dl/tollgate"
mkdir -p "$APK_DIR"
if [ ! -s "$APK_DIR/$TOLLGATE_ASSET" ] || [ ! -s "$APK_DIR/SHA256SUMS" ]; then
    command -v gh >/dev/null 2>&1 || die "gh is needed to fetch the tollgate release asset"
    log "downloading $TOLLGATE_ASSET from $TOLLGATE_REPO@$TOLLGATE_RELEASE"
    ( cd "$APK_DIR" && gh release download "$TOLLGATE_RELEASE" --repo "$TOLLGATE_REPO" \
        --pattern "$TOLLGATE_ASSET" --pattern 'SHA256SUMS' --clobber )
fi
EXPECTED_APK_SHA="$(sum_for "$TOLLGATE_ASSET" "$APK_DIR/SHA256SUMS")"
[ -n "$EXPECTED_APK_SHA" ] || die "no sha256 entry for $TOLLGATE_ASSET in the release SHA256SUMS"
( cd "$APK_DIR" && echo "$EXPECTED_APK_SHA  $TOLLGATE_ASSET" | sha256sum -c - ) \
    || die "tollgate apk sha256 mismatch against the release SHA256SUMS"
log "tollgate apk verified: $EXPECTED_APK_SHA"

# ─── 3. Stage files/ (the repo's file skeleton + the verified apk) ───────────
log "staging files/ from $FILES_SRC"
rm -rf "$WORK_DIR/files"
mkdir -p "$WORK_DIR/files"
cp -a "$FILES_SRC/." "$WORK_DIR/files/"
mkdir -p "$WORK_DIR/files/root"
cp -a "$APK_DIR/$TOLLGATE_ASSET" "$WORK_DIR/files/root/tollgate-wrt.apk"
chmod 0755 "$WORK_DIR/files/etc/uci-defaults/99-tollgate-firstboot"

# ─── 4. Build ───────────────────────────────────────────────────────────────
log "building image: PROFILE=$PROFILE PACKAGES=$PACKAGES"
(
  cd "$IB_DIR"
  make image PROFILE="$PROFILE" PACKAGES="$PACKAGES" FILES="$WORK_DIR/files"
)

# ─── 5. Collect the artifact ────────────────────────────────────────────────
BIN_DIR="$IB_DIR/bin/targets/${TARGET_PATH}/${PROFILE}"
SRC_BIN="$BIN_DIR/openwrt-${OPENWRT_RELEASE}-${PROFILE}-squashfs-sysupgrade.bin"
# The ImageBuilder may lay the image down beside profiles.json instead of in a
# per-profile directory, so fall back to a search one level up.
[ -s "$SRC_BIN" ] || SRC_BIN="$(find "$BIN_DIR" "$IB_DIR/bin/targets/${TARGET_PATH}" -maxdepth 1 \
    -name "openwrt-${OPENWRT_RELEASE}-${PROFILE}-squashfs-sysupgrade.bin" 2>/dev/null | head -n1)"
[ -s "$SRC_BIN" ] || die "no sysupgrade image produced under $IB_DIR/bin/targets/$TARGET_PATH"

install -m 0644 "$SRC_BIN" "$OUT_DIR/$OUT_NAME"

BIN_ROOT="$(dirname "$SRC_BIN")"
MANIFEST_SRC="$BIN_ROOT/openwrt-${OPENWRT_RELEASE}-mediatek-filogic-${PROFILE}.manifest"
[ -s "$MANIFEST_SRC" ] || MANIFEST_SRC="$(find "$BIN_ROOT" "$IB_DIR/bin/targets/${TARGET_PATH}" -maxdepth 1 \
    -name "*${PROFILE}*.manifest" 2>/dev/null | head -n1)"
if [ -n "$MANIFEST_SRC" ] && [ -s "$MANIFEST_SRC" ]; then
    install -m 0644 "$MANIFEST_SRC" "$OUT_DIR/${OUT_NAME%.bin}.manifest"
fi

# ─── 6. Self-check: the expected packages must be in the manifest ───────────
if [ -n "${MANIFEST_SRC:-}" ] && [ -s "$MANIFEST_SRC" ]; then
    log "manifest package check"
    missing=0
    for p in nodogsplash jq libmicrohttpd-no-ssl base-files dropbear; do
        if grep -qE "^${p}([ -])" "$MANIFEST_SRC"; then
            printf '    present: %s\n' "$(grep -E "^${p}([ -])" "$MANIFEST_SRC" | head -n1)"
        else
            printf '    MISSING: %s\n' "$p"; missing=1
        fi
    done
    # tollgate-wrt is baked in via FILES on purpose, so it appears in NO
    # manifest — do not look for it here; check the rootfs payload instead.
    [ "$missing" = "0" ] || die "manifest is missing an expected package"
fi

log "artifact: $OUT_DIR/$OUT_NAME"
sha256sum "$OUT_DIR/$OUT_NAME"
stat -c 'size: %s bytes' "$OUT_DIR/$OUT_NAME"
log "done — hand this file to a tester with docs/comfast-cf-wr632ax-testing.md"
