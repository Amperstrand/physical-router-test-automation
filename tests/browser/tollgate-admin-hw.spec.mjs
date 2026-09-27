/**
 * tollgate-admin-hw.spec.mjs
 *
 * Hardware smoke for the admin board served on :8090 (TollGate brand at
 * root path). Logs in with the router root password via ubus session.login
 * and asserts the dashboard shell renders.
 *
 * Requires the admin build + rpcd plugin installed (see deployment-kit and the
 * portal repo's packaging/files/etc/uci-defaults/92-tollgate-admin-setup).
 *
 *   ROUTER_IP=192.168.1.1 ROUTER_PASSWORD=... npx playwright test \
 *     --config tests/browser/tollgate-admin-hw.config.mjs
 */
import { test, expect } from '@playwright/test';

const ROUTER_PASSWORD = process.env.ROUTER_PASSWORD || 'password';

test('admin board: login and dashboard render', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));

  await page.goto('./', { waitUntil: 'domcontentloaded', timeout: 30000 });

  await page.waitForSelector('input#password', { timeout: 30000 });
  await page.fill('input#username', 'root');
  await page.fill('input#password', ROUTER_PASSWORD);
  await page.click('button[type="submit"]');

  await page.waitForSelector('.app-header', { timeout: 30000 });
  await expect(page.locator('.app-header')).toBeVisible();
  await expect(page.locator('.app-nav')).toBeVisible();

  await page.screenshot({ path: 'test-results/tollgate-admin-hw.png', fullPage: true });
  expect(errors, `page errors:\n${errors.join('\n')}`).toEqual([]);
});
