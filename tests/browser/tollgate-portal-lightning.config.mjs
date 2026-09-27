/**
 * Playwright config for the captive-portal Lightning + balance-page regression.
 *
 * Run from a host that can reach the router LAN (e.g. CobradorWave):
 *   ROUTER_IP=192.168.1.1 npx playwright test \
 *     --config tests/browser/tollgate-portal-lightning.config.mjs
 */
import { defineConfig } from '@playwright/test';

const ROUTER_IP = process.env.ROUTER_IP || '192.168.1.1';

export default defineConfig({
	testDir: '.',
	testMatch: 'tollgate-portal-lightning.spec.mjs',
	retries: 0,
	timeout: 2 * 60 * 1000,
	workers: 1,
	reporter: [
		['list'],
		['html', { outputFolder: 'tollgate-portal-lightning-report', open: 'never' }],
	],
	use: {
		baseURL: `http://${ROUTER_IP}:2051`,
		headless: true,
		channel: 'chrome',
		viewport: { width: 1280, height: 900 },
		screenshot: 'on',
		trace: 'retain-on-failure',
		// no video: Playwright's ffmpeg bundle is unavailable on some distros
		// (e.g. ubuntu26.04), which would fail the run before any assertion
		video: 'off',
		ignoreHTTPSErrors: true,
		actionTimeout: 20000,
		navigationTimeout: 30000,
	},
});
