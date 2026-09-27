import { test, expect } from '@playwright/test';
import { execSync } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';
import {
	SEL_SUCCESS,
	SEL_SUBMIT_CLICK,
	SEL_SUBMIT_READY,
	SEL_TOKEN_INPUT,
	fillCashuToken,
	openCashuTab,
} from './helpers/portal-selectors.mjs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const MINT_TOKEN_BIN = path.resolve(__dirname, '..', 'scripts', 'mint-token', 'mint-token');
const MINT_URL = process.env.TOLLGATE_MINT_URL || 'https://testnut-compat.mints.orangesync.tech';
const MINT_AMOUNT = '1';
const MINT_RETRIES = 3;
const MINT_RETRY_DELAY_MS = 3000;

function mintToken(url, amount, retries = MINT_RETRIES) {
	let lastError;
	for (let attempt = 1; attempt <= retries; attempt++) {
		try {
			return execSync(`${MINT_TOKEN_BIN} ${url} ${amount}`, {
				timeout: 60000,
				encoding: 'utf-8',
			});
		} catch (e) {
			lastError = e;
			const msg = e.stderr || e.message || '';
			if (attempt < retries) {
				console.log(`  mint-token attempt ${attempt}/${retries} failed (${msg.trim().slice(0, 120)}), retrying in ${MINT_RETRY_DELAY_MS}ms...`);
				execSync(`sleep ${MINT_RETRY_DELAY_MS / 1000}`);
			}
		}
	}
	throw lastError;
}

const ROUTER_IP = process.env.TOLLGATE_CAPTIVE_PORTAL_HOST || '192.168.41.1';
const API_BASE = `http://${ROUTER_IP}:2121`;
const PORTAL_PORT = process.env.TOLLGATE_CAPTIVE_PORTAL_PORT || '80';
const PORTAL_BASE = `http://${ROUTER_IP}:${PORTAL_PORT}`;

async function hasBareZeroLiterals(page) {
	const content = await page.locator('.tollgate-captive-portal-method-content').innerText();
	const lines = content.split('\n').map(l => l.trim()).filter(l => l);
	return lines.filter(l => l === '0');
}

async function getApiResponse(request) {
	const response = await request.get(`${API_BASE}/`, { timeout: 10000 });
	if (!response.ok()) return null;
	return await response.json();
}

test.describe('Captive Portal — no bare "0" literals (bug fix verification)', () => {
	test('portal cashu tab has no bare "0" text nodes', async ({ page }) => {
		await page.goto(`${PORTAL_BASE}/splash.html`, { waitUntil: 'networkidle', timeout: 30000 });
		await page.waitForSelector('.tollgate-captive-portal-view', { timeout: 15000 });
		await page.waitForTimeout(3000);

		const view = page.locator('.tollgate-captive-portal-view');
		const text = await view.innerText();
		const lines = text.split('\n').map(l => l.trim()).filter(l => l);
		const bareZeros = lines.filter(l => l === '0');
		expect(bareZeros, `Should not render bare "0" text nodes, found: [${bareZeros}]`).toHaveLength(0);
	});

	test('portal lightning tab has no bare "0" text nodes', async ({ page }) => {
		await page.goto(`${PORTAL_BASE}/splash.html`, { waitUntil: 'networkidle', timeout: 30000 });

		const lightningTab = page.locator('#tab-lightning');
		if (await lightningTab.isVisible()) {
			await lightningTab.click();
		}
		await page.waitForTimeout(3000);

		const view = page.locator('.tollgate-captive-portal-view');
		const text = await view.innerText();
		const lines = text.split('\n').map(l => l.trim()).filter(l => l);
		const bareZeros = lines.filter(l => l === '0');
		expect(bareZeros, `Lightning tab should not render bare "0" text nodes, found: [${bareZeros}]`).toHaveLength(0);
	});
});

test.describe('Captive Portal — degraded mode (backend notice)', () => {
	test('shows backend error message when mints are unreachable', async ({ page }) => {
		const apiEvent = await getApiResponse(page.context().request || page.request);
		if (!apiEvent || apiEvent.kind !== 21023) {
			test.skip();
			return;
		}

		await page.goto(`${PORTAL_BASE}/splash.html`, { waitUntil: 'networkidle', timeout: 30000 });
		await page.waitForSelector('.status.error', { timeout: 15000 });

		const errorBox = page.locator('.status.error');
		await expect(errorBox).toBeVisible();

		const errorText = await errorBox.innerText();
		expect(errorText.length, 'Error message should not be empty').toBeGreaterThan(0);

		expect(errorText, 'Should contain "unreachable" or "initializing" or "unavailable"').toMatch(/unreachable|initializing|unavailable|No reachable mints/i);
	});

	test('shows retrying indicator when in degraded mode', async ({ page }) => {
		const apiEvent = await getApiResponse(page.context().request || page.request);
		if (!apiEvent || apiEvent.kind !== 21023) {
			test.skip();
			return;
		}

		await page.goto(`${PORTAL_BASE}/splash.html`, { waitUntil: 'networkidle', timeout: 30000 });
		await page.waitForSelector('.status.error', { timeout: 15000 });

		const retrying = page.locator('.tollgate-captive-portal-retrying');
		await expect(retrying).toBeVisible({ timeout: 10000 });

		const text = await retrying.innerText();
		expect(text.toLowerCase(), 'Should show retrying indicator').toContain('retry');
	});

	test('hides payment tabs when in degraded mode', async ({ page }) => {
		const apiEvent = await getApiResponse(page.context().request || page.request);
		if (!apiEvent || apiEvent.kind !== 21023) {
			test.skip();
			return;
		}

		await page.goto(`${PORTAL_BASE}/splash.html`, { waitUntil: 'networkidle', timeout: 30000 });
		await page.waitForSelector('.status.error', { timeout: 15000 });

		// The tabbed portal only renders the token input once the Cashu tab is
		// active, so the payment inputs are absent/not visible in degraded mode.
		const cashuInput = page.locator(SEL_TOKEN_INPUT);
		await expect(cashuInput).not.toBeVisible();
	});
});

test.describe('Captive Portal — happy path (mints reachable)', () => {
	test.skip(async ({ request }) => {
		const event = await getApiResponse(request);
		return !event || event.kind !== 10021 || !event.tags?.some(t => t[0] === 'price_per_step');
	}, 'Skipped: tollgate service has no reachable mints (degraded mode)');

	test('API returns valid advertisement with pricing', async ({ request }) => {
		const event = await getApiResponse(request);
		expect(event.kind, 'Event kind should be 10021').toBe(10021);

		const priceTags = event.tags.filter(t => t[0] === 'price_per_step');
		expect(priceTags.length, 'Should have at least one price_per_step tag').toBeGreaterThan(0);
	});

	test('portal shows cashu token input', async ({ page }) => {
		await page.goto(`${PORTAL_BASE}/splash.html`, { waitUntil: 'networkidle', timeout: 30000 });
		await openCashuTab(page);
		await page.waitForSelector(SEL_TOKEN_INPUT, { timeout: 15000 });
		await expect(page.locator(SEL_TOKEN_INPUT)).toBeVisible();
	});

	test('portal shows lightning amount input', async ({ page }) => {
		await page.goto(`${PORTAL_BASE}/splash.html`, { waitUntil: 'networkidle', timeout: 30000 });
		await page.locator('#tab-lightning').click();
		await page.waitForSelector('#lightning-unit-amount', { timeout: 10000 });
		await expect(page.locator('#lightning-unit-amount')).toBeVisible();
	});

	test('portal shows mint selection pricing buttons', async ({ page }) => {
		await page.goto(`${PORTAL_BASE}/splash.html`, { waitUntil: 'networkidle', timeout: 30000 });
		await page.waitForSelector('.tollgate-captive-portal-method-options', { timeout: 15000 });
		const count = await page.locator('.tollgate-captive-portal-method-options button').count();
		expect(count, 'Should have at least one mint option').toBeGreaterThan(0);
	});
});

test.describe('Captive Portal — cashu e2e payment', () => {
	test.skip(async ({ request }) => {
		const event = await getApiResponse(request);
		return !event || event.kind !== 10021 || !event.tags?.some(t => t[0] === 'price_per_step');
	}, 'Skipped: tollgate service has no reachable mints (degraded mode)');

	test('cashu token payment grants access and shows checkmark', async ({ page }) => {
		const raw = mintToken(MINT_URL, MINT_AMOUNT);
		const { token, amount } = JSON.parse(raw);
		expect(token, 'mint-token should produce a non-empty token').toBeTruthy();
		expect(amount, 'minted amount should be >= 1').toBeGreaterThanOrEqual(1);

		await page.goto(`${PORTAL_BASE}/splash.html`, { waitUntil: 'networkidle', timeout: 30000 });

		// The portal is tabbed: click the Cashu tab, then fill the token input
		// (located by placeholder, the token input has no stable id).
		await fillCashuToken(page, token);

		await page.waitForSelector(SEL_SUBMIT_READY, { timeout: 10000 });

		await page.click(SEL_SUBMIT_CLICK);

		await page.waitForSelector(SEL_SUCCESS, { timeout: 35000 });
		const success = page.locator(SEL_SUCCESS);
		await expect(success).toBeVisible();

		const content = page.locator('.tollgate-captive-portal-method-content');
		const text = await content.innerText();
		expect(text, 'Should show allotment with MB or GB unit').toMatch(/\d+\s*(MB|GB|MiB|GiB)/i);
	});
});
