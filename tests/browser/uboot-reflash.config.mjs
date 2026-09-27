// uboot-reflash.config.mjs — see uboot-reflash.spec.mjs
// System Chrome via channel:'chrome' (playwright's bundled browsers are not
// installed on this host). Headed by default: the FIRST live U-Boot run must be
// watchable, and the report should carry the HTML dump (UBOOT_DUMP=1).
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: 'uboot-reflash.spec.mjs',
  timeout: 15 * 60 * 1000, // flashing + reboot + probe
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    channel: 'chrome',
    // Headless by default: a headed launch fails outright on a host without a
    // display ("the platform failed to initialize"), which turned an intended
    // SKIP of the upload test into a browser-launch ERROR. The first live
    // U-Boot run should be watched — set UBOOT_HEADED=1 on a box with a display.
    headless: process.env.UBOOT_HEADED !== '1',
    ignoreHTTPSErrors: true,
    actionTimeout: 30000,
  },
});
