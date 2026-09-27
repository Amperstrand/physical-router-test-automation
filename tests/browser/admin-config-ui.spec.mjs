/**
 * Admin Config UI — full logged-in walkthrough with video.
 * 
 * Records the net4sats admin dashboard (configurationwizzard) which
 * plugs into OpenWrt via ubus schemas — the same way LuCI does.
 * 
 * Shows: Login → Dashboard → WiFi → Devices → Settings → Wallet
 */
import { test, expect } from '@playwright/test';

const ADMIN_URL = 'http://192.168.1.1:8090/net4sats/';
const PASSWORD = process.env.TOLLGATE_LUCI_PASSWORD || 'test123';

test.describe.configure({ mode: 'serial' });

test('1. Login to net4sats admin', async ({ page }) => {
	await page.goto(ADMIN_URL, { waitUntil: 'networkidle', timeout: 15000 });
	await page.waitForTimeout(1500);
	
	// The SPA shows a login form with net4sats branding
	await page.screenshot({ path: 'test-results/admin-login-page.png' });
	
	// Fill login form
	await page.fill('#username', 'root');
	await page.fill('#password', PASSWORD);
	await page.waitForTimeout(500);
	
	// Click sign in
	await page.locator('button[type="submit"]').click();
	
	// Wait for dashboard to load
	await page.waitForTimeout(3000);
	await page.screenshot({ path: 'test-results/admin-dashboard.png' });
});

test('2. Dashboard — system overview', async ({ page }) => {
	await page.goto(ADMIN_URL, { waitUntil: 'networkidle', timeout: 15000 });
	
	// Login
	await page.fill('#username', 'root');
	await page.fill('#password', PASSWORD);
	await page.click('button[type="submit"]');
	await page.waitForTimeout(3000);
	
	// Dashboard should show hostname, uptime, WAN status, tollgate status
	await page.screenshot({ path: 'test-results/admin-01-dashboard.png' });
});

test('3. WiFi tab — SSID and network config', async ({ page }) => {
	await page.goto(ADMIN_URL, { waitUntil: 'networkidle', timeout: 15000 });
	await page.fill('#username', 'root');
	await page.fill('#password', PASSWORD);
	await page.click('button[type="submit"]');
	await page.waitForTimeout(2000);
	
	// Click WiFi nav item
	await page.locator('text=WiFi').first().click();
	await page.waitForTimeout(2000);
	await page.screenshot({ path: 'test-results/admin-02-wifi.png' });
});

test('4. Devices tab — connected clients', async ({ page }) => {
	await page.goto(ADMIN_URL, { waitUntil: 'networkidle', timeout: 15000 });
	await page.fill('#username', 'root');
	await page.fill('#password', PASSWORD);
	await page.click('button[type="submit"]');
	await page.waitForTimeout(2000);
	
	await page.locator('text=Devices').first().click();
	await page.waitForTimeout(2000);
	await page.screenshot({ path: 'test-results/admin-03-devices.png' });
});

test('5. Settings tab — tollgate config schema', async ({ page }) => {
	await page.goto(ADMIN_URL, { waitUntil: 'networkidle', timeout: 15000 });
	await page.fill('#username', 'root');
	await page.fill('#password', PASSWORD);
	await page.click('button[type="submit"]');
	await page.waitForTimeout(2000);
	
	await page.locator('text=Settings').first().click();
	await page.waitForTimeout(2000);
	await page.screenshot({ path: 'test-results/admin-04-settings.png' });
});

test('6. Wallet tab — balance and payments', async ({ page }) => {
	await page.goto(ADMIN_URL, { waitUntil: 'networkidle', timeout: 15000 });
	await page.fill('#username', 'root');
	await page.fill('#password', PASSWORD);
	await page.click('button[type="submit"]');
	await page.waitForTimeout(2000);
	
	await page.locator('text=Wallet').first().click();
	await page.waitForTimeout(2000);
	await page.screenshot({ path: 'test-results/admin-05-wallet.png' });
});
