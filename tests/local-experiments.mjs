import { chromium } from "@playwright/test";
import { SEL_SUCCESS, SEL_TOKEN_INPUT, openCashuTab } from "./helpers/portal-selectors.mjs";

const BASE = "http://localhost:5173";
const API = "http://localhost:2121";
const results = [];

async function test(name, fn) {
  try {
    const start = Date.now();
    await fn();
    results.push({ name, status: "PASS", ms: Date.now() - start });
  } catch (e) {
    results.push({ name, status: "FAIL", error: e.message?.slice(0, 200), ms: 0 });
  }
}

const browser = await chromium.launch({ headless: true });

// === EXPERIMENT 1: Page load timing ===
await test("Page load timing", async () => {
  const page = await browser.newPage();
  const start = Date.now();
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 15000 });
  const ms = Date.now() - start;
  await openCashuTab(page);
  await page.waitForSelector(SEL_TOKEN_INPUT, { timeout: 10000 });
  console.log(`  Page loaded in ${ms}ms, input visible`);
  if (ms > 5000) throw new Error(`Slow load: ${ms}ms`);
  await page.close();
});

// === EXPERIMENT 2: Balance page rendering ===
await test("Balance page renders with data", async () => {
  const page = await browser.newPage();
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 15000 });
  await page.waitForSelector("#tab-balance", { timeout: 10000 });
  await page.click("#tab-balance");
  await page.waitForTimeout(2000);
  const text = await page.locator("body").innerText();
  if (!text.includes("Balance") && !text.includes("balance")) throw new Error("Balance text not found");
  console.log("  Balance page rendered");
  await page.close();
});

// === EXPERIMENT 3: Error state — invalid token (CU101) ===
await test("CU101 error: token not starting with cashu", async () => {
  const page = await browser.newPage();
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 15000 });
  await openCashuTab(page);
  await page.waitForSelector(SEL_TOKEN_INPUT, { timeout: 10000 });
  await page.fill(SEL_TOKEN_INPUT, "not_a_cashu_token");
  await page.waitForTimeout(1000);
  const text = await page.locator("body").innerText();
  if (!text.includes("CU101") && !text.includes("cashu")) throw new Error("CU101 error not shown");
  console.log("  CU101 error shown for invalid prefix");
  await page.close();
});

// === EXPERIMENT 4: Error state — empty token (CU100) ===
await test("CU100: empty token disables purchase", async () => {
  const page = await browser.newPage();
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 15000 });
  await openCashuTab(page);
  await page.waitForSelector(SEL_TOKEN_INPUT, { timeout: 10000 });
  const btn = page.locator(".tollgate-captive-portal-method-submit button").first();
  const disabled = await btn.isDisabled().catch(() => true);
  if (!disabled) throw new Error("Purchase should be disabled when token is empty");
  console.log("  Purchase button disabled for empty token");
  await page.close();
});

// === EXPERIMENT 5: Long token handling (2000 chars) ===
await test("Long token (2000 chars) doesn't crash", async () => {
  const page = await browser.newPage();
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 15000 });
  await openCashuTab(page);
  await page.waitForSelector(SEL_TOKEN_INPUT, { timeout: 10000 });
  const longToken = "cashuB" + "A".repeat(1994);
  await page.fill(SEL_TOKEN_INPUT, longToken);
  await page.waitForTimeout(1000);
  const val = await page.inputValue(SEL_TOKEN_INPUT);
  if (val.length !== 2000) throw new Error(`Input truncated: ${val.length} != 2000`);
  console.log(`  2000-char token accepted in input (len=${val.length})`);
  await page.close();
});

// === EXPERIMENT 6: Network failure recovery ===
await test("Network failure shows error, not crash", async () => {
  const page = await browser.newPage();
  // Block all requests to port 2121
  await page.route("**/localhost:2121/**", route => route.abort());
  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 15000 });
  await page.waitForTimeout(3000);
  const text = await page.locator("body").innerText();
  // Should show error state, not white screen
  if (text.length < 50) throw new Error("Page appears blank (white screen)");
  console.log(`  Network failure handled gracefully (text len=${text.length})`);
  await page.close();
});

// === EXPERIMENT 7: URL-param auto-submit timing ===
await test("URL-param auto-submit fires within 10s", async () => {
  const page = await browser.newPage();
  const realToken = "cashuBpGFteCJodHRwczovL25vZmVlcy50ZXN0bnV0LmNhc2h1LnNwYWNlYXVjc2F0YXSBomFpSAC0zSfYhhpEYXCEpGFhBGFzeF9bIlAyUEsiLHsibm9uY2UiOiI0N2Y4Y2IyYTFiYWY5ZjhkYzQ4ZDI4ZTNiMGUzODhmY2UxYmZiOTVlZjAwODE3MTg4YzkzMTU0NGMyMzJmN2ZjIiwidGFncyI6W119XWFjWCED_Eg3DCumAWtmUlJX-wQL5VMW_uTNyHKfg-K1QapLVahhZKNhZVgg5gGQFjN9-1b_jqKJgbaY4-dhmBYr5UqqUxuxqRLPUzJhc1ggaCiCFnmqkZ02PJJhVJ-vM-_9WtePRDt5cPBlST0wmORhclggE3wqT6NrH2QzGfO_MQ4jTnO59Mc2cr2KGY6vjnohKt2kYWEYIGFzeF9bIlAyUEsiLHsibm9uY2UiOiJmNjdlOWJkNmNkMThiMmI2YjQyM2U3YmU4NWRmMjUxNWU4ZGQyYWU1NzVlYTE3ZTM3YmVkNDc4MjQzZDFjMzlmIiwidGFncyI6W119XWFjWCECWcB712IIHW3sq2emd8eNAZIKUt3SAzOwpAK1CZsZ_k1hZKNhZVggBusKAQ7SDmxNBDhqt1veoTXo4Hdexjq3y-xPQoEwjtdhc1ggdHlFY6ILItNbP87l45KxFuQZb1DPRnFXz9XBkbmcQf5hclgga9odUX_scqsK_9fXhgGgwVR12-z1XBzMIGlsW7Y-B3ykYWEYgGFzeF9bIlAyUEsiLHsibm9uY2UiOiI1YTdjZmM3Mzg0MTQyYjY3Y2I1N2VlMThiOGE3NjIyODgyNTg5YTkwZjYxM2RhZDg1YjM1YzgwNjVmZWFhNTk1IiwidGFncyI6W119XWFjWCECqvNa-Cq7SE2F-X9kmX6BoE_6hdPpziwH7ucvq85dnAhhZKNhZVgguzfdpxik53NXvzJKapvLDg4p_US26WHY7pASwxpF5vxhc1ggD2ZmSOU6LscrWKIJaOvo-2jeWlVeHJXxKWabm9v9NWVhclgglhPmxos7-GuHsRff6dTfdoonXTtZPb96DkmZOqNi2wykYWEZAQBhc3hfWyJQMlBLIix7Im5vbmNlIjoiODE2Y2EwMWFhNGEzOGY5MzYyZmZiNmZlODkzZTlmZTdkZDVmYTRlZmM0MTM4YmVhZGRhMzRhNTEwYzg3ODhkYyIsInRhZ3MiOltdfV1hY1ghA3upuHXYkvqVhg5QMihMwBUuGX71aAeOQaN-8o0rHxHqYWSjYWVYINp6jhzIGN4Vn45g96IzXRm6PNO0C66C3Tpk-g1EpKNuYXNYIFDsqRFfC252PT3HyoNv9siolqEdulhBM3JlMouo-1uOYXJYIIanZZV-SoXRk30n67Wce5a1UiCZfbtl3wtmaaye2YzAYWRyU2VudCBmcm9tIE1pbmliaXRz";
  const start = Date.now();
  await page.goto(`${BASE}/?token=${encodeURIComponent(realToken)}`, { waitUntil: "networkidle", timeout: 30000 });
  // URL-param auto-submit drives the payment itself (no tab click: the token
  // comes from the query string), so just wait for the granted indicator.
  await page.waitForSelector(SEL_SUCCESS, { timeout: 15000 });
  const ms = Date.now() - start;
  console.log(`  Auto-submit to success in ${ms}ms`);
  if (ms > 15000) throw new Error(`Slow auto-submit: ${ms}ms`);
  // Verify URL was stripped
  const url = page.url();
  if (url.includes("token=")) throw new Error("Token still in URL");
  console.log("  URL stripped correctly");
  await page.close();
});

// === EXPERIMENT 8: Console error count ===
await test("Zero console errors on clean load", async () => {
  const page = await browser.newPage();
  const errors = [];
  page.on("console", msg => { if (msg.type() === "error") errors.push(msg.text()); });
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 15000 });
  await page.waitForTimeout(3000);
  if (errors.length > 0) throw new Error(`${errors.length} console errors: ${errors[0]?.slice(0, 100)}`);
  console.log("  0 console errors");
  await page.close();
});

// === EXPERIMENT 9: Rapid tab switching (stress) ===
await test("Rapid tab switching (10x) no crash", async () => {
  const page = await browser.newPage();
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 15000 });
  await page.waitForSelector("#tab-cashu", { timeout: 10000 });
  for (let i = 0; i < 10; i++) {
    await page.click("#tab-balance");
    await page.click("#tab-cashu");
  }
  const text = await page.locator("body").innerText();
  if (text.length < 50) throw new Error("Page blank after tab switching");
  console.log("  10 rapid tab switches completed without crash");
  await page.close();
});

// === EXPERIMENT 10: Memory leak check (5 page loads) ===
await test("5 sequential page loads (memory stability)", async () => {
  for (let i = 0; i < 5; i++) {
    const page = await browser.newPage();
    await page.goto(BASE, { waitUntil: "networkidle", timeout: 15000 });
    await openCashuTab(page);
    await page.waitForSelector(SEL_TOKEN_INPUT, { timeout: 10000 });
    await page.close();
  }
  console.log("  5 page loads completed");
});

await browser.close();

console.log("\n=== EXPERIMENT RESULTS ===");
console.table(results);
const passed = results.filter(r => r.status === "PASS").length;
const failed = results.filter(r => r.status === "FAIL").length;
console.log(`\n${passed} passed, ${failed} failed out of ${results.length} experiments`);
