import { defineConfig } from '@playwright/test';
import { readFileSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

// Load .env from project root into process.env.
// Playwright's built-in dotenv loading doesn't trigger when config is in a
// subdirectory — this ensures env vars are always available.
const envPath = resolve(dirname(fileURLToPath(import.meta.url)), '..', '.env');
try {
	for (const line of readFileSync(envPath, 'utf8').split('\n')) {
		const m = line.match(/^([A-Z_][A-Z0-9_]*)=(.*)/);
		if (m && !(m[1] in process.env)) process.env[m[1]] = m[2];
	}
} catch { /* .env is optional */ }

const viewport = process.env.TOLLGATE_VIEWPORT || 'desktop';
const viewports = {
	desktop: { width: 1280, height: 900 },
	mobile: { width: 375, height: 812 },
};

// Which browser binary to launch.
//
// Playwright's bundled chromium cannot be installed on every host: on
// ubuntu26.04-x64 `playwright install chromium` refuses with
// "ERROR: Playwright does not support chromium on ubuntu26.04-x64" (measured
// 2026-10-02), which leaves the whole browser suite unrunnable even though a
// perfectly good system Chrome is present. Prefer an explicit override, then a
// known system browser, and only then fall back to the bundled build (an empty
// object = Playwright's default).
function chromiumLaunchOptions() {
	const explicit = process.env.PLAYWRIGHT_CHROME || process.env.CHROME_PATH;
	if (explicit) return { executablePath: explicit };
	for (const p of [
		'/usr/bin/google-chrome',
		'/usr/bin/google-chrome-stable',
		'/usr/bin/chromium',
		'/usr/bin/chromium-browser',
		'/snap/bin/chromium',
	]) {
		if (existsSync(p)) return { executablePath: p, args: ['--no-sandbox', '--disable-dev-shm-usage'] };
	}
	return {};
}

// Projects enforce ordering: non-destructive tests run first,
// destructive tests (reboot, firmware) run last since they leave
// the router in a transitional state.
export default defineConfig({
	testDir: '.',
	testMatch: '**/*.spec.mjs',
	retries: 1,
	timeout: 60000,
	workers: 1,
	reporter: [
		['html', { outputFolder: 'report', open: 'never' }],
		['json', { outputFile: 'report/report.json' }],
		['list'],
	],
	use: {
		baseURL: process.env.TOLLGATE_LUCI_URL ?? 'http://192.168.1.1:8080',
		launchOptions: chromiumLaunchOptions(),
		screenshot: 'on',
		trace: 'on-first-retry',
		actionTimeout: 10000,
		storageState: { cookies: [], origins: [] },
	},
	projects: [
		{
			name: `${viewport}-luci`,
			testMatch: 'web/tollgate.spec.mjs',
			use: { viewport: viewports[viewport] || viewports.desktop },
		},
		{
			name: `${viewport}-portal`,
			testMatch: 'captive_portal.spec.mjs',
			use: { viewport: viewports[viewport] || viewports.desktop },
		},
		{
			name: `${viewport}-admin`,
			testMatch: 'admin_spa.spec.mjs',
			use: { viewport: viewports[viewport] || viewports.desktop },
		},
		{
			name: `${viewport}-admin-login`,
			testMatch: 'admin-login.spec.mjs',
			use: { viewport: viewports[viewport] || viewports.desktop },
		},
		{
			name: `${viewport}-css-regression`,
			testMatch: 'css-regression.spec.mjs',
			use: { viewport: viewports[viewport] || viewports.desktop },
		},
		{
			name: `${viewport}-cudy`,
			testMatch: 'browser/cudy_wr3000.spec.mjs',
			// Read-only by default; the one mutating probe (OEM firmware route) is
			// env-gated inside the spec, so no retry is needed to be safe.
			retries: 0,
			use: { viewport: viewports[viewport] || viewports.desktop },
		},
		{
			name: `${viewport}-protocol`,
			testMatch: /protocol\/(?:payment-protocol|payment-lifecycle|data-allotment|router-network-config|tollgate-payment-protocol)\.spec\.mjs/,
			dependencies: [`${viewport}-luci`],
			use: { viewport: viewports[viewport] || viewports.desktop },
		},
		{
			name: `${viewport}-destructive`,
			testMatch: /destructive\/(?:reboot-recovery|firmware-upgrade)\.spec\.mjs/,
			dependencies: [`${viewport}-protocol`],
			retries: 0,
			use: { viewport: viewports[viewport] || viewports.desktop },
		},
	],
});
