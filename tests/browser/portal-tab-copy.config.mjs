/**
 * Playwright config for portal tab-copy tests — mirrors the 2026-08-27
 * evidence config (portal-fixes-e2e.config.mjs) with portable paths.
 */
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: 'portal-tab-copy.spec.mjs',
  retries: 0,
  timeout: 120000,
  workers: 1,
  reporter: [['list']],
  use: {
    headless: true,
    channel: 'chrome',
    viewport: { width: 1280, height: 900 },
    screenshot: 'on',
    video: 'on',
    trace: 'on',
    ignoreHTTPSErrors: true,
    actionTimeout: 30000,
    navigationTimeout: 30000,
    launchOptions: {
      slowMo: 300,
    },
  },
  projects: [
    {
      name: 'chromium',
      use: {
        channel: 'chrome',
        video: {
          mode: 'on',
          dir: 'test-results/portal-tab-copy-videos',
        },
      },
    },
  ],
  outputDir: 'test-results/portal-tab-copy-output',
});
