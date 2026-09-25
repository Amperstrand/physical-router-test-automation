// Capture the captive portal still for film act 4 — as a client lands on it:
// start at the NDS splash URL and let the browser follow the shim's
// location.replace() to the real portal on :2051. Gates on a non-blank body.
// Usage: node installer/film_portal_still.mjs <output-dir>
import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';

const out = process.argv[2] || 'results/installer-film/raw/act4';
mkdirSync(out, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
try {
  const splash = 'http://10.99.95.1:2050/splash.html?redir=http%3a%2f%2f1.1.1.1%2f';
  await page.goto(splash, { timeout: 25000, waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);
  const url = page.url();
  const text = (await page.locator('body').innerText().catch(() => '')).trim();
  if (text.length < 40) {
    throw new Error(`portal body looks blank (${text.length} chars) at ${url}`);
  }
  await page.screenshot({ path: `${out}/portal.png` });
  console.log(`PORTAL_STILL_OK url=${url} textlen=${text.length}`);
} catch (e) {
  console.error('portal still failed:', e.message);
  process.exitCode = 1;
} finally {
  await browser.close();
}
