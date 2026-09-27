#!/usr/bin/env bash
# soak watchdog: if soak.log goes stale >20min, kill hung ssh/curl children so
# the cycle errors out and the loop continues. Alert-only for process death.
LOG=/tmp/soak/soak.log
while true; do
  sleep 300
  SPID=$(pgrep -f "soak-24[h]" | head -1)
  [ -z "$SPID" ] && { echo "[$(date -u +%FT%TZ)] ALERT: soak process gone" >> /tmp/soak/watchdog.log; continue; }
  AGE=$(( $(date +%s) - $(stat -c %Y "$LOG" 2>/dev/null || date +%s) ))
  if [ "$AGE" -gt 1200 ]; then
    echo "[$(date -u +%FT%TZ)] WARN: soak.log stale ${AGE}s" >> /tmp/soak/watchdog.log
    for KID in $(ps --ppid "$SPID" -o pid= 2>/dev/null) "$SPID"; do
      ps --ppid "$KID" -o pid=,etimes=,cmd= 2>/dev/null | while read -r gpid et cmdrest; do
        case "$cmdrest" in *ssh*|*curl*)
          [ "${et:-0}" -gt 300 ] && kill "$gpid" 2>/dev/null && echo "[$(date -u +%FT%TZ)] KILLED hung child $gpid ($cmdrest)" >> /tmp/soak/watchdog.log
        ;; esac
      done
    done
  fi
done
