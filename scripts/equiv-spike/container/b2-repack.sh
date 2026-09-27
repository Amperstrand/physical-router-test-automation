#!/usr/bin/env bash
# Runs INSIDE the SDK container as root (volume: $WORKDIR at /work).
# Repacks the lane-A SDK-free payload as .apk with static apk-tools,
# mirroring the oracle's staging rules. Writes /work/$REL/laneB2.apk.
#
# Staging rules established during the equivalence spike (2026-09-17):
#   - tar extraction with --numeric-owner under uid 0 (dir ownership is
#     embedded by apk mkpkg; fakeroot or root, never an unprivileged user)
#   - payload root mode 755
#   - lib/apk/packages bookkeeping dirs (buildroot auto-creates them)
#   - every mtime touched to SOURCE_DATE_EPOCH; mkpkg itself run with
#     SOURCE_DATE_EPOCH=0
#   - usr/share/doc removed to mirror the oracle's staging — allow-listed
#     delta pending the team decision in Amperstrand/tollgate-module-basic-go#92
# Env: REL SDE APK_VERSION ARCH
set -euo pipefail
OUT=/work/$REL
APK=/work/laneB2/apk.static
[ -x "$APK" ] || { echo "static apk missing at $APK (fetch apk-tools-static)" >&2; exit 2; }

rm -rf "$OUT/payload" "$OUT/scripts" && mkdir -p "$OUT/payload" "$OUT/scripts"
tar xzf "$OUT/laneA.data.tar.gz" -C "$OUT/payload" --numeric-owner
rm -rf "$OUT/payload/usr/share/doc"
mkdir -p "$OUT/payload/lib/apk/packages"
chmod 755 "$OUT/payload"
find "$OUT/payload" -exec touch -d "@$SDE" {} +

python3 - "$OUT/laneB1.dump.json" "$OUT/scripts" <<'PY'
import json, sys
dump, scripts_dir = sys.argv[1], sys.argv[2]
d = json.load(open(dump))
for key in ("pre-install", "post-install", "pre-upgrade", "post-upgrade", "pre-deinstall"):
    body = (d.get("scripts") or {}).get(key)
    if body is not None:
        open(f"{scripts_dir}/{key}", "w").write(body)
PY

SOURCE_DATE_EPOCH=0 "$APK" mkpkg \
  --info name:tollgate-wrt \
  --info "version:$APK_VERSION" \
  --info description:TollGate\ Basic\ Module\ for\ OpenWrt \
  --info "arch:$ARCH" \
  --info license:GPL-3.0-only \
  --info origin:feeds/base/tollgate-wrt \
  --info maintainer:TollGate\ \<tollgate@tollgate.me\> \
  --info provides:nodogsplash-files \
  --info depends:libc \
  --script pre-install:"$OUT/scripts/pre-install" \
  --script post-install:"$OUT/scripts/post-install" \
  --script pre-upgrade:"$OUT/scripts/pre-upgrade" \
  --script post-upgrade:"$OUT/scripts/post-upgrade" \
  --script pre-deinstall:"$OUT/scripts/pre-deinstall" \
  --files "$OUT/payload" \
  --output "$OUT/laneB2.apk"
sha256sum "$OUT/laneB2.apk"
