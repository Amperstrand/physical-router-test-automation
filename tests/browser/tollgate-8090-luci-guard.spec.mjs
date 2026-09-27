/**
 * tollgate-8090-luci-guard.spec.mjs
 *
 * Guards the regression: the TollGate admin board lives on :8090 and LuCI must
 * NEVER be reachable there. Historically uhttpd.admin.home=/www made
 * uhttpd's default cgi_prefix resolve :8090/cgi-bin/luci to the real LuCI CGI,
 * so http://<router>:8090/ served LuCI instead of the board.
 *
 * Markers (verified on hardware):
 *   board  -> <title>TollGate Admin</title>, no /luci-static/
 *   LuCI   -> /luci-static/ assets, <title>TollGate | Overview</title>
 *
 * Asserts, over the URL shape a user types (http://tollgate.lan:8090):
 *   1. :8090/             -> board (never LuCI)
 *   2. :8090/cgi-bin/luci -> board (the LuCI CGI is not wired up)
 *   3. :8080/cgi-bin/luci -> LuCI (we did not break LuCI)
 *   4. the board's login form renders over that URL
 *
 *   ROUTER_IP=192.168.1.1 npx playwright test \
 *     --config tests/browser/tollgate-8090-luci-guard.config.mjs
 */
import { test, expect, request } from '@playwright/test';

const ROUTER_IP = process.env.ROUTER_IP || '192.168.1.1';
const ADMIN_URL = process.env.ADMIN_URL || `http://${ROUTER_IP}:8090/`;
const LUCI_URL = (process.env.LUCI_URL || `http://${ROUTER_IP}:8080/`).replace(/\/$/, '');
const ADMIN_ORIGIN = new URL(ADMIN_URL).origin;

const BOARD_TITLE = /<title>\s*TollGate Admin\s*<\/title>/i;
const LUCI_ASSET = /\/luci-static\//;

async function get(ctx, url) {
	const resp = await ctx.get(url, { timeout: 20000 });
	return { status: resp.status(), body: await resp.text() };
}

test.describe(`:8090 LuCI guard (${ADMIN_ORIGIN})`, () => {
	let ctx;
	test.beforeAll(async () => {
		ctx = await request.newContext({ ignoreHTTPSErrors: true });
	});
	test.afterAll(async () => { await ctx?.dispose(); });

	test('http://…:8090/ serves the TollGate board, not LuCI', async () => {
		const { status, body } = await get(ctx, `${ADMIN_ORIGIN}/`);
		expect(status, `GET ${ADMIN_ORIGIN}/ status`).toBe(200);
		expect(body, `board marker at ${ADMIN_ORIGIN}/`).toMatch(BOARD_TITLE);
		expect(body, `LuCI assets must not be served on :8090`).not.toMatch(LUCI_ASSET);
	});

	test('http://…:8090/cgi-bin/luci serves the board, not the LuCI CGI', async () => {
		const { status, body } = await get(ctx, `${ADMIN_ORIGIN}/cgi-bin/luci`);
		expect(status, `GET ${ADMIN_ORIGIN}/cgi-bin/luci status`).toBe(200);
		expect(body, `board marker at /cgi-bin/luci`).toMatch(BOARD_TITLE);
		expect(body, `LuCI assets must not be served on :8090`).not.toMatch(LUCI_ASSET);
	});

	test('LuCI still lives on the main uhttpd (:8080)', async () => {
		const { body } = await get(ctx, `${LUCI_URL}/cgi-bin/luci`);
		expect(body, `LuCI assets at ${LUCI_URL}/cgi-bin/luci`).toMatch(LUCI_ASSET);
	});

	test('the board login form renders on the user-facing URL', async ({ page }) => {
		await page.goto(ADMIN_URL, { waitUntil: 'domcontentloaded', timeout: 30000 });
		await expect(page).toHaveTitle(/TollGate Admin/i);
		await expect(page.locator('input#password')).toBeVisible({ timeout: 20000 });
		await page.screenshot({ path: 'test-results/tollgate-8090-luci-guard.png', fullPage: true });
	});
});
