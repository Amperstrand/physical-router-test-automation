// admin-demo.mjs — Load net4sats admin SPA, login, screenshot all pages, record video.
import { chromium } from '@playwright/test';

const BASE = process.env.ADMIN_BASE || 'http://192.168.1.1:8080/net4sats/';
const OUT = '/tmp/admin-demo-out';
import { mkdirSync } from 'fs';
mkdirSync(OUT, { recursive: true });

const EXE = '/home/c03rad0r/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome';

const browser = await chromium.launch({
  headless: true,
  executablePath: EXE,
  args: ['--no-sandbox', '--disable-setuid-sandbox'],
});

const ctx = await browser.newContext({
  viewport: { width: 1280, height: 800 },
  ignoreHTTPSErrors: true,
  recordVideo: { dir: OUT, size: { width: 1280, height: 800 } },
});

const page = await ctx.newPage();
const logs = [];
page.on('console', m => logs.push(`[${m.type()}] ${m.text()}`));
page.on('pageerror', e => logs.push(`[pageerror] ${e.message}`));

console.log(`=== Navigating to ${BASE} ===`);
await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 20000 }).catch(e => console.log('goto:', e.message));
await page.waitForTimeout(4000);

console.log('=== TITLE ===', await page.title());
console.log('=== URL ===', page.url());
console.log('=== BODY (first 500) ===');
const body0 = await page.evaluate(() => document.body.innerText).catch(() => '(none)');
console.log(body0?.slice(0, 500));
await page.screenshot({ path: `${OUT}/01-initial-load.png`, fullPage: true });

// Try login — admin uses rpcd/ubus session login
async function tryAdminLogin(page) {
  const passwordInput = await page.$('input[type="password"]');
  if (!passwordInput) {
    console.log('  no password field found');
    return false;
  }
  console.log('  found password field, attempting login...');
  // Try common passwords
  const passwords = [process.env.TOLLGATE_LUCI_PASSWORD || '', 'test123', 'root', 'password', 'admin', ''];
  for (const pw of passwords) {
    if (!pw && passwords.indexOf(pw) !== 0) continue;
    await passwordInput.fill(pw);
    const userInput = await page.$('input[type="text"], input[name*="user" i], input[name*="name" i]');
    if (userInput) await userInput.fill('root');
    const submitBtn = await page.$('button[type="submit"], button:has-text("Login"), button:has-text("Sign"), input[type="submit"]');
    if (submitBtn) await submitBtn.click();
    else await passwordInput.press('Enter');
    await page.waitForTimeout(3000);
    // Check if we're still on login
    const stillPw = await page.$('input[type="password"]');
    if (!stillPw) {
      console.log(`  login succeeded with password: "${pw}"`);
      return true;
    }
    console.log(`  password "${pw}" failed, trying next...`);
  }
  return false;
}

const loggedIn = await tryAdminLogin(page);
console.log('=== LOGGED IN ===', loggedIn);
await page.screenshot({ path: `${OUT}/02-after-login.png`, fullPage: true });

// Navigate through all sidebar pages
const navItems = await page.$$eval('nav a, .sidebar a, [role="navigation"] a, a[href]', els =>
  els.map(el => ({ text: (el.textContent || '').trim(), href: el.getAttribute('href') || '' }))
    .filter(l => l.text && l.text.length > 0 && l.text.length < 50)
).catch(() => []);

console.log('=== NAV LINKS ===');
navItems.forEach(l => console.log(`  ${l.text} -> ${l.href}`));

const pages = ['dashboard', 'wifi', 'devices', 'settings', 'wallet'];
for (let i = 0; i < pages.length; i++) {
  const p = pages[i];
  console.log(`\n=== Navigating to ${p} ===`);
  // Try clicking nav link
  const link = await page.$(`a:has-text("${p}")`, { exact: false }).catch(() => null);
  if (link) {
    await link.click().catch(() => {});
  } else {
    // Try hash navigation
    await page.goto(`${BASE}#${p}`, { waitUntil: 'domcontentloaded' }).catch(() => {});
  }
  await page.waitForTimeout(3000);
  const body = await page.evaluate(() => document.body.innerText).catch(() => '(none)');
  console.log(`  ${p} body (first 300):`, body?.slice(0, 300)?.replace(/\n/g, ' '));
  await page.screenshot({ path: `${OUT}/0${i + 3}-${p}.png`, fullPage: true });
}

console.log('\n=== CONSOLE LOGS (last 30) ===');
console.log(logs.slice(-30).join('\n'));

await page.close();
await ctx.close();
const vid = (await browser.close().catch(() => {}), 'done');
console.log(`\n=== Screenshots + video saved to ${OUT} ===`);
