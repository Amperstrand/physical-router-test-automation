#!/usr/bin/env bash
# Runs INSIDE the pinned OpenWrt SDK container (volume: $WORKDIR at /work).
# Builds the oracle .apk the way CI's package-apk job does: prebuilt binaries
# staged into packaging/, packaged by buildroot. Writes:
#   /work/$REL/laneB1.apk      oracle artifact
#   /work/$REL/laneB1.dump.json adbdump
# Env: REL ARCH GOARCH_BIN SDE VERSION  (GOARCH_BIN = local-build compile key)
set -euo pipefail
OUT=/work/$REL
STAGE=/builder/package/tollgate-wrt

rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -r /work/tollgate/packaging/. "$STAGE/"
cp /work/tollgate/LICENSE "$STAGE/LICENSE"
cp "/work/tollgate/bin/$GOARCH_BIN/tollgate-wrt" "$STAGE/tollgate-wrt"
cp "/work/tollgate/bin/$GOARCH_BIN/tollgate" "$STAGE/tollgate"
find /builder/package -exec touch -d "@$SDE" {} +

cd /builder
make defconfig >/dev/null
echo "CONFIG_PACKAGE_tollgate-wrt=y" >> .config
make -j2 package/tollgate-wrt/compile >/dev/null

PKG=$(find bin/packages -name "tollgate-wrt*.apk" | head -1)
[ -n "$PKG" ] || { echo "NO APK PRODUCED" >&2; exit 3; }
mkdir -p "$OUT"
cp "$PKG" "$OUT/laneB1.apk"
staging_dir/host/bin/apk adbdump --format json "$PKG" > "$OUT/laneB1.dump.json"
sha256sum "$OUT/laneB1.apk"
