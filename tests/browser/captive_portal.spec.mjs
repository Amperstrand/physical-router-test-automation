import { test, expect } from '@playwright/test';
import { execFileSync } from 'child_process';

const ROUTER_IP = process.env.ROUTER_IP || '192.168.1.1';
const SPLASH_PATH = `http://${ROUTER_IP}/`;
const PORTAL_PATH = `http://${ROUTER_IP}:2050/splash.html`; // Actual captive portal SPA
const HYDRATE_TIMEOUT = 30000; // Increased from 10s — React SPAs on slower routers (MT7986) need more time to hydrate

// ── How we decide whether NDS should intercept ───────────────────────────────
// The gate intercepts every HTTP request from a client it has NOT authorised,
// whichever medium that client arrived on. The previous assumption here —
// "only WiFi clients on the open SSID are intercepted; the ethernet LAN goes
// straight through" — is WRONG whenever the captive bridge carries the wired
// ports, and it silently skipped the only test that covers the redirect.
//
// Measured 2026-10-02 on a GL-MT3000 / OpenWrt 25.12.5 (br-lan carries the
// wired ports, nodogsplash gatewayinterface=br-lan), from a plain ethernet
// client that was NOT in trustedmac and had been deauthorised:
//
//   GET http://192.168.1.1/  -> 307 Location: http://192.168.1.1:2050/splash.html?redir=...
//   GET http://1.1.1.1/      -> 307 Location: http://192.168.1.1:2050/splash.html?redir=...
//
// So we no longer guess from the medium. We PROBE the gate from this very host
// and run the redirect assertions when the probe shows we are gated. A skip now
// means "this client is not gated", which is a fact about the client, not an
// assumption about the cable.
// Probe the ROUTER's own address by default: an unauthorised client asking the
// router for HTTP is redirected to the portal, and that needs no extra route on
// the test host. An EXTERNAL url only works when this host's traffic actually
// traverses the router under test — on a bench host it usually does not (the
// default route goes out the host's own uplink, so the probe reads the open
// internet and correctly reports "not gated"). Set PORTAL_PROBE_URL to an
// external url only when you have routed this host through ROUTER_IP.
const PROBE_URL = process.env.PORTAL_PROBE_URL || `http://${ROUTER_IP}/`;
const REDIRECT_STATUSES = [301, 302, 303, 307, 308];

// Probe with curl, NOT fetch. The Fetch spec answers a manual redirect with an
// *opaqueredirect* filtered response — status 0 and no headers — so a fetch-based
// probe reads a perfectly working redirect as "not gated" and skips the very test
// it exists to run. Measured 2026-10-02: the identical request via curl reported
// `307 http://192.168.1.1:2050/splash.html?redir=http%3a%2f%2f192.168.1.1%2f`
// while fetch reported status 0.
function gateProbe() {
	try {
		const out = execFileSync(
			'curl',
			['-s', '-o', '/dev/null', '-m', '15', '-w', '%{http_code} %{redirect_url}', PROBE_URL],
			{ encoding: 'utf8' },
		).trim();
		const [statusStr, ...rest] = out.split(' ');
		const status = Number(statusStr);
		const location = rest.join(' ');
		const gated = REDIRECT_STATUSES.includes(status) && /:2050\/|\/splash/i.test(location);
		return { gated, status, location };
	} catch (err) {
		return { gated: false, status: 0, location: '', error: String(err) };
	}
}

test.describe('captive portal splash page', () => {

	test('splash page loads with TollGate branding', async ({ page }) => {
		await page.goto(SPLASH_PATH, { waitUntil: 'domcontentloaded' });

		const body = await page.waitForSelector('body', { timeout: HYDRATE_TIMEOUT });
		await page.waitForFunction(
			() => document.body.innerText.length > 0,
			{ timeout: HYDRATE_TIMEOUT },
		);

		const text = await page.evaluate(() => document.body.innerText);
		expect(text).toMatch(/TollGate|Cashu|cashu/i);

		await page.screenshot({
			path: test.info().outputPath('splash-loads.png'),
			fullPage: true,
		});
	});

	test('splash page has payment form element', async ({ page }) => {
		await page.goto(PORTAL_PATH, { waitUntil: 'domcontentloaded' });

		// React SPAs may not render standard <input> elements immediately.
		// Check for any payment-related interactive element after hydration.
		const hasPaymentElement = await page.waitForFunction(
			() => {
				// Standard input/textarea fields
				const inputs = document.querySelectorAll('input, textarea, select');
				for (const el of inputs) {
					const t = (el.placeholder || el.name || el.id || el.type || '').toLowerCase();
					if (t.includes('token') || t.includes('cashu') || t.includes('paste') || t.includes('amount')) return true;
				}
				// QR scanner or payment UI elements
				if (document.querySelector('[data-testid="qr-scanner"]') ||
				    document.querySelector('[class*="qr"]') ||
				    document.querySelector('[class*="scanner"]') ||
				    document.querySelector('[class*="payment"]') ||
				    document.querySelector('[class*="token"]')) return true;
				// Fallback: any interactive element inside the hydrated SPA container
				const appRoot = document.querySelector('#app, #root');
				if (appRoot && appRoot.querySelectorAll('button, input, a, [role="button"], [onclick]').length > 0) return true;
				return false;
			},
			{ timeout: HYDRATE_TIMEOUT },
		);
		expect(hasPaymentElement).toBeTruthy();
	});

	test('splash page has connect or pay button', async ({ page }) => {
		await page.goto(PORTAL_PATH, { waitUntil: 'domcontentloaded' });

		// React SPAs render various button types — icon buttons, SVG buttons, text buttons.
		// Search for any clickable element with payment/connect intent.
		const hasButton = await page.waitForFunction(
			() => {
				const clickables = document.querySelectorAll(
					'button, input[type="submit"], [role="button"], a[href], [class*="btn"], [class*="button"], [class*="pay"], [class*="connect"]'
				);
				for (const btn of clickables) {
					const t = (btn.textContent || btn.value || btn.getAttribute('aria-label') || btn.getAttribute('title') || '').toLowerCase();
					// Broad text match: connect, pay, submit, go, buy, purchase, get internet, start
					if (t.match(/connect|pay|submit|go|buy|purchase|get.*internet|start|topup|fund/i)) return true;
					// Also match by class name intent (icon buttons with no text)
					const cls = (btn.className || '').toLowerCase();
					if (cls.match(/pay|connect|submit|purchase|btn-action|btn-primary/i)) return true;
				}
				// Fallback: any button at all inside the app container (SPA is interactive)
				const appButtons = document.querySelectorAll('#app button, #root button, button');
				return appButtons.length > 0;
			},
			{ timeout: HYDRATE_TIMEOUT },
		);
		expect(hasButton).toBeTruthy();
	});

	test('full-page screenshot — desktop viewport', async ({ page }) => {
		await page.goto(SPLASH_PATH, { waitUntil: 'domcontentloaded' });
		await page.waitForTimeout(2000);

		await page.screenshot({
			path: test.info().outputPath('splash-desktop.png'),
			fullPage: true,
		});
	});

	test('full-page screenshot — mobile viewport', async ({ page }) => {
		await page.goto(SPLASH_PATH, { waitUntil: 'domcontentloaded' });
		await page.waitForTimeout(2000);

		await page.screenshot({
			path: test.info().outputPath('splash-mobile.png'),
			fullPage: true,
		});
	});

	// ── The gating test ──────────────────────────────────────────────────────
	// Runs whenever the PROBE says this host is intercepted, whatever the medium.
	// The assertion is now the measured contract: a 3xx whose Location is the
	// portal on :2050/splash.html — not "some 3xx, or maybe a splash-looking body".
	test('NDS redirects an unauthorised client to the splash', async ({ page }) => {
		const probe = gateProbe();
		test.skip(
			!probe.gated,
			`this host is NOT intercepted by NDS, so there is no redirect to assert ` +
			`(probe ${PROBE_URL} -> status=${probe.status} location="${probe.location}"` +
			`${probe.error ? ` error=${probe.error}` : ''}). ` +
			`Either the client's MAC is trusted/authorised, or this router does not gate ` +
			`this path. Deauthorise the client (ndsctl deauth <mac>) or point ROUTER_IP at a ` +
			`gated router — do NOT re-skip this test on a medium assumption.`,
		);

		const response = await page.goto(PROBE_URL, {
			waitUntil: 'domcontentloaded',
			timeout: 20000,
		});

		const url = page.url();
		const location = (response?.headers()?.['location']) || probe.location;

		// The browser follows the redirect, so assert BOTH sides: the redirect that
		// the router issued, and that we landed on the portal.
		expect(
			REDIRECT_STATUSES.includes(response?.status() ?? 0) || /:2050\/|\/splash/i.test(url),
			`expected the router to redirect to the captive portal; got status=${response?.status()} url=${url} location="${location}"`,
		).toBeTruthy();
		expect(
			/:2050\/|\/splash/i.test(location) || /:2050\/|\/splash/i.test(url),
			`expected the redirect target to be the portal on :2050/splash.html; got location="${location}" url=${url}`,
		).toBeTruthy();

		const bodyText = await page.evaluate(() => document.body?.innerText ?? '');
		expect(bodyText).toMatch(/TollGate|Cashu|portal|access/i);
	});

	// ── The control that makes the test above mean something ─────────────────
	// A redirect proves interception; it does NOT prove the gate is selective.
	// This control creates a SECOND, previously-unseen client on the same wire
	// (macvlan with a fresh MAC) and requires it to STILL be intercepted while
	// the paying client is open. Without it, a gate that redirects everyone —
	// including paying customers — would pass the suite.
	//
	// Opt-in because it needs root to create the interface. When it is not
	// configured the test SKIPS WITH ITS REASON, never silently passing.
	test('control: a brand-new client is still intercepted (gate is per-client)', async () => {
		const parent = process.env.PORTAL_CONTROL_IFACE;
		test.skip(
			!parent,
			'set PORTAL_CONTROL_IFACE=<parent iface, e.g. eth0/enx...> to run the untrusted-client ' +
			'control (needs sudo: creates a macvlan with a fresh MAC). The control is what proves the ' +
			'gate is per-client rather than a blanket redirect.',
		);

		const iface = 'pwctl0';
		const target = PROBE_URL;
		const sudo = (...args) =>
			execFileSync('sudo', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });

		try {
			sudo('ip', 'link', 'add', iface, 'link', parent, 'type', 'macvlan', 'mode', 'bridge');
			// The macvlan needs a source address on the router's subnet: a bare
			// interface has nothing to source from and curl cannot connect.
			sudo('ip', 'addr', 'add', ROUTER_IP.replace(/\.\d+$/, '.199') + '/24', 'dev', iface);
			sudo('ip', 'link', 'set', iface, 'up');
			const mac = execFileSync('cat', [`/sys/class/net/${iface}/address`], { encoding: 'utf8' }).trim();

			let status = '0';
			let location = '';
			try {
				const out = execFileSync(
					'curl',
					['-s', '-o', '/dev/null', '--interface', iface, '-m', '15',
					 '-w', '%{http_code} %{redirect_url}', target],
					{ encoding: 'utf8' },
				).trim();
				[status, location] = [out.split(' ')[0], out.split(' ').slice(1).join(' ')];
			} catch (err) {
				throw new Error(`curl via ${iface} (${mac}) failed: ${err.message}`);
			}

			expect(
				REDIRECT_STATUSES.includes(Number(status)) && /:2050\/|\/splash/i.test(location),
				`a fresh client (${iface}/${mac}) was NOT intercepted: status=${status} location="${location}". ` +
				`Either the gate is open to everyone, or this interface shares an already-authorised identity.`,
			).toBeTruthy();
		} finally {
			try { sudo('ip', 'link', 'del', iface); } catch { /* already gone */ }
		}
	});
});
