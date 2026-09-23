// Capture the captive portal still for film act 4.
// Usage: node installer/film_portal_still.mjs <output-dir>
import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';

const out = process.argv[2] || 'results/installer-film/raw/act4';
mkdirSync(out, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
try {
  await page.goto('http://10.99.95.1:2050/', { timeout: 20000, waitUntil: 'load' });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${out}/portal.png` });
  console.log('PORTAL_STILL_OK');
} catch (e) {
  console.error('portal still failed:', e.message);
  process.exitCode = 1;
} finally {
  await browser.close();
}
