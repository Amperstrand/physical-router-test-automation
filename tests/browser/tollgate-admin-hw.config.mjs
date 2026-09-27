/**
 * Playwright config for the admin-board hardware smoke (:8090, root).
 *
 *   ROUTER_IP=192.168.1.1 ROUTER_PASSWORD=... npx playwright test \
 *     --config tests/browser/tollgate-admin-hw.config.mjs
 */
import { defineConfig } from '@playwright/test';

const ROUTER_IP = process.env.ROUTER_IP || '192.168.1.1';

export default defineConfig({
	testDir: '.',
	testMatch: 'tollgate-admin-hw.spec.mjs',
	retries: 0,
	timeout: 90 * 1000,
	workers: 1,
	reporter: [
		['list'],
		['html', { outputFolder: 'tollgate-admin-hw-report', open: 'never' }],
	],
	use: {
		baseURL: `http://${ROUTER_IP}:8090/`,
		headless: true,
		channel: 'chrome',
		viewport: { width: 1280, height: 900 },
		screenshot: 'on',
		trace: 'retain-on-failure',
		// Playwright's ffmpeg bundle is unavailable on some hosts (ubuntu26.04)
		video: 'off',
		ignoreHTTPSErrors: true,
		actionTimeout: 20000,
		navigationTimeout: 30000,
	},
});
