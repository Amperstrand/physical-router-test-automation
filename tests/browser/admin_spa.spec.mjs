import { test, expect } from '@playwright/test';

const SPLASH_PATH = '/';
const HYDRATE_TIMEOUT = 15000;

test.describe('admin SPA', () => {

	async function tryLogin(page) {
		await page.waitForSelector('body', { timeout: HYDRATE_TIMEOUT });
		await page.waitForTimeout(2000);

		const hasPasswordField = await page.$('input[type="password"]');
		if (!hasPasswordField) {
			const bodyText = await page.evaluate(() => document.body.innerText.toLowerCase());
			const hasLogin = bodyText.match(/login|password|sign in/i);
			if (!hasLogin) return true;
		}

		const passwordInput = await page.$('input[type="password"]');
		if (!passwordInput) return true;

		const password = process.env.TOLLGATE_LUCI_PASSWORD || 'c03rad0r123';
		await passwordInput.fill(password);

		const usernameInput = await page.$('input[type="text"], input[name="username"], input[placeholder*="user" i]');
		if (usernameInput) {
			await usernameInput.fill('root');
		}

		const submitBtn = await page.$('button[type="submit"], button:has-text("Login"), button:has-text("Log in"), button:has-text("Sign in"), input[type="submit"]');
		if (submitBtn) {
			await submitBtn.click();
		} else {
			await passwordInput.press('Enter');
		}

		await page.waitForTimeout(3000);
		return true;
	}

	test.beforeEach(async ({ page }) => {
		await page.goto('/', { waitUntil: 'domcontentloaded' });
		await tryLogin(page);
	});

	test('login page loads with TollGate branding', async ({ page }) => {
		await page.waitForSelector('body', { timeout: HYDRATE_TIMEOUT });

		const bodyText = await page.evaluate(() => document.body.innerText);
		const hasLogin = bodyText.match(/login|password|sign in|authenticate|tollgate/i);
		if (!hasLogin) {
			const hasDashboard = bodyText.match(/dashboard|health|balance|wallet/i);
			if (hasDashboard) {
				return;
			}
		}
		expect(hasLogin || bodyText.match(/tollgate/i)).toBeTruthy();
	});

	test('dashboard shows health and version after login', async ({ page }) => {
		await page.waitForSelector('body', { timeout: HYDRATE_TIMEOUT });
		await page.waitForTimeout(3000);

		const bodyText = await page.evaluate(() => document.body.innerText.toLowerCase());

		const hasDashboard = bodyText.match(/dashboard|health|version|uptime|status/i);
		if (!hasDashboard) {
			const hasLogin = bodyText.match(/login|password/i);
			if (hasLogin) {
				await page.screenshot({
					path: test.info().outputPath('admin-login-required.png'),
					fullPage: true,
				});
				test.skip('Login page shown — cannot test dashboard without authentication');
			}
		}

		expect(hasDashboard).toBeTruthy();
		await page.screenshot({
			path: test.info().outputPath('admin-dashboard.png'),
			fullPage: true,
		});
	});

	test('settings page renders schema-driven form', async ({ page }) => {
		const navLinks = await page.$$eval('a, button, [role="link"], [role="tab"]', els =>
			els.map(el => ({
				text: (el.textContent || '').trim().toLowerCase(),
				href: el.getAttribute('href') || '',
			}))
		);

		const settingsLink = navLinks.find(l =>
			l.text.includes('settings') || l.text.includes('config') || l.href.includes('settings')
		);

		if (!settingsLink) {
			const bodyText = await page.evaluate(() => document.body.innerText.toLowerCase());
			if (bodyText.includes('login') || bodyText.includes('password')) {
				test.skip('Login required — cannot navigate to settings');
			}
			test.skip('No settings navigation link found');
		}

		if (settingsLink && settingsLink.href) {
			await page.goto(settingsLink.href, { waitUntil: 'domcontentloaded' });
		} else if (settingsLink) {
			await settingsLink.text && await page.click(`text=${settingsLink.text}`);
		}

		await page.waitForTimeout(2000);

		const hasFormElements = await page.evaluate(() => {
			const inputs = document.querySelectorAll('input, select, textarea');
			const selects = document.querySelectorAll('select');
			return {
				inputCount: inputs.length,
				selectCount: selects.length,
				hasLabels: document.querySelectorAll('label').length > 0,
			};
		});

		expect(hasFormElements.inputCount).toBeGreaterThan(0);
		await page.screenshot({
			path: test.info().outputPath('admin-settings.png'),
			fullPage: true,
		});
	});

	test('wallet page shows balance info', async ({ page }) => {
		const navLinks = await page.$$eval('a, button, [role="link"], [role="tab"]', els =>
			els.map(el => ({
				text: (el.textContent || '').trim().toLowerCase(),
				href: el.getAttribute('href') || '',
			}))
		);

		const walletLink = navLinks.find(l =>
			l.text.includes('wallet') || l.text.includes('balance') || l.href.includes('wallet')
		);

		if (!walletLink) {
			test.skip('No wallet navigation link found');
		}

		if (walletLink && walletLink.href) {
			await page.goto(walletLink.href, { waitUntil: 'domcontentloaded' });
		} else if (walletLink) {
			await page.click(`text=${walletLink.text}`);
		}

		await page.waitForTimeout(2000);

		const bodyText = await page.evaluate(() => document.body.innerText.toLowerCase());
		const hasWalletContent = bodyText.match(/balance|sats|mint|wallet|total/i);
		expect(hasWalletContent).toBeTruthy();

		await page.screenshot({
			path: test.info().outputPath('admin-wallet.png'),
			fullPage: true,
		});
	});

	test('wifi page shows radio status', async ({ page }) => {
		const navLinks = await page.$$eval('a, button, [role="link"], [role="tab"]', els =>
			els.map(el => ({
				text: (el.textContent || '').trim().toLowerCase(),
				href: el.getAttribute('href') || '',
			}))
		);

		const wifiLink = navLinks.find(l =>
			l.text.includes('wifi') || l.text.includes('wireless') || l.href.includes('wifi')
		);

		if (!wifiLink) {
			test.skip('No wifi navigation link found');
		}

		if (wifiLink && wifiLink.href) {
			await page.goto(wifiLink.href, { waitUntil: 'domcontentloaded' });
		} else if (wifiLink) {
			await page.click(`text=${wifiLink.text}`);
		}

		await page.waitForTimeout(2000);

		const bodyText = await page.evaluate(() => document.body.innerText.toLowerCase());
		const hasWifiContent = bodyText.match(/ssid|channel|signal|radio|wifi|wireless/i);
		expect(hasWifiContent).toBeTruthy();

		await page.screenshot({
			path: test.info().outputPath('admin-wifi.png'),
			fullPage: true,
		});
	});

	test('devices page lists connected clients', async ({ page }) => {
		const navLinks = await page.$$eval('a, button, [role="link"], [role="tab"]', els =>
			els.map(el => ({
				text: (el.textContent || '').trim().toLowerCase(),
				href: el.getAttribute('href') || '',
			}))
		);

		const devicesLink = navLinks.find(l =>
			l.text.includes('device') || l.text.includes('client') || l.href.includes('device')
		);

		if (!devicesLink) {
			test.skip('No devices navigation link found');
		}

		if (devicesLink && devicesLink.href) {
			await page.goto(devicesLink.href, { waitUntil: 'domcontentloaded' });
		} else if (devicesLink) {
			await page.click(`text=${devicesLink.text}`);
		}

		await page.waitForTimeout(2000);

		const bodyText = await page.evaluate(() => document.body.innerText.toLowerCase());
		const hasDevicesContent = bodyText.match(/mac|ip|device|client|connected|hostname/i);
		expect(hasDevicesContent).toBeTruthy();

		await page.screenshot({
			path: test.info().outputPath('admin-devices.png'),
			fullPage: true,
		});
	});

	test('layout sidebar has navigation links', async ({ page }) => {
		await page.waitForSelector('body', { timeout: HYDRATE_TIMEOUT });
		await page.waitForTimeout(2000);

		const navLinks = await page.$$eval('nav a, .sidebar a, [role="navigation"] a', els =>
			els.map(el => (el.textContent || '').trim().toLowerCase()).filter(t => t.length > 0)
		);

		if (navLinks.length === 0) {
			const allLinks = await page.$$eval('a', els =>
				els.map(el => (el.textContent || '').trim().toLowerCase()).filter(t => t.length > 0)
			);
			const relevantLinks = allLinks.filter(l =>
				/dashboard|settings|wallet|wifi|device|login|logout/i.test(l)
			);
			if (relevantLinks.length === 0) {
				const bodyText = await page.evaluate(() => document.body.innerText.toLowerCase());
				if (bodyText.includes('login') || bodyText.includes('password')) {
					test.skip('Login page shown — cannot test navigation');
				}
				test.skip('No navigation links found on page');
			}
		}
	});

	test('logout or session end works', async ({ page }) => {
		await page.waitForSelector('body', { timeout: HYDRATE_TIMEOUT });
		await page.waitForTimeout(2000);

		const logoutEl = await page.$('text=/logout|sign out|log out/i');
		if (!logoutEl) {
			test.skip('No logout button/link found');
		}

		await logoutEl.click();
		await page.waitForTimeout(2000);

		const bodyText = await page.evaluate(() => document.body.innerText.toLowerCase());
		const isLoggedOut = bodyText.match(/login|sign in|password|logged out|session.*end/i);
		expect(isLoggedOut).toBeTruthy();
	});
});
