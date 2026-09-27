#!/bin/bash
# serial.sh — send a command to the upgrade-test VM serial console, capture output.
# The serial console is the only access path before network provisioning.
CMD="$1"; W="${2:-3}"
(sleep 1; echo "$CMD"; sleep "$W") | timeout $((W+6)) sudo socat - UNIX-CONNECT:/home/ubuntu/upgrade-test/run/serial.sock 2>/dev/null | tail -8
