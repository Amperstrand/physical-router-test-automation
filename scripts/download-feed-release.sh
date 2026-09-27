#!/usr/bin/env bash
# Download a FreedomTechFeed/packages feed release asset (.ipk/.apk) for a given
# OpenWrt arch, verify it, and print the local absolute path to stdout.
#
# All diagnostics go to stderr so the stdout path can be used in $(...).
#
# Usage:
#   scripts/download-feed-release.sh <release-tag> <arch> <ipk|apk> [outdir]
#
# Examples:
#   scripts/download-feed-release.sh v0.6.0-alpha2-pre aarch64_cortex-a53 ipk
#   scripts/download-feed-release.sh v0.6.0-alpha2-pre aarch64_cortex-a53 apk
#
# Env:
#   FEED_REPO   default FreedomTechFeed/packages
#
# The asset name is derived from the tag exactly as the installer does it:
#   v0.6.0-alpha2-pre -> 0.6.0_alpha2_pre  (drop leading 'v', '-' -> '_')
#   tollgate-wrt_0.6.0_alpha2_pre_<arch>.<ext>
set -euo pipefail

TAG="${1:?usage: $0 <release-tag> <arch> <ipk|apk> [outdir]}"
ARCH="${2:?usage: $0 <release-tag> <arch> <ipk|apk> [outdir]}"
FMT="${3:?usage: $0 <release-tag> <arch> <ipk|apk> [outdir]}"
OUTDIR="${4:-${TMPDIR:-/tmp}/feed-rc}"
REPO="${FEED_REPO:-FreedomTechFeed/packages}"

case "$FMT" in
  ipk|apk) ;;
  *) echo "[download-feed-release] ERROR: format must be ipk or apk (got '$FMT')" >&2; exit 2 ;;
esac

# tag -> PKG_VERSION spelling (apk-legal, underscore)
VER="${TAG#v}"
VER="${VER//-/_}"
ASSET="tollgate-wrt_${VER}_${ARCH}.${FMT}"

log() { echo "[download-feed-release] $*" >&2; }

log "repo=$REPO tag=$TAG arch=$ARCH fmt=$FMT"
log "asset=$ASSET"

mkdir -p "$OUTDIR"

# Expected digest (sha256:...) published by GitHub for the release asset, if any.
EXPECT="$(gh api "repos/$REPO/releases/tags/$TAG" \
  --jq ".assets[] | select(.name==\"$ASSET\") | .digest" 2>/dev/null || true)"
if [ -z "${EXPECT:-}" ] || [ "$EXPECT" = "null" ]; then
  log "NOTE: release publishes no digest for $ASSET — verifying transfer integrity only"
fi

# Refuse to hand back a stale file if the download fails.
rm -f "$OUTDIR/$ASSET"
if ! gh release download "$TAG" -R "$REPO" -p "$ASSET" -D "$OUTDIR" --clobber; then
  log "ERROR: could not download $ASSET from $REPO@$TAG"
  exit 1
fi

OUT="$OUTDIR/$ASSET"
[ -s "$OUT" ] || { log "ERROR: downloaded file is empty: $OUT"; exit 1; }

GOT="sha256:$(sha256sum "$OUT" | awk '{print $1}')"
log "sha256=$GOT"
if [ -n "${EXPECT:-}" ] && [ "$EXPECT" != "null" ]; then
  if [ "$EXPECT" != "$GOT" ]; then
    log "ERROR: digest mismatch for $ASSET"
    log "  expected: $EXPECT"
    log "  got:      $GOT"
    exit 1
  fi
  log "digest verified against release asset"
fi

log "ok: $OUT ($(du -h "$OUT" | cut -f1))"
echo "$OUT"
