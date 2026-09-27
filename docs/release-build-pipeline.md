# TollGate Release & Build Pipeline — Portal Focus

> Status snapshot: 2026-09-17, `tollgate-module-basic-go` main @ `67d4f6c`,
> `VERSION` = `v0.6.0-alpha2` (staged, not yet tagged). Written for the
> v0.6.0 campaign (PRTA #113); portal-integration question raised while
> deciding what changes before vs after the release.

## Repos and roles

| Repo | Role |
|---|---|
| `OpenTollGate/tollgate-module-basic-go` | Backend + packaging + release channel publisher (this is "tmbg") |
| `OpenTollGate/tollgate-captive-portal-site` | The captive-portal SPA (Vite build). Embedded into the package at build time, pinned by commit |
| `OpenTollGate/tollgate-os` | Firmware image builder — consumes the Nostr release channel, not git |
| `net4sats/*` | Retail product identity on top of TollGate: branded portal copy, onboarding wizard, its own OpenWrt feed |
| `Amperstrand/tollgate-module-basic-go` | Working fork (agent-filed issues live here) |

## How the portal is compiled into the final artifact

**Package lane (both ipk and apk):** `packaging/portal-build.sh` runs on the
CI runner *before* packaging:

1. Portal revision is **pinned by SHA** in `packaging/build-inputs.json`
   (`portal.commit`, currently `86ac5fc…`). A floating `PORTAL_REF` is
   rejected unless `PORTAL_ALLOW_FLOATING=1` (dev only, output then not
   reproducible).
2. node/npm versions are verified against the same manifest
   (`NODE_VERSION` 22.17.0 / `NPM_VERSION` 10.9.2).
3. `npm ci && npm run build` → `build/*` copied into
   `packaging/files/tollgate-captive-portal-site/`, all mtimes normalized to
   `SOURCE_DATE_EPOCH`.
4. Provenance recorded: `packaging/portal-resolved.sha` +
   `packaging/portal-build-inputs.json` (resolved SHA, pinned SHA, node/npm,
   epoch).
5. Both packaging lanes then embed that directory into the payload at
   `/etc/tollgate/tollgate-captive-portal-site/` — the SDK-free ipk lane via
   `local-build-ipk.sh`, the SDK apk lane via `packaging/Makefile`'s install
   define. One shared staging source, fips-style.

**Image lane (TollGate OS):** `tollgate-os` does **not** rebuild anything.
Its workflow discovers `tollgate-wrt` **from the Nostr release channel**
(kind-1063 filtered by publisher pubkey + version + arch — the same channel
our #112 harness verifies), downloads from a blossom mirror, and injects the
package into OpenWrt's **image-builder**. `packages.json` pins
author+version+compression per release (the `trigger-build-os` repository
dispatch from tmbg's publish job updates it). The portal therefore reaches
firmware images *inside* the `tollgate-wrt` package — portal iteration
necessarily produces a full package release.

## How the portal is served on the router

```
client (pre-auth)
  → nodogsplash redirect → http://<gw>:2050/splash.html   (NDS's hardcoded splash)
      = tiny STUB in /etc/nodogsplash/htdocs  (uci-default 90-…-symlink)
  → stub bounces to the SPA on the dedicated uhttpd "portal" instance :2051
      docroot /etc/tollgate/tollgate-captive-portal-site   (pre-auth allowed
      via NDS users_to_router; instance created in 99-tollgate-setup)
  → SPA JS talks to the backend on :2121
```

The stub exists because NDS's libmicrohttpd pre-auth path is fragile serving
a full SPA (MAC-resolution misses render HTTP 500 for every request) and
hardcodes `splash.html` as its splash page. Older installs with a symlinked
htdocs are migrated to the stub by the same uci-default.

## The net4sats variant

net4sats is the retail identity built on TollGate, with its own distribution:

- `net4sats-feed` — a classic OpenWrt feed (`blossom.net4sats.com/feed/net4sats`;
  opkg `src/gz` + apk `repositories` lines; `curl … install.sh | sh`). The
  `net4sats` meta-package pulls **configurationwizzard** (admin UI + branded
  captive portal, built from `net4sats/configurationwizzard`) + `tollgate-wrt`
  + `nodogsplash`.
- Its MANIFEST shows tollgate-wrt itself fetched from
  `releases.tollgate.me/alpha/v0.5.0-beta2/{arch}` — i.e. a **classic
  per-release feed index** (opkg/apk layout) exists in the retail path,
  hosted per release. (Ownership/hosting details of releases.tollgate.me not
  verified here.)
- The branded portal (`net4sats/tollgate-captive-portal-site` /
  `net4sats-captive-portal-site`) is a **standalone copy, not a GitHub fork**
  (no parent link; last push 2026-07-17) — it tracks upstream manually, so
  upstream portal changes reach the branded variant only by manual re-copy.

Key structural difference vs upstream TollGate: net4sats **decouples the
portal from the backend package** (its own `configurationwizzard` package,
independently versioned) and **distributes via standard feeds**, while
upstream embeds the portal in `tollgate-wrt` and distributes via Nostr
events.

## Assessment — better ways, minimal changes

Ranked by value/effort; **all post-release**. The v0.6.0 tag is gated and
alpha2 is staged; any change to portal packaging invalidates the
reproducibility evidence just gathered (upstream #371 spike) and reopens the
artifact matrix. Nothing portal-related should change before the tag.

1. **Split the portal into its own package** (`tollgate-portal` ipk/apk from
   the same repo/CI): portal iterations stop forcing full backend releases;
   image lane can pin backend + portal versions independently in
   `packages.json`. net4sats's configurationwizzard proves the pattern in
   production. Moderate effort (second Package define + matrix changes).
2. **Publish feed indexes alongside kind-1063** (Packages / APKINDEX per
   release on a static host): makes `opkg/apk add tollgate-wrt` work for
   standard OpenWrt users; the retail path (releases.tollgate.me,
   net4sats-feed on blossom) already demonstrates both hostings. Small
   effort in the publish job.
3. **Turn the branded portal copy into a real fork (or subtree) + document
   the drift policy** so upstream↔net4sats diffing is one command.
   Coordination item with the net4sats side.
4. Optional, lower value: vendor prebuilt portal assets (pinned tarball)
   instead of building at package time — removes node from the ipk lane, but
   the current build is already reproducible and single-source; only worth
   it if CI time matters.

## Release flow summary (current)

```
tmbg tag push (vX.Y.Z)
  → determine-versioning (tag → version + channel stable/alpha/beta)
  → build Go binaries natively (pinned go via build-inputs.json)
  → portal-build.sh (pinned portal SHA + node/npm)
  → ipk lane: packaging/build-ipk.sh (SDK-free, deterministic)
  → apk lane: openwrt/sdk:<target>-25.12.0@digest (packaging-only)
  → upload to 5 blossom mirrors (BLOSSOM_MIN_SUCCESS=1)
  → kind-1063 events to 5 relays, signed by publisher key
  → verify events landed → repository-dispatch tollgate-os
       → tollgate-os updates packages.json, image-builder builds
         firmware from Nostr-discovered packages, publishes images
```

Known open items on this pipeline: mirror redundancy currently aspirational
(2/3 dead for v0.5.0; min-success gate is 1), cross-lane byte-equality
pending the apk-tools sorted-mkpkg fix (upstream #371), license-text staging
divergence parked as a team decision (Amperstrand fork #92).
