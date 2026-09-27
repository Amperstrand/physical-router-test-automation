import { test, expect } from '@playwright/test';

const ADMIN_URL = process.env.ADMIN_URL || 'http://192.168.1.1:8090/net4sats/';
const ADMIN_USER = process.env.ADMIN_USER || 'root';
const ADMIN_PASS = process.env.ADMIN_PASS || 'test123';

test.use({
  video: 'on',
  screenshot: 'on',
  ignoreHTTPSErrors: true,
  viewport: { width: 1280, height: 800 },
});

test.setTimeout(120000);

test('net4sats Admin Config UI — Full Tour', async ({ page, context }) => {
  // Step 1: Load SPA and log in
  await page.goto(ADMIN_URL, { waitUntil: 'networkidle', timeout: 15000 });

  // Wait for EITHER login form OR existing dashboard (in case session persisted)
  await page.waitForTimeout(2000);

  const loginForm = page.locator('#password');
  if (await loginForm.isVisible({ timeout: 8000 }).catch(() => false)) {
    await page.locator('#username').fill(ADMIN_USER);
    await page.locator('#password').fill(ADMIN_PASS);
    await page.locator('button[type="submit"]').click();
    // Wait for dashboard to load — header means we're authenticated
    await expect(page.locator('header')).toBeVisible({ timeout: 15000 });
    await page.waitForTimeout(2000);
  }

  // --- Dashboard ---
  await expect(page.locator('header')).toBeVisible();
  const navItems = page.locator('.nav-item');
  await expect(navItems).toHaveCount(5);
  await page.screenshot({ path: '/tmp/screenshots/admin-01-dashboard.png', fullPage: true });
  await page.waitForTimeout(2000);

  // --- WiFi ---
  await page.evaluate(() => { window.location.hash = '/wifi'; });
  await page.waitForTimeout(3000);
  // Verify we're NOT seeing an error
  const wifiError = page.locator('text=SESSION_EXPIRED');
  await expect(wifiError).not.toBeVisible();
  await page.screenshot({ path: '/tmp/screenshots/admin-02-wifi.png', fullPage: true });
  await page.waitForTimeout(2000);

  // --- Devices ---
  await page.evaluate(() => { window.location.hash = '/devices'; });
  await page.waitForTimeout(3000);
  const devicesError = page.locator('text=SESSION_EXPIRED');
  await expect(devicesError).not.toBeVisible();
  await page.screenshot({ path: '/tmp/screenshots/admin-03-devices.png', fullPage: true });
  await page.waitForTimeout(2000);

  // --- Settings ---
  await page.evaluate(() => { window.location.hash = '/settings'; });
  await page.waitForTimeout(4000); // settings page is heavy (schema forms)
  const settingsError = page.locator('text=SESSION_EXPIRED');
  await expect(settingsError).not.toBeVisible();
  await page.screenshot({ path: '/tmp/screenshots/admin-04-settings.png', fullPage: true });
  await page.waitForTimeout(2000);

  // --- Wallet ---
  await page.evaluate(() => { window.location.hash = '/wallet'; });
  await page.waitForTimeout(3000);
  const walletError = page.locator('text=SESSION_EXPIRED');
  await expect(walletError).not.toBeVisible();
  await page.screenshot({ path: '/tmp/screenshots/admin-05-wallet.png', fullPage: true });
  await page.waitForTimeout(2000);
});
