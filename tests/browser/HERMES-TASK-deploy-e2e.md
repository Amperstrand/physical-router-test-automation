# Task: net4sats wizard alpha16 deploy E2E — deliverables 2, 3, 4

Prior work done (do NOT redo): wifi-scan-proof.spec.mjs PASSING, mp4 exists
at test-results/wifi-scan-output/wifi-scan-proof.mp4 (262 KB). The 502 root
cause was resolved by prior worker; do not re-debug SSH auth.

## Environment (parent-verified 2026-09-01 22:05 IST)

- Repo: /home/c03rad0r/physical-router-test-automation. Specs+configs in
  tests/browser/. Working dir for commands: tests/browser/.
- Laptop: enp0s31f6 = 192.168.1.200/24 (router br-lan). WiFi wlp58s0 =
  internet. sudo NOPASSWD. sshpass installed.
- Router: GL-MT3000 at 192.168.1.1, OpenWrt 24.10.4, root SSH password:
  password. Internet via laptop NAT (working).
- Wizard binary alpha16 at /tmp/net4sats-wizard (14.6 MB). Run
  `sha256sum /tmp/net4sats-wizard` and `/tmp/net4sats-wizard --version`
  (or run it with --help) to sanity-verify; expect version string
  v0.7.0-alpha16. LEAVE this binary in place when done.
- Route guard RUNNING (pid 60338): re-applies router default route + DNS
  during deploy-phase network restarts. READ /tmp/router-route-guard.sh to
  learn its stop mechanism (argv fifo file). LEAVE RUNNING during the
  deploy. STOP it in cleanup if the script design allows; if unclear,
  leave running and say so in the report.
- Router may have tollgate-wrt leftovers from earlier experiments polling
  gateway=192.168.2.254 — ignore unless the deploy status handler errors
  on it; then report the conflict.
- No wizard/Xvfb/playwright processes running now. Kill any stragglers
  first (pgrep -af, only kill wizard/e2e-related ones).

## CRITICAL lesson from the run you are replacing

The prior worker launched the deploy spec as a session-owned background
terminal process, then hit its 30-tool-call cap. Session teardown KILLED
its xvfb-run + playwright children at launch moment — no output dir was
ever created. FIX: launch DETACHED with
`nohup setsid bash -c '...' > /tmp/deploy-run.log 2>&1 &`
and poll that log file with short terminal calls. Detached processes
survive your session teardown. A single foreground call with
timeout=600 is also acceptable.

## Deliverables

### D2. wizard-deploy-e2e.spec.mjs PASSING + fresh mp4

1. Read wizard-deploy-e2e.spec.mjs and wizard-deploy-e2e.config.mjs FULLY
   first. Spec is git-modified uncommitted. Config forces headed
   (headless: false), slowMo 400, video+trace on, timeout 600000,
   outputDir under test-results (read exact path from config).
2. Spec env defaults are WRONG for this lab (192.168.28.1 /
   test123 / alpha8 release). Override at launch:
   ROUTER_IP=192.168.1.1, ROUTER_PASSWORD=*** (read exact env var names
   from spec header lines), WIZARD_PORT stays 8099.
3. Wizard binary source: the spec downloads its own binary via
   WIZARD_RELEASE_URL into tests/browser/net4sats-wizard. Get the exact
   v0.7.0-alpha16 asset URL from
   curl -s https://api.github.com/repos/felixfelix-bot/net4sats-wizard-go/releases
   (asset name net4sats-wizard, tag v0.7.0-alpha16). If download step is
   unreliable: cp -f /tmp/net4sats-wizard to the path the spec expects
   and chmod +x (read spec spawn logic for exact path).
4. Launch detached (see lesson above): env vars + `xvfb-run -a npx
   playwright test --config=wizard-deploy-e2e.config.mjs` redirect to
   /tmp/deploy-run.log. Poll the log. Spec must: spawn wizard on 8099,
   drive UI, call deploy API, poll status, assert splash page at
   http://192.168.1.1:2051 with Lightning invoice.
5. On PASS: ffmpeg convert each webm to mp4 (libx264, -an is fine — no
   audio track), single mp4 per test, stored at the deploy-e2e outputDir
   root. Report exact path + byte size.

### D3. Backend verify FROM the router itself

While the deployed system is still alive: sshpass -p password ssh
-o StrictHostKeyChecking=no root@192.168.1.1 'wget -qO- http://127.0.0.1:2121/...'
using the correct backend path per the spec source / wizard README.
Report the actual command and first ~5 lines of real response.

### D4. Cleanup + report

- Kill wizard/Xvfw/node/chromium processes. Stop route-guard per its
  script design.
- LEAVE /tmp/net4sats-wizard binary. Do NOT delete any test logs,
  videos, artifacts.
- Report: spec pass/fail per test, exact mp4 AND webm paths with byte
  sizes, key evidence lines (invoice seen, :2121 response), and any
  conflicts. Retry any single failing step once max. Do not claim
  success without real execution output.
