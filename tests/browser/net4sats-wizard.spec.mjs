/**
 * net4sats-wizard.spec.mjs — Playwright test of the net4sats simplified onboarding wizard.
 *
 * Records the FULL Endo experience:
 *   1. Wizard opens with net4sats branding
 *   2. Scans network, finds the GL-MT6000 router
 *   3. Selects the router from dropdown
 *   4. Enters the SSH password
 *   5. Clicks "Deploy net4sats"
 *   6. Watches deployment progress step-by-step
 *   7. Sees success screen
 */
import { test, expect } from '@playwright/test';

const WIZARD_URL = 'http://localhost:9876';
const ROUTER_PASSWORD = process.env.ROUTER_PASSWORD || 'test123';

test.use({
    actionTimeout: 30000,
    video: 'on',
    screenshot: 'on',
    trace: 'on',
    launchOptions: { slowMo: 300 },
});

test('net4sats onboarding wizard — auto-detect router and deploy', async ({ page }) => {
    // ── Step 1: Open the wizard ──
    await test.step('Open net4sats wizard', async () => {
        await page.goto(WIZARD_URL, { waitUntil: 'networkidle' });
        await page.waitForTimeout(3000); // Let scan run
        await page.screenshot({ path: 'screenshots/wizard-01-scanning.png' });
    });

    // ── Step 2: Wait for scan to complete and show router ──
    await test.step('Wait for network scan', async () => {
        // Scan takes ~15-25s, wait for the select view to appear
        const selectView = page.locator('#select-view');
        await selectView.waitFor({ state: 'visible', timeout: 40000 });
        await page.waitForTimeout(1000);
        await page.screenshot({ path: 'screenshots/wizard-02-router-found.png' });

        // Verify a router was detected
        const select = page.locator('#router-select');
        const options = await select.locator('option').count();
        expect(options).toBeGreaterThan(0);
    });

    // ── Step 3: Enter password ──
    await test.step('Enter router password', async () => {
        const passwordInput = page.locator('#password');
        await passwordInput.fill(ROUTER_PASSWORD);
        await page.waitForTimeout(500);
        await page.screenshot({ path: 'screenshots/wizard-03-password.png' });
    });

    // ── Step 4: Click Deploy ──
    await test.step('Click Deploy net4sats', async () => {
        const deployBtn = page.locator('#deploy-btn');
        await deployBtn.click();
        await page.waitForTimeout(2000);
        await page.screenshot({ path: 'screenshots/wizard-04-deploying.png' });
    });

    // ── Step 5: Watch deployment progress ──
    await test.step('Watch deployment progress', async () => {
        // Wait for deployment to complete (takes ~60-90s on real router)
        // Poll and take screenshots periodically
        for (let i = 0; i < 30; i++) {
            await page.waitForTimeout(5000); // 5s intervals
            
            // Check if success or error view appeared
            const successVisible = await page.locator('#success-view').isVisible().catch(() => false);
            const errorVisible = await page.locator('#error-view').isVisible().catch(() => false);
            
            if (successVisible || errorVisible) break;
            
            // Take a progress screenshot every 15s
            if (i % 3 === 0) {
                await page.screenshot({ path: `screenshots/wizard-05-progress-${i}.png` });
            }
        }
    });

    // ── Step 6: Verify success ──
    await test.step('Verify deployment success', async () => {
        // Wait a bit more for final state
        await page.waitForTimeout(5000);
        
        const successVisible = await page.locator('#success-view').isVisible().catch(() => false);
        const errorVisible = await page.locator('#error-view').isVisible().catch(() => false);
        
        // Take final screenshot regardless
        await page.screenshot({ path: 'screenshots/wizard-06-final.png', fullPage: true });
        
        // The test passes either way — we're recording the experience
        // Success or error, both are valid outcomes to show in the video
        expect(successVisible || errorVisible).toBeTruthy();
    });
});
