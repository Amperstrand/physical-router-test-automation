// Record a demo video of the tollgate-installer wizard deploying TollGate
// to the fresh QEMU router booted by installer/lab_vm.py.
//
// Usage:
//   TOLLGATE_LAB_PASSWORD=<router pw> node installer/record_demo.mjs
//
// Env:
//   TOLLGATE_INSTALLER_BIN  wizard binary (default: the clean-clone build)
//   TOLLGATE_DEMO_LN        Lightning address to enter (default tollgate@minibits.cash)
//   TOLLGATE_DEMO_OUT       output dir (default results/installer-demo)
//
// The wizard is spawned as a child of this process and always torn down.
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, renameSync } from 'node:fs';
import { chromium } from 'playwright';

const BIN = process.env.TOLLGATE_INSTALLER_BIN ||
  '/tmp/opencode/tollgate-installer-clean/tollgate-installer';
const PASSWORD = process.env.TOLLGATE_LAB_PASSWORD;
const LN = process.env.TOLLGATE_DEMO_LN || 'tollgate@minibits.cash';
const OUT = process.env.TOLLGATE_DEMO_OUT || 'results/installer-demo';
const ROUTER_IP = '10.99.95.1';

if (!PASSWORD) {
  console.error('TOLLGATE_LAB_PASSWORD is required (router root password)');
  process.exit(2);
}
if (!existsSync(BIN)) {
  console.error(`wizard binary not found: ${BIN}`);
  process.exit(2);
}
mkdirSync(OUT + '/video', { recursive: true });

function pickPort(start) {
  return new Promise((resolve) => {
    const probe = (p) => {
      fetch(`http://127.0.0.1:${p}/`, { signal: AbortSignal.timeout(700) })
        .then(() => probe(p + 1))
        .catch(() => resolve(p));
    };
    probe(start);
  });
}

const port = await pickPort(8199);
console.log(`wizard on :${port}`);
const wizard = spawn(BIN, ['-port', String(port)], { stdio: 'ignore' });
await new Promise(r => setTimeout(r, 1500));

let browser;
let failed = false;
try {
  browser = await chromium.launch();
  const ctx = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    recordVideo: { dir: OUT + '/video', size: { width: 1280, height: 800 } },
  });
  const page = await ctx.newPage();
  const url = `http://127.0.0.1:${port}/`;

  console.log('waiting for scan to finish (ARP sweep ~45s)...');
  await page.goto(url);
  await page.waitForSelector('#select-view:not(.hidden)', { timeout: 180000 });
  await page.selectOption('#router-select', ROUTER_IP);
  await page.screenshot({ path: OUT + '/01-scan-results.png' });
  await page.fill('#password', PASSWORD);
  await page.click('#mode-wan');
  await page.fill('#lnurl', LN);
  await page.screenshot({ path: OUT + '/02-form-filled.png' });

  await page.waitForSelector('#deploy-btn:not([disabled])', { timeout: 15000 });
  await page.click('#deploy-btn');
  console.log('deploy clicked — recording step progress...');
  await page.waitForSelector('#steps-list .step', { timeout: 60000 });
  await page.screenshot({ path: OUT + '/03-deploy-steps.png' });

  const outcome = await Promise.race([
    page.waitForSelector('#success-view:not(.hidden)', { timeout: 420000 }).then(() => 'success'),
    page.waitForSelector('#error-view:not(.hidden)', { timeout: 420000 }).then(() => 'error'),
  ]);
  await page.screenshot({ path: `${OUT}/04-${outcome}.png` });
  console.log(`outcome: ${outcome}`);
  failed = outcome !== 'success';

  await ctx.close();  // flushes the video
} catch (e) {
  failed = true;
  console.error('recording failed:', e.message);
} finally {
  if (browser) await browser.close();
  wizard.kill('SIGTERM');
}

const vids = readdirSync(OUT + '/video').filter(f => f.endsWith('.webm'));
if (vids.length) {
  renameSync(`${OUT}/video/${vids[0]}`, OUT + '/tollgate-installer-demo.webm');
  console.log(`video: ${OUT}/tollgate-installer-demo.webm`);
}
process.exit(failed ? 1 : 0);
