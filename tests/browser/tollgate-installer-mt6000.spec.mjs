/**
 * tollgate-installer-mt6000.spec.mjs
 *
 * End-to-end proof that the TollGate installer wizard makes a GL-MT6000 — whose
 * WAN (eth1) is wired to a CobradorWave LAN — actually usable, and that it
 * self-heals the two real field failures found on this hardware:
 *
 *   1. DNS corruption. An older installer build read `network.lan.ipaddr`
 *      verbatim. On current OpenWrt that value is "192.168.1.1/24", so the
 *      wizard wrote `address=/tollgate.lan/192.168.1.1/24` into dnsmasq and
 *      `192.168.1.1/24 tollgate.lan` into /etc/hosts. dnsmasq rejects the bad
 *      address ("Bad address in --address") and CRASH-LOOPS: the router can
 *      ping 1.1.1.1 but cannot resolve any name, so the payment backend never
 *      binds :2121 and the deploy dies at the health check.
 *
 *   2. Subnet collision. The tollgate-wrt package derives its private network
 *      (br-private) from the LAN subnet (third octet +/-1). If that lands
 *      inside the upstream subnet (here upstream 192.168.2.0/24 and the router's
 *      own private net 192.168.2.1/24), the router answers for the upstream
 *      gateway's IP and name resolution breaks. The installer must relocate the
 *      colliding local subnet (and move its DHCP pool) automatically.
 *
 * The test SEEDS the broken state (corrupt dnsmasq entries + a colliding
 * private subnet) and then drives the wizard UI exactly as an operator would,
 * asserting the deploy succeeds and that, afterwards, dnsmasq is healthy and no
 * local subnet overlaps the upstream.
 *
 * Run it on the machine that can reach the router's LAN (CobradorWave), which
 * also has the installer binary and Playwright:
 *
 *   cd ~/physical-router-test-automation
 *   ROUTER_PASSWORD=password INSTALLER_BIN=~/tollgate-installer-new \
 *     npx playwright test --config tests/browser/tollgate-installer-mt6000.config.mjs
 *
 * Environment:
 *   INSTALLER_BIN    installer binary          (default ~/tollgate-installer-new)
 *   INSTALLER_PORT   wizard port               (default 18099)
 *   ROUTER_IP        router LAN address        (default 192.168.1.1)
 *   ROUTER_PASSWORD  router root password      (REQUIRED — test skips when unset)
 *   TOLLGATE_LNURL   owner Lightning address   (default test@example.com)
 *   SKIP_SEED        set 1 to skip corrupting the router before the run
 *   DEPLOY_MODE      wan | sta                 (default wan)
 *   UPSTREAM_SSID / UPSTREAM_PASSWORD          (required for sta mode)
 */
import { test, expect } from '@playwright/test';
import { execSync, spawn } from 'child_process';
import { existsSync } from 'fs';
import { homedir } from 'os';
import { basename } from 'path';

const ROUTER_IP = process.env.ROUTER_IP || '192.168.1.1';
const ROUTER_PASSWORD = process.env.ROUTER_PASSWORD || '';
const INSTALLER_BIN = process.env.INSTALLER_BIN || `${homedir()}/tollgate-installer-new`;
const INSTALLER_PORT = process.env.INSTALLER_PORT || '18099';
const WIZARD_URL = `http://localhost:${INSTALLER_PORT}`;
const TOLLGATE_LNURL = process.env.TOLLGATE_LNURL || 'test@example.com';
const DEPLOY_MODE = process.env.DEPLOY_MODE || 'wan';
const UPSTREAM_SSID = process.env.UPSTREAM_SSID || '';
const UPSTREAM_PASSWORD = process.env.UPSTREAM_PASSWORD || '';
const SKIP_SEED = process.env.SKIP_SEED === '1';

const SSH_OPTS =
  '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ' +
  '-o PreferredAuthentications=password -o PubkeyAuthentication=no -o ConnectTimeout=10';

test.skip(!ROUTER_PASSWORD, 'ROUTER_PASSWORD must be set (router root password)');
test.describe.configure({ mode: 'serial' });

// ── helpers ────────────────────────────────────────────────────────────────

function ssh(cmd) {
  const escaped = cmd.replace(/'/g, `'\\''`);
  return execSync(
    `sshpass -p '${ROUTER_PASSWORD}' ssh ${SSH_OPTS} root@${ROUTER_IP} '${escaped}'`,
    { encoding: 'utf8', timeout: 60000, stdio: ['pipe', 'pipe', 'pipe'] },
  ).trim();
}

function sshSafe(cmd) {
  try {
    return ssh(cmd);
  } catch {
    return '';
  }
}

function ipToInt(ip) {
  return ip.split('.').reduce((a, o) => ((a << 8) + (parseInt(o, 10) || 0)) >>> 0, 0) >>> 0;
}

function cidr(c) {
  const [ip, p] = c.split('/');
  const prefix = parseInt(p, 10);
  const mask = prefix >= 0 && prefix <= 32 ? (~0 << (32 - prefix)) >>> 0 : 0xffffffff;
  return { net: (ipToInt(ip) & mask) >>> 0, mask };
}

function overlaps(a, b) {
  const A = cidr(a);
  const B = cidr(b);
  return ((A.net & B.mask) >>> 0) === B.net || ((B.net & A.mask) >>> 0) === A.net;
}

// The interface carrying the default route is the upstream (WAN/STA) interface.
function upstreamIface() {
  return sshSafe(`ip route show default 2>/dev/null | awk '{print $5}' | head -1`);
}

function upstreamCIDR() {
  const iface = upstreamIface();
  if (!iface) return '';
  return sshSafe(`ip -4 -o addr show dev ${iface} 2>/dev/null | awk '{print $4}' | head -1`);
}

async function waitForWizard(page, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await page.request.get(`${WIZARD_URL}/`, { timeout: 3000 });
      if (r.ok()) return;
    } catch {
      /* not up yet */
    }
    await page.waitForTimeout(700);
  }
  throw new Error(`installer not reachable at ${WIZARD_URL}`);
}

test.beforeAll(async () => {
  if (!existsSync(INSTALLER_BIN)) {
    test.skip(true, `installer binary not found: ${INSTALLER_BIN}`);
  }
  // Always start from a FRESH wizard: a long-lived instance can wedge after
  // several deploy jobs (observed: a later run never left the scan view, while
  // a restart fixed it). Kill any listener for this port first.
  try {
    execSync(`pkill -f '${basename(INSTALLER_BIN)}.*-port ${INSTALLER_PORT}'`, { stdio: 'ignore' });
  } catch {
    /* nothing to kill */
  }
  await new Promise((r) => setTimeout(r, 1000));
  const child = spawn(INSTALLER_BIN, ['-port', INSTALLER_PORT], {
    detached: true,
    stdio: 'ignore',
  });
  child.unref();
});

// ── 1. seed the exact broken state seen in the field ─────────────────────────

test('seeds DNS corruption + a subnet collision', async ({ page }) => {
  await waitForWizard(page);
  if (SKIP_SEED) {
    console.log('[seed] SKIP_SEED=1 — leaving the router as-is');
    return;
  }

  const lan = sshSafe(`uci -q get network.lan.ipaddr 2>/dev/null | cut -d/ -f1`);
  expect(lan, 'router LAN IP must be readable').toMatch(/^\d+\.\d+\.\d+\.\d+$/);

  const gw = sshSafe(`ip route show default 2>/dev/null | awk '{print $3}' | head -1`);
  const gwValid = /^\d+\.\d+\.\d+\.\d+$/.test(gw);

  const parts = [
    // (a) DNS corruption exactly as the old installer wrote it.
    `uci -q set dhcp.@dnsmasq[0].address='/tollgate.lan/${lan}/24'`,
    `uci -q set dhcp.lan.dhcp_option='6,${lan}/24'`,
    `sed -i '/tollgate\\.lan/d; /tollgate\\.local/d' /etc/hosts`,
    `echo '${lan}/24 tollgate.lan tollgate.local' >> /etc/hosts`,
  ];
  // (b) force the derived private subnet into the upstream subnet — only when
  // a real upstream gateway is known (otherwise the derived value would be
  // garbage and could break the router's config).
  if (gwValid) {
    const collidingPrivate = `${gw.split('.').slice(0, 3).join('.')}.1`;
    parts.push(
      `if uci -q get network.private >/dev/null 2>&1; then ` +
        `uci -q set network.private.ipaddr='${collidingPrivate}'; ` +
        `uci -q set network.private.netmask='255.255.255.0'; uci commit network; ` +
        `/sbin/ifup private 2>/dev/null; fi`,
    );
    console.log(`[seed] forced private  = ${collidingPrivate} (upstream ${gw})`);
  } else {
    console.warn('[seed] no usable default gateway — skipping the collision seed');
  }
  parts.push(`uci commit dhcp`, `/etc/init.d/dnsmasq restart 2>/dev/null`, `sleep 2; true`);
  sshSafe(parts.join('; '));

  const addr = sshSafe(`uci -q get dhcp.@dnsmasq[0].address`);
  const hosts = sshSafe(`grep tollgate /etc/hosts`);
  console.log(`[seed] dnsmasq address = ${addr}`);
  console.log(`[seed] /etc/hosts      = ${hosts}`);
  expect(addr).toContain(`/tollgate.lan/${lan}/24`);
  expect(hosts).toContain(`${lan}/24`);
});

// ── 2. drive the wizard exactly as an operator would ─────────────────────────

test('wizard deploys and repairs the router', async ({ page }) => {
  test.setTimeout(20 * 60 * 1000);
  await page.goto(WIZARD_URL, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#select-view:not(.hidden)', { timeout: 90000 });

  // Pick the router (detection runs passwordless, so a protected router may
  // show a generic label — select by IP when present, else the only entry).
  const select = page.locator('#router-select');
  const values = await select.locator('option').evaluateAll((opts) => opts.map((o) => o.value));
  expect(values.length, 'at least one router must be discovered').toBeGreaterThan(0);
  await select.selectOption(values.includes(ROUTER_IP) ? ROUTER_IP : values[0]);

  await page.fill('#password', ROUTER_PASSWORD);
  await page.waitForTimeout(1500); // let the debounced identify run

  if (DEPLOY_MODE === 'sta') {
    if (!UPSTREAM_SSID || !UPSTREAM_PASSWORD) {
      throw new Error('sta mode requires UPSTREAM_SSID and UPSTREAM_PASSWORD');
    }
    await page.click('#mode-sta');
    // Switching to STA triggers a WiFi scan that populates the #ssid <select>.
    await page.waitForFunction(
      () => {
        const s = document.getElementById('ssid');
        return s && !s.disabled && [...s.options].some((o) => o.value);
      },
      null,
      { timeout: 90000 },
    );
    await page.locator('#ssid').selectOption(UPSTREAM_SSID);
    await page.fill('#wifi-pass', UPSTREAM_PASSWORD);
  } else {
    await page.click('#mode-wan');
  }
  await page.fill('#lnurl', TOLLGATE_LNURL);

  // STA waits for the automatic upstream WiFi test to confirm before enabling
  // Deploy, so allow generous time here.
  await expect(page.locator('#deploy-btn')).toBeEnabled({ timeout: 180000 });
  await page.click('#deploy-btn');

  await page.waitForSelector('#deploy-view:not(.hidden)', { timeout: 20000 });
  await expect(page.locator('#success-view:not(.hidden)')).toBeVisible({ timeout: 18 * 60 * 1000 });

  const title = await page.locator('#success-view').innerText();
  console.log(`[deploy] success view:\n${title}`);
});

// ── 3. assert the router is healthy afterwards ───────────────────────────────

test('router ends healthy: dnsmasq fixed and no subnet collision', async () => {
  const addr = ssh(`uci -q get dhcp.@dnsmasq[0].address`);
  expect(addr, 'dnsmasq address must be a bare IP (no /24)').toMatch(
    /^\/tollgate\.lan\/\d+\.\d+\.\d+\.\d+$/,
  );

  const opt = ssh(`uci -q get dhcp.lan.dhcp_option`);
  expect(opt, 'DHCP option 6 must be a bare IP').toMatch(/^6,\d+\.\d+\.\d+\.\d+$/);

  const hosts = ssh(`grep tollgate /etc/hosts`);
  expect(hosts, '/etc/hosts must not carry a CIDR suffix').toMatch(
    /^\d+\.\d+\.\d+\.\d+ tollgate\.lan tollgate\.local$/m,
  );
  expect(hosts).not.toMatch(/\d+\/24 tollgate\.lan/);

  expect(sshSafe(`pgrep -f '[d]nsmasq' >/dev/null && echo up || echo down`)).toBe('up');
  expect(sshSafe(`nslookup github.com 127.0.0.1 2>&1 | grep -c 'Address'`)).not.toBe('0');

  const lan = addr.split('/')[2]; // /tollgate.lan/<ip>
  const upstream = upstreamCIDR();
  expect(upstream, 'router must have an upstream address').toMatch(/^\d+\.\d+\.\d+\.\d+\/\d+$/);
  // Only the bridge networks the module serves (the LAN AP + the private AP)
  // must stay out of the upstream subnet — the uplink interface itself
  // (eth1 in WAN mode, phy*-sta* in STA mode) is expected to be in it.
  for (const iface of ['br-lan', 'br-private']) {
    const c = sshSafe(`ip -4 -o addr show dev ${iface} 2>/dev/null | awk '{print $4}' | head -1`);
    if (!c || !c.includes('/')) continue;
    expect(
      overlaps(c, upstream),
      `${iface} ${c} overlaps upstream ${upstream}`,
    ).toBe(false);
  }

  // The deployed backend must actually serve its API.
  expect(sshSafe(`curl -sf -m 5 -o /dev/null -w '%{http_code}' http://${lan}:2121/`)).toBe('200');
});
