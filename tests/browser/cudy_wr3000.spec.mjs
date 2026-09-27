// Stock Cudy WR3000 (R31) — "CudyOS", a vendor-skinned LuCI — bench-prep checks.
//
// WHY THIS EXISTS
// The bench uses a Cudy WR3000 v1 as a gateway/test device. Its stock firmware is
// OpenWrt-derived but NOT OpenWrt: it serves a bespoke menu (System Status / Quick
// Setup / General Settings / Advanced Settings / Diagnostic Tools) and most stock
// LuCI admin paths do not exist on it at all. Anyone who assumes uci/LuCI paths
// wastes time, and the box's factory state explains a symptom we chased for hours:
// it hands every LAN client `gateway = dns = <itself>` (192.168.10.1), and when it
// has no working upstream it answers EVERY DNS name with its own address and
// forwards nothing — which the operator reported as "the router isn't doing NAT
// properly". These checks pin the identity, the real route map, the DHCP parameters
// it advertises, and the DNS interception, so that diagnosis is a test result
// instead of a story.
//
// SAFETY
// Read-only by default, exactly like the rest of the bench kit. The ONE mutating
// probe — attempting an OEM firmware upload to find out whether Cudy's
// signature-protection refuses a stock OpenWrt image — only runs with
// CUDY_TRY_FW_UPLOAD=1 AND CUDY_FW_IMAGE=<path>, and even then it asserts the
// firmware version is UNCHANGED afterwards (the expected outcome; the box is not
// meant to flash from the vendor UI).
//
// ENV
//   CUDY_URL           default http://192.168.10.1
//   CUDY_PASSWORD      default TOLLGATE_LUCI_PASSWORD, else the fleet lab default
//   CUDY_TRY_FW_UPLOAD =1 to run the firmware-route probe (default off)
//   CUDY_FW_IMAGE      path to the stock OpenWrt image for that probe
//
// The suite SKIPS (not fails) when no Cudy answers at CUDY_URL, so it is safe in a
// mixed bench run.

import { test, expect } from '@playwright/test';
import { Resolver } from 'node:dns/promises';

const BASE = process.env.CUDY_URL || 'http://192.168.10.1';
// No credential literal lives in this file: the suite requires the password to be
// supplied (CUDY_PASSWORD, or the shared TOLLGATE_LUCI_PASSWORD) and SKIPS with an
// explicit message when neither is set.
const PASSWORD = process.env.CUDY_PASSWORD || process.env.TOLLGATE_LUCI_PASSWORD || '';
const TRY_FW_UPLOAD = process.env.CUDY_TRY_FW_UPLOAD === '1';
const FW_IMAGE = process.env.CUDY_FW_IMAGE || '';
const BOX_HOST = new URL(BASE).hostname;

async function boxReachable() {
	try {
		const res = await fetch(`${BASE}/cgi-bin/luci/`, { signal: AbortSignal.timeout(4000) });
		return res.status > 0;
	} catch {
		return false;
	}
}

async function login(page) {
	// CudyOS serves the login form at /cgi-bin/luci/ with a 403 status and a hidden
	// luci_username=admin; the only field a human fills is the password.
	await page.goto(`${BASE}/cgi-bin/luci/`, { waitUntil: 'domcontentloaded' });
	const pw = await page.waitForSelector('input[type=password]', { timeout: 15000 });
	expect(pw, 'the Cudy login form must offer a password field').toBeTruthy();
	await pw.fill(PASSWORD);
	await page.keyboard.press('Enter');
	await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
	await page.waitForTimeout(1500);
	const stillLogin = await page.$('input[type=password]');
	expect(stillLogin, `login refused at ${BASE} — wrong CUDY_PASSWORD?`).toBeNull();
}

async function bodyText(page) {
	return ((await page.textContent('body').catch(() => '')) || '').replace(/\s+/g, ' ').trim();
}

test.describe('stock Cudy WR3000 (CudyOS/LuCI)', () => {
	test.beforeAll(async () => {
		test.skip(!PASSWORD, 'set CUDY_PASSWORD (or TOLLGATE_LUCI_PASSWORD) — this suite must not carry a credential literal');
		test.skip(!(await boxReachable()), `no Cudy answering at ${BASE} — set CUDY_URL`);
	});

	test.beforeEach(async ({ page }) => {
		await login(page);
	});

	test('identity: the box is a Cudy WR3000 running the vendor firmware', async ({ page }) => {
		await page.goto(`${BASE}/cgi-bin/luci/admin/system/system`, { waitUntil: 'domcontentloaded' });
		const text = await bodyText(page);
		expect(text, 'model line').toMatch(/Model\s*WR3000/);
		expect(text, 'hardware revision').toMatch(/WR3000 V1\.0/);
		expect(text, 'firmware version').toMatch(/Firmware Version\s*2\.\d+\.\d+/);
		expect(text, 'uptime').toMatch(/Uptime/i);
		await test.info().attach('cudy-identity.txt', { body: text.slice(0, 2000), contentType: 'text/plain' });
	});

	test('route map: vendor menu exists and stock LuCI admin paths do NOT', async ({ page }) => {
		const menu = await page.$$eval('a[href]', (els) =>
			els.map((e) => ({ text: (e.textContent || '').trim(), href: e.getAttribute('href') }))
		);
		const hrefs = menu.map((m) => m.href);
		for (const path of [
			'/cgi-bin/luci/admin/wizard',   // Quick Setup
			'/cgi-bin/luci/admin/setup',    // General Settings
			'/cgi-bin/luci/admin/panel',    // Advanced Settings
			'/cgi-bin/luci/admin/tools',    // Diagnostic Tools
		]) {
			expect(hrefs, `CudyOS menu must expose ${path}`).toContain(path);
		}
		// A stock-LuCI path that does not exist here: documents that this is a vendor
		// skin, not OpenWrt-with-LuCI (so uci-oriented runbooks do not apply).
		await page.goto(`${BASE}/cgi-bin/luci/admin/status/overview`, { waitUntil: 'domcontentloaded' });
		expect(await bodyText(page)).toMatch(/No page is registered/i);
	});

	test('DHCP: the box advertises itself as gateway and DNS to every LAN client', async ({ page }) => {
		await page.goto(`${BASE}/cgi-bin/luci/admin/services`, { waitUntil: 'domcontentloaded' });
		const text = await bodyText(page);
		expect(text, 'DHCP server section').toMatch(/DHCP Server/i);
		expect(text, 'a lease range').toMatch(/IP Start\s*\d+\.\d+\.\d+\.\d+/);
		expect(text, 'gateway it hands out').toMatch(new RegExp(`Default Gateway\\s*${BOX_HOST.replace(/\./g, '\\.')}`));
		expect(text, 'DNS it hands out').toMatch(new RegExp(`Preferred DNS\\s*${BOX_HOST.replace(/\./g, '\\.')}`));
		await test.info().attach('cudy-dhcp.txt', { body: text.slice(0, 2000), contentType: 'text/plain' });
	});

	test('DNS interception: it answers names it cannot know with its own address', async () => {
		// This is the mechanism behind "the router isn't doing NAT": a client's every
		// lookup lands on the router, which cannot forward it. Queried directly so the
		// test host's own resolver is not in the path.
		const resolver = new Resolver({ timeout: 4000, tries: 1 });
		resolver.setServers([BOX_HOST]);
		const answers = await resolver.resolve4('name-that-cannot-exist.invalid').catch((e) => ({ error: String(e) }));
		expect(answers, 'an unresolvable name must still be answered (interception)').not.toHaveProperty('error');
		expect(answers, 'the answer must be the box itself').toContain(BOX_HOST);
	});

	test('firmware route (opt-in): the vendor UI does not install a stock OpenWrt image', async ({ page }) => {
		test.skip(!TRY_FW_UPLOAD, 'set CUDY_TRY_FW_UPLOAD=1 and CUDY_FW_IMAGE=<stock image> to probe the OEM firmware route');
		test.skip(!FW_IMAGE, 'CUDY_FW_IMAGE must point at a stock OpenWrt image');

		const before = await (async () => {
			await page.goto(`${BASE}/cgi-bin/luci/admin/system/system`, { waitUntil: 'domcontentloaded' });
			return (await bodyText(page)).match(/Firmware Version\s*([0-9.\-]+)/)?.[1] || 'unknown';
		})();

		await page.goto(`${BASE}/cgi-bin/luci/admin/setup`, { waitUntil: 'domcontentloaded' });
		const fwLink = await page.$('a[href*="active=autoupgrade"]');
		expect(fwLink, 'the General Settings page must link the Firmware section').toBeTruthy();
		await fwLink.click();
		await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
		await page.waitForTimeout(1200);

		const fileInput = await page.$('input[type=file]');
		expect(fileInput, 'the Firmware section must expose a file input').toBeTruthy();
		await fileInput.setInputFiles(FW_IMAGE);
		const submit = await page.$('input[type=submit], button[type=submit], button:has-text("Upgrade"), button:has-text("Flash")');
		if (submit) await submit.click();
		await page.waitForTimeout(8000);

		const message = await bodyText(page);
		await test.info().attach('cudy-firmware-route.txt', { body: message.slice(0, 3000), contentType: 'text/plain' });

		await page.goto(`${BASE}/cgi-bin/luci/admin/system/system`, { waitUntil: 'domcontentloaded' });
		const after = (await bodyText(page)).match(/Firmware Version\s*([0-9.\-]+)/)?.[1] || 'unknown';
		// The expected and documented outcome: Cudy signs its images and ships no
		// signature-disable build, so the vendor route must NOT accept stock OpenWrt.
		// Asserting on the version (not on a vendor-specific message) keeps this
		// meaningful whichever refusal wording the firmware uses.
		expect(after, 'the OEM route must not have flashed the box').toBe(before);
		expect(after, 'still the vendor firmware').toMatch(/^2\./);
	});
});
