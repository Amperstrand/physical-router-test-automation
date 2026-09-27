/**
 * Playwright config for the :8090 "never LuCI" guard.
 *
 *   ROUTER_IP=192.168.1.1 npx playwright test \
 *     --config tests/browser/tollgate-8090-luci-guard.config.mjs
 *
 * ADMIN_URL / LUCI_URL can be overridden to point at a throwaway reproduction
 * listener (see the spec header).
 */
import { defineConfig } from '@playwright/test';

const ROUTER_IP = process.env.ROUTER_IP || '192.168.1.1';

export default defineConfig({
	testDir: '.',
	testMatch: 'tollgate-8090-luci-guard.spec.mjs',
	retries: 0,
	timeout: 60 * 1000,
	workers: 1,
	reporter: [
		['list'],
		['html', { outputFolder: 'tollgate-8090-luci-guard-report', open: 'never' }],
	],
	use: {
		baseURL: process.env.ADMIN_URL || `http://${ROUTER_IP}:8090/`,
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
		// let the browser resolve the router's dnsmasq alias even when the host DNS can't
		launchOptions: {
			args: [`--host-resolver-rules=MAP tollgate.lan ${ROUTER_IP}`],
		},
	},
});
