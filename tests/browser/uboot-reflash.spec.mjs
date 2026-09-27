/**
 * uboot-reflash.spec.mjs — flash a GL.iNet router from its U-Boot web recovery
 * page, driven by a browser, with a mode preflight so it can never fire against
 * a router that is merely running OpenWrt.
 *
 * WHY: the operator (c08r4d0r) can put a GL.iNet box into U-Boot mode on demand.
 * That makes U-Boot the recovery path for hardware we hold NO credentials for —
 * e.g. the upstream hop of the lab rig, whose root password is unknown and whose
 * dropbear refuses every password and key we own. Reflash from U-Boot, and the
 * fresh mainline image comes up with an EMPTY root password, which the kit's
 * 01b-postflash-bootstrap.sh then sets to the shared lab password. Credential
 * lockout stops being a dead end.
 *
 * SAFETY MODEL — read before running:
 *   * DESTRUCTIVE. It wipes the device's overlay. It refuses to run unless
 *     REFLASH_CONFIRM=I_KNOW_THIS_WIPES_THE_ROUTER is set.
 *   * MODE PREFLIGHT IS MANDATORY. Test 1 classifies the device and test 2
 *     SKIPS unless U-Boot mode was positively detected. A running OpenWrt also
 *     answers on 192.168.1.1, so "port 80 is open" is NOT mode evidence.
 *   * IMAGE IS SHA256-GATED. UBQOT_IMAGE_SHA256 or a sidecar <image>.sha256 must
 *     match, or nothing is uploaded.
 *   * The image must be a factory/sysupgrade image for THIS board. Check
 *     `cat /tmp/sysinfo/board_name` on the target BEFORE taking it down, and see
 *     the physical-router-testing skill (staging stock vendor -> mainline).
 *
 * STATUS OF THE U-BOOT BRANCH: UNVERIFIED AGAINST A LIVE U-BOOT PAGE.
 * The mode-detection and upload selectors were written from the GL.iNet
 * U-Boot web-failsafe layout and from the kit's MT3000 notes; they have NOT been
 * exercised against a box actually sitting in U-Boot. The first real run must be
 * headed with UBOOT_DUMP=1 so the page HTML is dumped and the selectors pinned.
 * Do not report this as a working flasher until that run has happened.
 *
 * Run (only when the operator confirms the box is in U-Boot mode):
 *   UBOOT_IMAGE=~/img/openwrt-25.12.5-mediatek-filogic-glinet_gl-mt6000-squashfs-sysupgrade.bin \
 *   UBOOT_IMAGE_SHA256=<sha256> \
 *   REFLASH_CONFIRM=I_KNOW_THIS_WIPES_THE_ROUTER \
 *   UBOOT_DUMP=1 \
 *   npx playwright test --config=tests/browser/uboot-reflash.config.mjs --headed
 */
import { test, expect } from '@playwright/test';
import { createHash } from 'node:crypto';
import { readFileSync, existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';

const UBOOT_URL = process.env.UBOOT_URL || 'http://192.168.1.1/';
const IMAGE = process.env.UBOOT_IMAGE || '';
const IMAGE_SHA = (process.env.UBOOT_IMAGE_SHA256 || '').toLowerCase().trim();
const CONFIRMED = process.env.REFLASH_CONFIRM === 'I_KNOW_THIS_WIPES_THE_ROUTER';
const DUMP = process.env.UBOOT_DUMP === '1';
const POST_FLASH_HOST = process.env.POST_FLASH_HOST || '192.168.1.1';
const POST_FLASH_WAIT_MS = Number(process.env.POST_FLASH_WAIT_MS || 8 * 60 * 1000);

// Evidence collected across tests; printed as one block in the last test so a
// reviewer sees the whole chain in the report rather than scattered lines.
const evidence = [];
const note = (s) => { evidence.push(s); console.log(`   [EVIDENCE] ${s}`); };

function sha256(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex');
}

/** fetch() the device and classify which firmware/BL is answering. */
async function classifyDevice(requestApi) {
  const res = await requestApi.get(UBOOT_URL, { timeout: 15000, failOnStatusCode: false });
  const body = await res.text().catch(() => '');
  const lower = body.toLowerCase();
  const hasFileInput = /<input[^>]+type=["']?file/i.test(body);
  const looksUboot = /u-?boot/.test(lower) || /failsafe|recovery|firmware/.test(lower);
  // Careful: mainline filogic bootloaders are literally labelled "OpenWrt U-Boot",
  // so the bare word openwrt must NOT be treated as evidence of a running system.
  // Only LuCI/TollGate plumbing counts.
  const looksOpenwrt = /cgi-bin\/luci|luci-static|\bL\.env\b|tollgate|net4sats/.test(lower);
  // A page with a file-upload form is the bootloader, whatever words it uses.
  const inUboot = hasFileInput || (looksUboot && !looksOpenwrt);
  return { status: res.status(), body, hasFileInput, looksUboot, looksOpenwrt, inUboot, len: body.length };
}

test.describe.configure({ mode: 'serial' });

test('1. preflight — device is POSITIVELY in U-Boot recovery mode', async ({ request }) => {
  const d = await classifyDevice(request);
  note(`GET ${UBOOT_URL} -> HTTP ${d.status}, ${d.len} bytes, file-input=${d.hasFileInput}, uboot-markers=${d.looksUboot}, openwrt-plumbing=${d.looksOpenwrt}, inUboot=${d.inUboot}`);

  if (DUMP) {
    console.log('   [DUMP] first 1200 chars of the page:\n' + d.body.slice(0, 1200));
  }

  // A running OpenWrt also answers here: that is the dangerous false positive.
  expect(
    d.looksOpenwrt && !d.hasFileInput,
    `Device at ${UBOOT_URL} is answering like a RUNNING OpenWrt/LuCI — it is NOT in U-Boot mode. ` +
      `Power the router down, hold RESET, apply power, keep holding ~8s until the LED blinks, ` +
      `set this host to a 192.168.1.2/24 address, then re-run.`
  ).toBe(false);

  expect(
    d.inUboot,
    `Device at ${UBOOT_URL} does not look like a U-Boot web-failsafe page (no file input, no U-Boot markers). ` +
      `If the box IS in U-Boot mode, run once with UBOOT_DUMP=1 and pin the selectors from the dump.`
  ).toBe(true);

  expect(CONFIRMED, 'Refusing to continue: set REFLASH_CONFIRM=I_KNOW_THIS_WIPES_THE_ROUTER').toBe(true);
});

test('2. flash the image from the U-Boot page and watch the device come back', async ({ page }) => {
  test.skip(!CONFIRMED, 'REFLASH_CONFIRM not set — dry preflight only (this is a skip, NOT a pass)');
  test.skip(!IMAGE, 'UBOOT_IMAGE not set — nothing to upload');

  // ---- image integrity, before anything is sent to the bootloader
  expect(existsSync(IMAGE), `UBOOT_IMAGE does not exist: ${IMAGE}`).toBe(true);
  const localSha = sha256(IMAGE);
  let expected = IMAGE_SHA;
  if (!expected && existsSync(`${IMAGE}.sha256`)) {
    expected = readFileSync(`${IMAGE}.sha256`, 'utf8').trim().split(/\s+/)[0].toLowerCase();
  }
  expect(expected, 'No UBQOT_IMAGE_SHA256 and no <image>.sha256 sidecar — refusing an unverified flash').toBeTruthy();
  expect(localSha, 'image sha256 mismatch — refusing to flash').toBe(expected);
  note(`image ${IMAGE} sha256=${localSha} (matches the expected digest)`);

  // ---- upload
  await page.goto(UBOOT_URL, { waitUntil: 'domcontentloaded', timeout: 20000 });
  const fileInput = page.locator('input[type="file"]').first();
  await expect(fileInput, 'U-Boot page has no <input type=file> — pin the selector from UBOOT_DUMP=1').toBeVisible({ timeout: 15000 });
  await fileInput.setInputFiles(IMAGE);
  note(`uploaded ${IMAGE} to the bootloader form`);

  const submit = page
    .locator('input[type="submit"], button[type="submit"], button, input[value*="Update" i], input[value*="Flash" i], input[value*="Upgrade" i]')
    .first();
  await submit.click();
  note('submitted the flash form; waiting for the bootloader to report progress/completion');

  // ---- the bootloader either prints progress and reboots, or just dies mid-flight.
  //      Either way the real success signal is the DEVICE COMING BACK.
  await page.waitForTimeout(5000);
  const after = (await page.content().catch(() => '')).slice(0, 400);
  note(`page after submit: ${after.replace(/\s+/g, ' ').slice(0, 200)}`);

  // ---- poll until the flashed firmware answers again
  const deadline = Date.now() + POST_FLASH_WAIT_MS;
  let back = false;
  let board = '';
  while (Date.now() < deadline) {
    try {
      const out = execFileSync(
        'sshpass',
        ['-p', '', 'ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
         '-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=password,keyboard-interactive',
         '-o', 'ConnectTimeout=5', `root@${POST_FLASH_HOST}`,
         'cat /tmp/sysinfo/board_name; . /etc/openwrt_release; echo $DISTRIB_DESCRIPTION'],
        { encoding: 'utf8', timeout: 20000, stdio: ['ignore', 'pipe', 'ignore'] }
      ).trim();
      if (out) { back = true; board = out; break; }
    } catch {
      // not up yet (fresh -n image: empty password, ssh may lag the web UI)
    }
    await new Promise((r) => setTimeout(r, 10000));
  }

  note(`post-flash probe of ${POST_FLASH_HOST}: ${back ? 'UP — ' + board.replace(/\n/g, ' | ') : 'no answer within the window'}`);
  expect(back, `device did not come back on ${POST_FLASH_HOST} within ${POST_FLASH_WAIT_MS / 1000}s — treat as INCONCLUSIVE, check it physically`).toBe(true);

  console.log('\n--- U-BOOT REFLASH EVIDENCE ---\n' + evidence.map((l) => '  ' + l).join('\n'));
});
