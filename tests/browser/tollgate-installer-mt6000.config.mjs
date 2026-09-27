/**
 * Playwright config for the GL-MT6000 TollGate installer regression
 * (DNS self-heal + subnet-collision relocation).
 *
 * Run from the machine that can reach the router's LAN (CobradorWave):
 *   cd ~/physical-router-test-automation
 *   ROUTER_PASSWORD=password INSTALLER_BIN=~/tollgate-installer-new \
 *     npx playwright test --config tests/browser/tollgate-installer-mt6000.config.mjs
 */
import { defineConfig } from '@playwright/test';

const PORT = process.env.INSTALLER_PORT || '18099';

export default defineConfig({
	testDir: '.',
	testMatch: 'tollgate-installer-mt6000.spec.mjs',
	retries: 0,
	// Generous: a full wizard deploy (package install + branding + health)
	// can take several minutes on the MT6000.
	timeout: 20 * 60 * 1000,
	workers: 1,
	reporter: [
		['list'],
		['html', { outputFolder: 'tollgate-installer-mt6000-report', open: 'never' }],
	],
	use: {
		baseURL: `http://localhost:${PORT}`,
		headless: true,
		channel: 'chrome',
		viewport: { width: 1280, height: 900 },
		screenshot: 'on',
		trace: 'retain-on-failure',
		video: 'on',
		actionTimeout: 30000,
		navigationTimeout: 30000,
	},
});
