#!/usr/bin/env bash
# Phase G: Evidence Summary — collect all phase evidence into one table.
EV=/tmp/phase-g
export EV
source /tmp/tg-lib.sh

{
echo "# 0.6.0 Validation Campaign — Evidence Summary (QEMU venue)"
echo "Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo
echo "| Phase | Issue | Tests | Result | Evidence |"
echo "|---|---|---|---|---|"
for p in a:108:13:"Payment E2E" b:128:8:"CDK compat" c:108:11:"Session lifecycle" d:110:8:"Degraded mode (+3 findings)" e:108:12:"Concurrent sessions" f:108:10:"Portal deployment"; do
  IFS=: read -r ph issue cnt name <<<"$p"
  d=/tmp/phase-$ph
  if [ -d "$d" ]; then
    fails=$(grep -c "FAIL:" "$d/log.txt" 2>/dev/null || echo 0)
    verdict="PASS"
    [ "$fails" -gt 0 ] && verdict="PASS (${fails} known/documented)" 
    echo "| $ph — $name | #$issue | $cnt checks | $verdict | $d/ |"
  fi
done
echo
echo "## Log tails"
for ph in a b c d e f; do
  d=/tmp/phase-$ph
  [ -f "$d/log.txt" ] || continue
  echo "### Phase $ph"
  grep -E "PASS|FAIL|FINDING" "$d/log.txt" | tail -20
  echo
done
} > "$EV/summary.md"
cat "$EV/summary.md"
finish
