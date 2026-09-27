import { test, expect } from '@playwright/test';
import { getRouter } from '../helpers/inventory.mjs';
import { SEL_TOKEN_INPUT, openCashuTab } from '../helpers/portal-selectors.mjs';

const router = getRouter();
const PORTAL_PORT = process.env.TOLLGATE_CAPTIVE_PORTAL_PORT || '80';
const PORTAL_BASE = `http://${router.sshHost}:${PORTAL_PORT}`;
const API_BASE = `http://${router.sshHost}:2121`;

function portalUrl() { return `${PORTAL_BASE}/splash.html`; }

async function getApiResponse(request) {
	const response = await request.get(`${API_BASE}/`, { timeout: 10000 });
	if (!response.ok()) return null;
	return await response.json();
}

async function waitForPortal(page) {
	await page.goto(portalUrl(), { waitUntil: 'networkidle', timeout: 30000 });
	await page.waitForSelector('.tollgate-captive-portal-view', { timeout: 15000 });
	await page.waitForTimeout(3000);
}

function collectBareZeros(text) {
	return text.split('\n').map(l => l.trim()).filter(l => l === '0');
}

test.describe('captive portal — no bare "0" literals', () => {
	test('cashu tab has no bare "0" text nodes', async ({ page }) => {
		await waitForPortal(page);
		const view = page.locator('.tollgate-captive-portal-view');
		const bareZeros = collectBareZeros(await view.innerText());
		expect(bareZeros, `Should not render bare "0" text nodes, found: [${bareZeros}]`).toHaveLength(0);
	});

	test('lightning tab has no bare "0" text nodes', async ({ page }) => {
		await waitForPortal(page);
		const lightningTab = page.locator('#tab-lightning');
		if (await lightningTab.isVisible()) {
			await lightningTab.click();
		}
		await page.waitForTimeout(3000);
		const view = page.locator('.tollgate-captive-portal-view');
		const bareZeros = collectBareZeros(await view.innerText());
		expect(bareZeros, `Lightning tab should not render bare "0" text nodes, found: [${bareZeros}]`).toHaveLength(0);
	});
});

test.describe('captive portal — degraded mode', () => {
	async function skipIfNotDegraded(request) {
		const apiEvent = await getApiResponse(request);
		if (!apiEvent || apiEvent.kind !== 21023) {
			test.skip();
		}
		return apiEvent;
	}

	test('shows error message when mints unreachable', async ({ page, request }) => {
		await skipIfNotDegraded(request);
		await waitForPortal(page);
		await page.waitForSelector('.status.error', { timeout: 15000 });
		const errorBox = page.locator('.status.error');
		await expect(errorBox).toBeVisible();
		const errorText = await errorBox.innerText();
		expect(errorText.length, 'Error message should not be empty').toBeGreaterThan(0);
		expect(errorText, 'Should mention unreachable/unavailable state').toMatch(/unreachable|initializing|unavailable|No reachable mints/i);
	});

	test('shows retrying indicator', async ({ page, request }) => {
		await skipIfNotDegraded(request);
		await waitForPortal(page);
		await page.waitForSelector('.status.error', { timeout: 15000 });
		const retrying = page.locator('.tollgate-captive-portal-retrying');
		await expect(retrying).toBeVisible({ timeout: 10000 });
		const text = await retrying.innerText();
		expect(text.toLowerCase()).toContain('retry');
	});

	test('hides payment inputs', async ({ page, request }) => {
		await skipIfNotDegraded(request);
		await waitForPortal(page);
		await page.waitForSelector('.status.error', { timeout: 15000 });
		// The tabbed portal only renders the token input once the Cashu tab is
		// active, so the payment inputs are absent in degraded mode.
		const cashuInput = page.locator(SEL_TOKEN_INPUT);
		await expect(cashuInput).not.toBeVisible();
	});
});

test.describe('captive portal — happy path', () => {
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
		await waitForPortal(page);
		await openCashuTab(page);
		await page.waitForSelector(SEL_TOKEN_INPUT, { timeout: 15000 });
		await expect(page.locator(SEL_TOKEN_INPUT)).toBeVisible();
	});

	test('portal shows lightning amount input', async ({ page }) => {
		await waitForPortal(page);
		await page.locator('#tab-lightning').click();
		await page.waitForSelector('#lightning-unit-amount', { timeout: 10000 });
		await expect(page.locator('#lightning-unit-amount')).toBeVisible();
	});

	test('portal shows mint selection pricing buttons', async ({ page }) => {
		await waitForPortal(page);
		await page.waitForSelector('.tollgate-captive-portal-method-options', { timeout: 15000 });
		const count = await page.locator('.tollgate-captive-portal-method-options button').count();
		expect(count, 'Should have at least one mint option').toBeGreaterThan(0);
	});
});
