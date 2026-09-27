# TollGate release-based client cache purge — recommendation

Problem (from the rig, 2026-09-26): after a portal deploy/change, returning
client browsers keep stale portal HTML/redirects (Chrome served a cached
301 to a decommissioned portal IP for an entire day). Real users cannot be
asked to clear caches; the TollGate must force freshness per release.

## Root causes on our stack
1. Portal HTML served without `Cache-Control` → browsers heuristically
   cache (uhttpd default: no Cache-Control, Last-Modified 1970 from
   reproducible builds → effectively "never changes").
2. Redirects: NDS/portal flow issues 301s (permanently cached by Chrome).
   The ESP32 lane already worked around this with a cache-busting shim.
3. The SPA (cashu.me-style) assets are already content-hashed — the entry
   document is the only piece that must stay fresh.

## The release mechanism (server-controlled, zero user action)

**Layer 1 — headers on the portal origin (every deploy inherits this):**
- `splash.html` and all HTML: `Cache-Control: no-cache, must-revalidate`
  (revalidate always; 304s keep it cheap). For the captive context where
  every byte costs, `no-store` on splash.html is also defensible — the
  portal is small and always pre-auth (unmetered side).
- Hashed `/assets/*`: `Cache-Control: public, max-age=31536000, immutable`
  (cache-busting pattern: new deploy → new hashes → new URLs).
- Redirects: use 302/307 only — never 301 — anywhere in the portal flow
  (NDS `RedirectURL`, backend 3xx, the :2050→:2051 shim). 301s poison
  clients across releases; 302s are re-evaluated every time.
- `sw.js` (if the PWA registers one): `no-cache, max-age=0, must-revalidate`
  and `updateViaCache: 'none'` at registration — otherwise worker update
  checks are answered from disk for up to 24h.

**Layer 2 — deploy-time version bump (the "release purge"):**
The uhttpd config is one file per deploy; add a versioned redirect at the
document root that changes every install:
```
# in the ipk's uci-defaults / portal deploy:
# splash.html references /assets/index-<hash>.js (already versioned).
# Deploy stamps a build id into splash.html (asset-manifest.json already
# carries it) — new build id → new HTML bytes → no-cache revalidation
# delivers it on the client's very next portal hit.
```
No purge action needed: because HTML is `no-cache` and assets are
content-hashed, a deploy IS the purge. This matches the standard
"cache-busting + never mutate immutable assets" best practice (MDN).

**Layer 3 — the kill switch for emergencies (bad deploy already cached):**
Serve `Clear-Site-Data: "storage"` on the portal document for one release
window. Constraints (from research): HTTPS-only (our portal is HTTP —
Chrome ignores it on insecure origins; iOS Safari honors it on HTTP only
partially) — so on our plain-HTTP captive portal this header is NOT
reliable. The HTTP-compatible equivalent: a JS bootstrap purge in the
portal bundle:
```js
// runs before the SPA mounts; guarded by a build-id in localStorage
if (storedBuildId !== BUILD_ID) {
  const keys = await caches.keys();
  await Promise.all(keys.map(k => caches.delete(k)));
  const regs = await navigator.serviceWorker.getRegistrations();
  await Promise.all(regs.map(r => r.unregister()));
  localStorage.setItem('tg_build', BUILD_ID);
  location.reload();
}
```
(the kiosk-pattern bootstrap purge — clears Cache Storage + SW regs once
per build fingerprint, then reloads; reload-guard prevents loops).

**Layer 4 — NDS redirect hygiene:**
NDS's port-80 hijack redirect must always target the CURRENT portal host
(never an absolute stale IP): prefer `status 302` + a location computed
from the request, or keep DHCP option 114 (RFC 8910 captive URI) fresh —
the NR7101 build already ships option 114 pointing at the portal.

## What NOT to do
- Do not rely on `Clear-Site-Data` as the primary mechanism (HTTPS-only).
- Do not 301 anything in the portal flow.
- Do not cache-bust hashed assets (they are already immutable-per-build).
- Do not ask users to clear anything.

## Verification for the golden image
1. `curl -I http://<dut>:2051/splash.html` → `Cache-Control: no-cache` (or
   no-store) present.
2. `curl -I http://<dut>:2051/assets/<hashed>.js` → max-age=31536000.
3. Deploy a build with changed content → client's next load serves the new
   HTML (verify on the phone via a fresh-key URL + Cache inspector).
4. No 301s in the flow: `curl -sI http://<dut>:2050/` → 302 (or 307).
