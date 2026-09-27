// Shared selector contract for the TollGate captive portal (tabbed UI).
//
// Single source of truth for the DOM contract used by the live-hardware specs
// (tests/captive-portal.spec.mjs, tests/protocol/captive-portal.spec.mjs and
// tests/local-experiments.mjs) so the selectors cannot silently drift apart
// again the way they did when the portal became tabbed.
//
// The contract, mirroring lib/portal_payment.py (the Python twin used by the
// pytest integration tier):
//   * Payment methods are tabs. The Cashu token input only renders after the
//     Cashu tab (`.tollgate-captive-portal-tabs-tab-cashu`) is clicked, so
//     every cashu flow must click that tab first.
//   * The token input has no guaranteed stable id; locate it by its
//     placeholder substring ("cashuxyz…") instead of the legacy `id` attribute
//     that the pre-tabbed portal used.
//   * The submit button lives inside `.tollgate-captive-portal-method-submit`
//     and is enabled (no `disabled` attribute) once a valid token is entered.
//   * The post-payment success indicator renders under
//     `.tollgate-captive-portal-access-granted`; the leaf class is emitted as
//     `...-access-granted-checkmark` by both portal front-ends
//     (tollgate-captive-portal-site/src/App.jsx:267,302 and
//     net4sats-captive-portal-site/src/App.jsx:255,290, verified 2026-09-11),
//     while lib/portal_payment.py (PR #87) uses the shorter
//     `...-access-granted-check` spelling. New bundles mark the success-state
//     checkmark with the shared, prefix-free id
//     `captive-portal-access-granted-checkmark` (tollgate-captive-portal-site#42,
//     net4sats-captive-portal-site#3) — success view only, so the
//     expired-session view that reuses the checkmark class can never match.
//     SEL_SUCCESS is id-first with the `class*=` fallback for already-deployed
//     portals; on old bundles the fallback also matches the expired view (a
//     known limitation of the compatibility path).
export const SEL_CASHU_TAB = '.tollgate-captive-portal-tabs-tab-cashu';
export const SEL_TOKEN_INPUT = 'input[placeholder*="cashu"]';
export const SEL_SUBMIT_READY = '.tollgate-captive-portal-method-submit button:not([disabled])';
export const SEL_SUBMIT_CLICK = '.tollgate-captive-portal-method-submit button';
export const SEL_SUCCESS = '#captive-portal-access-granted-checkmark, [class*="access-granted-check"]';
export const SEL_CONTENT = '.tollgate-captive-portal-method-content';

/**
 * Click the Cashu tab so the token input renders.
 *
 * Idempotent: clicking the already-active Cashu tab is a no-op for the portal
 * (it simply re-selects the same method), so callers do not need to know which
 * tab the portal defaults to.
 */
export async function openCashuTab(page, { timeout = 15000 } = {}) {
	await page.waitForSelector(SEL_CASHU_TAB, { timeout });
	await page.click(SEL_CASHU_TAB);
}

/** Click the Cashu tab, then wait for the token input and type `token` into it. */
export async function fillCashuToken(page, token, { timeout = 15000 } = {}) {
	await openCashuTab(page, { timeout });
	await page.waitForSelector(SEL_TOKEN_INPUT, { timeout });
	await page.locator(SEL_TOKEN_INPUT).fill(token);
}
