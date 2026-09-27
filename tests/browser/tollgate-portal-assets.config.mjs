/**
 * Playwright config for the captive-portal asset-integrity regression
 * (feed package must ship the hashed /assets bundles referenced by splash.html).
 *
 * Run from a host that can reach the router LAN (e.g. CobradorWave):
 *   ROUTER_IP=192.168.1.1 npx playwright test \
 *     --config tests/browser/tollgate-portal-assets.config.mjs
 */
import { defineConfig } from '@playwright/test';

const ROUTER_IP = process.env.ROUTER_IP || '192.168.1.1';

export default defineConfig({
	testDir: '.',
	testMatch: 'tollgate-portal-assets.spec.mjs',
	retries: 0,
	timeout: 2 * 60 * 1000,
	workers: 1,
	reporter: [
		['list'],
		['html', { outputFolder: 'tollgate-portal-assets-report', open: 'never' }],
	],
	use: {
		baseURL: `http://${ROUTER_IP}:2051`,
		headless: true,
		channel: 'chrome',
		viewport: { width: 1280, height: 900 },
		screenshot: 'on',
		trace: 'retain-on-failure',
		video: 'on',
		ignoreHTTPSErrors: true,
		actionTimeout: 20000,
		navigationTimeout: 30000,
	},
});
