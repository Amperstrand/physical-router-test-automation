/**
 * tollgate-portal-lightning.spec.mjs
 *
 * Hardware regression for the captive-portal Lightning tab + balance page:
 *
 *   1. The Lightning tab is enabled only when the backend answers the runtime
 *      capability probe (GET /ln-invoice). A greyed/`data-disabled="true"` tab
 *      on a lightning-capable build is a regression.
 *   2. Selecting Lightning opens the amount input (the payment flow mounts).
 *   3. The standalone balance page (":2051/balance.html") boots and renders —
 *      it must fetch ":2121/balance", not the portal origin (the old bug
 *      resolved `/balance` against :2051 and 404'd).
 *
 * Run from a host that can reach the router LAN (e.g. CobradorWave):
 *   ROUTER_IP=192.168.1.1 npx playwright test \
 *     --config tests/browser/tollgate-portal-lightning.config.mjs
 */
import { test, expect } from '@playwright/test';

const ROUTER_IP = process.env.ROUTER_IP || '192.168.1.1';
const PORTAL_PORT = process.env.PORTAL_PORT || '2051';
const PORTAL = `http://${ROUTER_IP}:${PORTAL_PORT}`;

const HYDRATE_TIMEOUT = 30000;

test.describe.configure({ mode: 'serial' });

test('1. Lightning tab is enabled by the capability probe', async ({ page }) => {
  await page.goto(`${PORTAL}/splash.html`, { waitUntil: 'domcontentloaded', timeout: HYDRATE_TIMEOUT });

  const tab = page.locator('#tab-lightning');
  await tab.waitFor({ state: 'visible', timeout: HYDRATE_TIMEOUT });

  // the probe is async — wait for it to resolve to enabled
  await expect(
    tab,
    'Lightning tab stayed disabled: the gateway did not answer the /ln-invoice capability probe',
  ).toHaveAttribute('data-disabled', 'false', { timeout: HYDRATE_TIMEOUT });

  console.log('[portal] lightning tab enabled (capability probe passed)');
});

test('2. selecting Lightning mounts the amount input', async ({ page }) => {
  await page.goto(`${PORTAL}/splash.html`, { waitUntil: 'domcontentloaded', timeout: HYDRATE_TIMEOUT });
  const tab = page.locator('#tab-lightning');
  await tab.waitFor({ state: 'visible', timeout: HYDRATE_TIMEOUT });
  await expect(tab).toHaveAttribute('data-disabled', 'false', { timeout: HYDRATE_TIMEOUT });

  await tab.click();
  await expect(page.locator('#lightning-unit-amount')).toBeVisible({ timeout: HYDRATE_TIMEOUT });
  console.log('[portal] lightning method mounted');
});

test('3. standalone balance page boots and renders', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));

  await page.goto(`${PORTAL}/balance.html`, { waitUntil: 'domcontentloaded', timeout: HYDRATE_TIMEOUT });

  const root = page.locator('.tollgate-captive-portal-balance-page');
  await root.waitFor({ state: 'visible', timeout: HYDRATE_TIMEOUT });

  await page.screenshot({ path: 'test-results/tollgate-portal-lightning-balance.png', fullPage: true });
  expect(errors, `page errors on balance page:\n${errors.join('\n')}`).toEqual([]);
  console.log('[portal] balance page rendered');
});
