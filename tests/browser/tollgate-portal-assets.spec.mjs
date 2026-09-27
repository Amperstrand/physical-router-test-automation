/**
 * tollgate-portal-assets.spec.mjs
 *
 * Regression for the captive-portal "NS_ERROR_CORRUPTED_CONTENT / disallowed
 * MIME type (text/html)" failure: the feed-built tollgate-wrt package shipped
 * splash.html WITHOUT the hashed /assets/*.js|css bundles, so uhttpd answered
 * every missing asset with its HTML 404 page. The SPA never booted.
 *
 * This spec verifies, against a real router:
 *   1. Every /assets/*.{js,css} referenced by splash.html resolves with a 200
 *      and a JS/CSS content-type (never text/html).
 *   2. Icons/manifest resolve (logo192.png, manifest.json, favicon.ico).
 *   3. The SPA actually boots in Chrome (#root mounts) with no MIME/corrupted
 *      console errors, following the real pre-auth flow (nodogsplash stub on
 *      :2050 -> uhttpd portal on :2051).
 *
 * Run from a host that can reach the router LAN (e.g. CobradorWave):
 *   ROUTER_IP=192.168.1.1 npx playwright test \
 *     --config tests/browser/tollgate-portal-assets.config.mjs
 */
import { test, expect } from '@playwright/test';

const ROUTER_IP = process.env.ROUTER_IP || '192.168.1.1';
const PORTAL_PORT = process.env.PORTAL_PORT || '2051';
const NDS_PORT = process.env.NDS_PORT || '2050';
const PORTAL = `http://${ROUTER_IP}:${PORTAL_PORT}`;

const MIME_OK = {
  js: [/javascript/i, /ecmascript/i],
  css: [/text\/css/i],
  png: [/image\/png/i],
};

function badMime(ref, contentType) {
  const ext = ref.split('?')[0].split('.').pop().toLowerCase();
  const allowed = MIME_OK[ext];
  if (!allowed) return false; // only assert on known asset types
  return !allowed.some((re) => re.test(contentType || ''));
}

test.describe.configure({ mode: 'serial' });

test('1. every asset referenced by splash.html resolves with the right MIME', async ({ request }) => {
  const resp = await request.get(`${PORTAL}/splash.html`);
  expect(resp.status(), 'splash.html must be served').toBe(200);
  const html = await resp.text();

  const refs = [...html.matchAll(/\/assets\/[A-Za-z0-9._-]+\.(?:js|css)/g)].map((m) => m[0]);
  expect(refs.length, 'splash.html must reference hashed /assets bundles').toBeGreaterThan(0);
  console.log(`[portal] splash.html references ${refs.length} assets`);

  for (const ref of [...new Set(refs)]) {
    const r = await request.get(`${PORTAL}${ref}`);
    const ct = r.headers()['content-type'] || '';
    expect(r.status(), `${ref} must be 200 (was ${r.status()})`).toBe(200);
    expect(badMime(ref, ct), `${ref} served as "${ct}" — HTML fallback means the asset is missing`).toBe(false);
    console.log(`[portal] OK ${ref} -> ${r.status()} ${ct}`);
  }

  for (const [ref, re] of [
    ['/logo192.png', /image\/png/i],
    ['/manifest.json', /application\/json/i],
    ['/favicon.ico', /(image\/x-icon|image\/vnd\.microsoft\.icon|image\/ico|application\/octet-stream)/i],
  ]) {
    const r = await request.get(`${PORTAL}${ref}`);
    const ct = r.headers()['content-type'] || '';
    expect(r.status(), `${ref} must be 200`).toBe(200);
    expect(ct, `${ref} content-type`).toMatch(re);
    console.log(`[portal] OK ${ref} -> ${r.status()} ${ct}`);
  }
});

test('2. the portal SPA boots with no MIME/corrupted errors', async ({ page }) => {
  const errors = [];
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text());
  });
  page.on('pageerror', (e) => errors.push(String(e)));

  await page.goto(`${PORTAL}/splash.html`, { waitUntil: 'domcontentloaded', timeout: 30000 });

  // React mounts into #root; a MIME-blocked entry script leaves it empty.
  await page.waitForFunction(
    () => {
      const root = document.getElementById('root');
      return root && root.children.length > 0;
    },
    null,
    { timeout: 30000 },
  );

  const rootHtml = (await page.locator('#root').innerHTML()).trim();
  expect(rootHtml.length, 'the SPA must render content, not an empty #root').toBeGreaterThan(0);
  await page.screenshot({ path: 'test-results/tollgate-portal-assets.png', fullPage: true });

  const mimeErrors = errors.filter((e) =>
    /disallowed MIME|CORRUPTED_CONTENT|was blocked because of a disallowed|Failed to load module script/i.test(e),
  );
  expect(mimeErrors, `MIME/module load errors:\n${mimeErrors.join('\n')}`).toEqual([]);
});

test('3. pre-auth flow: nodogsplash stub redirects to the portal on :2051', async ({ page }) => {
  await page.goto(`http://${ROUTER_IP}:${NDS_PORT}/`, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
  // The stub may run location.replace immediately; accept already being on :2051.
  await page.waitForFunction(
    (port) => location.port === port || location.href.includes(`:${port}/splash.html`),
    PORTAL_PORT,
    { timeout: 20000 },
  ).catch(async () => {
    // Some NDS versions serve the stub in-place and only redirect on JS exec;
    // fall back to asserting the stub content is present.
    const body = await page.content();
    expect(body, 'expected the NDS stub to reference the :2051 portal').toMatch(/:2051\/splash\.html/);
  });
  console.log(`[portal] pre-auth landed on ${page.url()}`);
});
