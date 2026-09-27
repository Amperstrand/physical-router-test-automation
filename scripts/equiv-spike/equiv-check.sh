#!/usr/bin/env bash
# equiv-check.sh — cross-lane package equivalence check (SDK oracle vs SDK-free).
#
# The SDK lane is the ORACLE (OpenWrt-way reference build); the SDK-free lane
# is the production path. This gate proves, for one commit + arch:
#   A. the SDK-free lane is deterministic (upstream `scripts/repro-test.sh`)
#   B. the SDK oracle produces its apk, packaging-only, from a pinned digest
#   C. a static-apk repack of the SDK-free payload is logically identical
#      to the oracle (canonical adbdump equality)
# and reports byte-level equality (see compare.py for why bytes additionally
# depend on payload tree insertion order).
#
# Prereq: setup.sh has run (checkout + pinned go/node in ~/.cache/tollgate-tools);
#         docker; static apk-tools fetched once to $WORKDIR/laneB2/apk.static.
# Usage: bash equiv-check.sh [--arch aarch64_cortex-a53] [--version vX.Y.Z]
#                            [--sde EPOCH] [--workdir ~/tg-equiv] [--strict-bytes]
# Exit: 0 pass, 1 regression, 2 environment error.
set -euo pipefail

ARCH=aarch64_cortex-a53
VERSION=''
SDE=''
WORKDIR=$HOME/tg-equiv
STRICT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --arch) ARCH=$2; shift 2 ;;
    --arch=*) ARCH=${1#*=}; shift ;;
    --version) VERSION=$2; shift 2 ;;
    --version=*) VERSION=${1#*=}; shift ;;
    --sde) SDE=$2; shift 2 ;;
    --sde=*) SDE=${1#*=}; shift ;;
    --workdir) WORKDIR=$2; shift 2 ;;
    --workdir=*) WORKDIR=${1#*=}; shift ;;
    --strict-bytes) STRICT=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

TOLLGATE=$WORKDIR/tollgate
HERE=$(cd "$(dirname "$0")" && pwd)
[ -d "$TOLLGATE" ] || { echo "no checkout at $TOLLGATE — run setup.sh first" >&2; exit 2; }
VERSION=${VERSION:-$(cat "$TOLLGATE/VERSION")}
SDE=${SDE:-$(git -C "$TOLLGATE" log -1 --format=%ct)}

case "$ARCH" in
  aarch64_cortex-a53) GOARCH_BIN=arm64;     SDK_TARGET=mediatek-filogic ;;
  aarch64_cortex-a72) GOARCH_BIN=arm64;     SDK_TARGET=bcm27xx-bcm2711 ;;
  arm_cortex-a7)      GOARCH_BIN=armv7;     SDK_TARGET=bcm27xx-bcm2709 ;;
  mips_24kc)          GOARCH_BIN=mips-sf;   SDK_TARGET=ath79-generic ;;
  mipsel_24kc)        GOARCH_BIN=mipsle-sf; SDK_TARGET=ramips-mt7621 ;;
  x86_64)             GOARCH_BIN=amd64;     SDK_TARGET=x86-64 ;;
  *) echo "unsupported arch: $ARCH" >&2; exit 2 ;;
esac

SDK_DIGEST=$(python3 -c "
import json
bi = json.load(open('$TOLLGATE/packaging/build-inputs.json'))
print(bi['openwrt_sdk']['targets']['$SDK_TARGET']['digest'])")
SDK_REF="openwrt/sdk@$SDK_DIGEST"
APK_VERSION=$(cd "$TOLLGATE" && bash packaging/normalize-apk-version.sh "$VERSION")
RUN=$WORKDIR/equiv-run-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$RUN"
echo "== equiv-check: $ARCH @ $VERSION (SDE=$SDE, apk version $APK_VERSION)"
echo "   oracle $SDK_REF | run dir $RUN"

TOOLPATH=$HOME/.cache/tollgate-tools/go/bin:$HOME/.cache/tollgate-tools/node/bin:$PATH
RC=0

echo "-- lane A: SDK-free determinism (upstream repro-test)"
if (cd "$TOLLGATE" && GOMAXPROCS=4 PATH="$TOOLPATH" \
     bash scripts/repro-test.sh ipk "$ARCH" > "$RUN/laneA-repro.log" 2>&1); then
  echo "   A: reproducible (clean roots byte-identical)"
else
  echo "   A: FAIL — see $RUN/laneA-repro.log"; RC=1
fi

echo "-- lane A artifact (local-build-ipk)"
(cd "$TOLLGATE" && GOMAXPROCS=4 PATH="$TOOLPATH" \
   ARCH="$ARCH" bash packaging/local-build-ipk.sh > "$RUN/laneA-build.log" 2>&1)
IPK=$(find "$TOLLGATE/packaging" -maxdepth 1 -name "tollgate-wrt_*_${ARCH}.ipk" -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
[ -n "$IPK" ] || { echo "env error: lane-A ipk missing" >&2; exit 2; }
mv -f "$IPK" "$RUN/laneA.ipk"
tar xOzf "$RUN/laneA.ipk" ./data.tar.gz > "$RUN/laneA.data.tar.gz"

echo "-- lane B1: SDK oracle build"
if docker run --rm --memory=3g -v "$WORKDIR:/work" -v "$HERE/container:/scripts:ro" \
     -e REL="$(basename "$RUN")" -e ARCH="$ARCH" -e GOARCH_BIN="$GOARCH_BIN" \
     -e SDE="$SDE" -e VERSION="$VERSION" \
     "$SDK_REF" bash /scripts/b1-oracle.sh > "$RUN/laneB1.log" 2>&1; then
  echo "   B1: $(sha256sum "$RUN/laneB1.apk" | cut -c1-16)…"
else
  echo "   B1: FAIL — see $RUN/laneB1.log"; RC=1
fi

echo "-- lane B2: static repack"
if docker run --rm --memory=1g -u 0 -v "$WORKDIR:/work" -v "$HERE/container:/scripts:ro" \
     -e REL="$(basename "$RUN")" -e ARCH="$ARCH" -e SDE="$SDE" -e APK_VERSION="$APK_VERSION" \
     "$SDK_REF" bash /scripts/b2-repack.sh > "$RUN/laneB2.log" 2>&1; then
  echo "   B2: $(sha256sum "$RUN/laneB2.apk" | cut -c1-16)…"
else
  echo "   B2: FAIL — see $RUN/laneB2.log"; RC=1
fi

echo "-- compare"
STRICT_ARG=''
[ $STRICT = 1 ] && STRICT_ARG=--strict-bytes
if python3 "$HERE/compare.py" "$RUN" $STRICT_ARG | tee "$RUN/compare.txt"; then :; else RC=1; fi
echo "allow-listed staging delta: usr/share/doc/tollgate-wrt/LICENSE (Amperstrand fork issue #92)"
echo "RESULT: $([ $RC = 0 ] && echo PASS || echo FAIL) — $RUN"
exit $RC
