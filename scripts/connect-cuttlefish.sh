#!/bin/bash
# connect-cuttlefish.sh — bring the ai-legion Cuttlefish phone to local adb.
#
# Tunnels the remote adb server (port 5037) to a local port, then connects.
# After running this, `pytest --client adb` works with the Cuttlefish phone.
#
# Usage: bash scripts/connect-cuttlefish.sh [SSH_HOST]
set -euo pipefail

HOST="${1:-ai-legion}"
LOCAL_ADB_PORT=5038

echo "── Setting up adb server tunnel: localhost:$LOCAL_ADB_PORT → $HOST:5037"

pkill -f "ssh.*-L $LOCAL_ADB_PORT" 2>/dev/null || true
sleep 0.5

ssh -f -N -o ControlPath=none -o ExitOnForwardFailure=yes -o BatchMode=yes \
    "$HOST" -L "$LOCAL_ADB_PORT:127.0.0.1:5037"

sleep 1

# Verify the phone is visible through the tunnel
DEVICES=$(ANDROID_ADB_SERVER_PORT=$LOCAL_ADB_PORT adb devices 2>/dev/null | grep "device$" | head -1)
if [ -n "$DEVICES" ]; then
    SERIAL=$(echo "$DEVICES" | cut -f1)
    echo "✅ Cuttlefish phone visible: $SERIAL"
    echo ""
    echo "Run tests with:"
    echo "  export ANDROID_ADB_SERVER_PORT=$LOCAL_ADB_PORT"
    echo "  export PHONE_SERIAL=\"$SERIAL\""
    echo "  pytest tests/phone/ --client adb"
    echo ""
    echo "For film recording:"
    echo "  export ANDROID_ADB_SERVER_PORT=$LOCAL_ADB_PORT"
    echo "  export PHONE_SERIAL=\"$SERIAL\""
    echo "  pytest tests/phone/ --client adb --film"
    echo ""
    echo "# Paste these into your shell:"
    echo "export ANDROID_ADB_SERVER_PORT=$LOCAL_ADB_PORT"
    echo "export PHONE_SERIAL=\"$SERIAL\""
else
    echo "❌ No devices visible through tunnel"
    echo "  Check: ssh $HOST 'adb devices'"
    exit 1
fi
