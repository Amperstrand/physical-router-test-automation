#!/usr/bin/env bash
# build-image.sh — build a flashable OpenWrt sysupgrade image for the COMFAST
# CF-WR632AX with the TollGate module baked in.
#
# ─────────────────────────────────────────────────────────────────────────────
# WHY THIS BUILDS ON A REMOTE HOST
# The OpenWrt ImageBuilder tarball is ~513 MB and expands to ~1.6 GB; the build
# itself needs several GB of scratch.  On a workstation whose /tmp is a small
# tmpfs this fails with "Disk quota exceeded".  This script therefore does ALL
# of the heavy lifting on a build host reached over SSH (default: vps3) and only
# pulls the finished ~20-30 MB image back.  Nothing large is ever written under
# the caller's /tmp.
# ─────────────────────────────────────────────────────────────────────────────
#
# WHY ImageBuilder FILES= + a first-boot install (and not PACKAGES= alone)
# tollgate-wrt is published from FreedomTechFeed/packages, NOT from the official
# OpenWrt package feeds, so it cannot be named in PACKAGES= — the ImageBuilder
# would try to resolve it from downloads.openwrt.org and fail.  Instead the ADB
# `.apk` is placed at files/root/tollgate-wrt.apk and a uci-defaults script
# installs it offline (apk-tools 3 direct-file install) on first boot, then
# deletes itself so the install runs exactly once.
#
# PROFILE
# `comfast_cf-wr632ax` is the STOCK OpenWrt layout and the safe default for a
# tester.  The device also has a `comfast_cf-wr632ax-ubootmod` profile; it is
# NOT used here — it requires replacing the vendor bootloader and the U-Boot
# layout carried a memory-speed stability issue in 25.12.0–25.12.4 (fixed in
# 25.12.5).  OpenWrt >= 25.12.5 is a hard requirement for this device.
#
# BUILD HOST PREREQUISITES (the ImageBuilder's own prereq check needs these and
# its failure message names only the missing tool, not the fix):
#   make gcc g++ wget curl tar zstd sha256sum file  — and GNU awk SPECIFICALLY.
#   On Debian, /usr/bin/awk is mawk by default and the build dies with
#       Checking 'awk'... failed.  Build dependency: Please install GNU 'awk'
#   Fix on the build host:  sudo apt-get install -y gawk
#   remote-build.sh checks this up front and names the fix.
#
# Re-runnable and idempotent: downloads and the assembled tree are reused when
# already present and hash-correct.  No secrets, no device access, no flashing.
#
# Usage:
#   scripts/comfast-cf-wr632ax/build-image.sh
#
# Env overrides:
#   BUILD_HOST        ssh target of the build host  (default debian@23.182.128.219)
#   SSH               ssh binary to use             (default: ssh)
#   SCP               scp binary to use             (default: scp)
#   REMOTE_DIR        scratch dir on the build host (default tg-build, under $HOME)
#   OPENWRT_RELEASE   (default 25.12.5)
#   TOLLGATE_RELEASE  (default v0.6.0-alpha4-pre19)
#   TOLLGATE_ASSET    (default tollgate-wrt_0.6.0_alpha4_pre19_aarch64_cortex-a53.apk)
#   TOLLGATE_REPO     (default FreedomTechFeed/packages)
#   WORK_DIR          local staging dir (default $HOME/artifacts/comfast-cf-wr632ax/build)
#   OUT_DIR           local artifact dir (default $HOME/artifacts/comfast-cf-wr632ax)
set -euo pipefail

OPENWRT_RELEASE="${OPENWRT_RELEASE:-25.12.5}"
TOLLGATE_RELEASE="${TOLLGATE_RELEASE:-v0.6.0-alpha4-pre19}"
TOLLGATE_ASSET="${TOLLGATE_ASSET:-tollgate-wrt_0.6.0_alpha4_pre19_aarch64_cortex-a53.apk}"
TOLLGATE_REPO="${TOLLGATE_REPO:-FreedomTechFeed/packages}"

BUILD_HOST="${BUILD_HOST:-debian@23.182.128.219}"
SSH="${SSH:-ssh}"
SCP="${SCP:-scp}"
REMOTE_DIR="${REMOTE_DIR:-tg-build}"

TARGET_PATH="mediatek/filogic"
PROFILE="comfast_cf-wr632ax"
ARCH="aarch64_cortex-a53"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK_DIR="${WORK_DIR:-$HOME/artifacts/comfast-cf-wr632ax/build}"
OUT_DIR="${OUT_DIR:-$HOME/artifacts/comfast-cf-wr632ax}"
FILES_SRC="$REPO_ROOT/scripts/comfast-cf-wr632ax/files"
REMOTE_SCRIPT_SRC="$REPO_ROOT/scripts/comfast-cf-wr632ax/remote-build.sh"

TG_VERSION_SLUG="$(printf '%s' "$TOLLGATE_RELEASE" | sed 's/^v//')"
OUT_NAME="openwrt-${OPENWRT_RELEASE}-mediatek-filogic-${PROFILE}-squashfs-sysupgrade-tollgate-${TG_VERSION_SLUG}.bin"

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

for bin in curl tar sha256sum; do
    command -v "$bin" >/dev/null 2>&1 || die "required tool not found: $bin"
done
[ -f "$REMOTE_SCRIPT_SRC" ] || die "missing remote-build.sh next to this script"

ssh_run() { "$SSH" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new "$BUILD_HOST" "$@"; }

mkdir -p "$WORK_DIR" "$OUT_DIR" "$WORK_DIR/dl"

# ─── 0. Pre-flight: the build host must be reachable and have room ───────────
log "build host: $BUILD_HOST"
ssh_run 'true' || die "cannot reach the build host $BUILD_HOST over non-interactive ssh"
log "build host ok: $(ssh_run 'hostname; df -h / | awk "NR==2{print \$4\" free on \"\$6}"; nproc' | tr '\n' ' ')"

# ─── 1. Establish the expected module apk sha256 LOCALLY (this is the pin) ───
TG_SUMS_URL="https://github.com/${TOLLGATE_REPO}/releases/download/${TOLLGATE_RELEASE}/SHA256SUMS"
TG_SUMS_LOCAL="$WORK_DIR/dl/tollgate-SHA256SUMS"
if [ ! -s "$TG_SUMS_LOCAL" ]; then
    log "fetching tollgate release SHA256SUMS: $TG_SUMS_URL"
    curl -fsSL -o "$TG_SUMS_LOCAL.part" "$TG_SUMS_URL" || die "could not fetch the tollgate SHA256SUMS asset"
    mv "$TG_SUMS_LOCAL.part" "$TG_SUMS_LOCAL"
fi
EXPECTED_APK_SHA="$(awk -v f="$TOLLGATE_ASSET" '{n=$2; sub(/^\*/,"",n); if (n==f) print $1}' "$TG_SUMS_LOCAL" | head -n1)"
[ -n "$EXPECTED_APK_SHA" ] || die "no sha256 entry for $TOLLGATE_ASSET in the release SHA256SUMS"
log "pinned tollgate apk sha256: $EXPECTED_APK_SHA"

# ─── 2. Establish the expected ImageBuilder sha256 LOCALLY (the pin) ─────────
IB_BASE="openwrt-imagebuilder-${OPENWRT_RELEASE}-mediatek-filogic.Linux-x86_64"
SUMS_URL="https://downloads.openwrt.org/releases/${OPENWRT_RELEASE}/targets/${TARGET_PATH}/sha256sums"
SUMS_LOCAL="$WORK_DIR/dl/sha256sums-${OPENWRT_RELEASE}.txt"
if [ ! -s "$SUMS_LOCAL" ]; then
    log "fetching OpenWrt release checksums: $SUMS_URL"
    curl -fsSL -o "$SUMS_LOCAL.part" "$SUMS_URL" || die "could not fetch the OpenWrt release sha256sums"
    mv "$SUMS_LOCAL.part" "$SUMS_LOCAL"
fi
EXPECTED_IB_SHA="$(awk -v f="$IB_BASE.tar.zst" '{n=$2; sub(/^\*/,"",n); if (n==f) print $1}' "$SUMS_LOCAL" | head -n1)"
[ -n "$EXPECTED_IB_SHA" ] || die "no sha256 entry for ${IB_BASE}.tar.zst in the release sha256sums"
log "pinned ImageBuilder sha256: $EXPECTED_IB_SHA"

# ─── 3. Stage files/ into a tarball (repo skeleton, modes preserved) ─────────
log "staging files/ from $FILES_SRC"
STAGE_DIR="$WORK_DIR/stage"
rm -rf "$STAGE_DIR"; mkdir -p "$STAGE_DIR"
cp -a "$FILES_SRC" "$STAGE_DIR/files"
chmod 0755 "$STAGE_DIR/files/etc/uci-defaults/99-tollgate-firstboot"
FILES_TARBALL="$WORK_DIR/dl/comfast-files.tar"
tar -cf "$FILES_TARBALL" -C "$STAGE_DIR" files
log "staged tarball: $FILES_TARBALL ($(wc -c < "$FILES_TARBALL") bytes)"
log "  files/ contents:"
tar -tf "$FILES_TARBALL" | sed 's/^/    /'

# ─── 4. Ship the remote script + staged files to the build host ──────────────
REMOTE="$(ssh_run 'printf %s "$HOME"')/$REMOTE_DIR"
log "remote scratch: $REMOTE"
ssh_run "mkdir -p '$REMOTE/dl/tollgate'"
"$SCP" -q -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
    "$REMOTE_SCRIPT_SRC" "$BUILD_HOST:$REMOTE/remote-build.sh" || die "scp of remote-build.sh failed"
"$SCP" -q -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
    "$FILES_TARBALL" "$BUILD_HOST:$REMOTE/comfast-files.tar" || die "scp of the staged files failed"

# ─── 5. Build on the remote host ────────────────────────────────────────────
log "starting remote build (first run downloads ~513 MB; several minutes)"
BUILD_OUT="$(ssh_run "cd '$REMOTE' && TOLLGATE_REPO='$TOLLGATE_REPO' \
    sh ./remote-build.sh '$OPENWRT_RELEASE' '$TOLLGATE_RELEASE' '$TOLLGATE_ASSET' '$EXPECTED_APK_SHA' \
    '$REMOTE/comfast-files.tar' '$REMOTE'")" || die "remote build failed (see the transcript above)"
printf '%s\n' "$BUILD_OUT"

ARTIFACT_PATH="$(printf '%s\n' "$BUILD_OUT" | sed -n 's/^ARTIFACT_PATH=//p' | tail -n1)"
ARTIFACT_SHA="$(printf '%s\n' "$BUILD_OUT"  | sed -n 's/^ARTIFACT_SHA256=//p' | tail -n1)"
ARTIFACT_SIZE="$(printf '%s\n' "$BUILD_OUT" | sed -n 's/^ARTIFACT_SIZE=//p' | tail -n1)"
MANIFEST_PATH="$(printf '%s\n' "$BUILD_OUT" | sed -n 's/^MANIFEST_PATH=//p' | tail -n1)"

[ -n "$ARTIFACT_PATH" ] || die "remote build did not report an artifact path (see output above)"
[ -n "$ARTIFACT_SHA" ]  || die "remote build did not report an artifact sha256"

# ─── 6. Pull the artifact back and verify its sha256 locally ────────────────
log "fetching artifact back to $OUT_DIR"
"$SCP" -q -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
    "$BUILD_HOST:$ARTIFACT_PATH" "$OUT_DIR/$OUT_NAME" || die "scp of the built image failed"
if [ -n "$MANIFEST_PATH" ]; then
    "$SCP" -q -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
        "$BUILD_HOST:$MANIFEST_PATH" "$OUT_DIR/${OUT_NAME%.bin}.manifest" || true
fi

LOCAL_SHA="$(sha256sum "$OUT_DIR/$OUT_NAME" | awk '{print $1}')"
[ "$LOCAL_SHA" = "$ARTIFACT_SHA" ] || die "local copy sha256 != remote sha256 ($LOCAL_SHA != $ARTIFACT_SHA)"
log "local sha256 matches the remote build"

# ─── 7. Report ──────────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════════════════════════════════"
echo " image  : $OUT_DIR/$OUT_NAME"
echo " sha256 : $LOCAL_SHA"
echo " size   : $ARTIFACT_SIZE bytes"
echo " openwrt: $OPENWRT_RELEASE / $TARGET_PATH ($ARCH)"
echo " profile: $PROFILE (STOCK layout)"
echo " module : tollgate-wrt $TG_VERSION_SLUG (apk sha256 $EXPECTED_APK_SHA)"
echo "════════════════════════════════════════════════════════════════════════"
[ -f "$OUT_DIR/${OUT_NAME%.bin}.manifest" ] && log "package manifest: $OUT_DIR/${OUT_NAME%.bin}.manifest"
log "done. Hand this file to a tester with docs/comfast-cf-wr632ax-testing.md"
log "NOT YET BOOTED ON HARDWARE — built and hash-verified only."
