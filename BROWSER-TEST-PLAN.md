# Browser Test Execution Plan

## Goal
Run Playwright admin SPA tests on both physical routers, render dashboard, publish to nsite, and post results to all relevant PRs/issues.

## Workstation Setup (one-time)

### Prerequisites
```bash
# Python deps
pip install nostr-sdk pynostr requests websockets pytest playwright

# Playwright browsers
npx playwright install chromium

# nsite publishing (two options):

# Option A: Python-only (recommended, no deno needed)
pip install nostr-sdk requests
# Uses scripts/publish-nsite.py

# Option B: nsyte CLI (requires deno)
# Install deno:
curl -fsSL https://deno.land/install.sh | sh
export PATH="$HOME/.deno/bin:$PATH"
# Install nsyte from source:
git clone https://github.com/sandwichfarm/nsyte.git /tmp/nsyte
cd /tmp/nsyte && deno task compile
cp dist/nsyte ~/.local/bin/
```

### Router Setup
```bash
# Set passwords (both routers)
ssh root@10.47.41.1 "printf 'c03rad0r123\nc03rad0r123\n' | passwd root"
ssh root@192.168.244.1 "printf 'c03rad0r123\nc03rad0r123\n' | passwd root"
```

Credentials are stored in `mint-health/routers.env`:
- `ROUTER_PASSWORD=c03rad0r123`
- `TOLLGATE_LUCI_PASSWORD=c03rad0r123`

## Execution Checklist

### Step 1: Merge PR #24 (squash)
- [x] Cherry-picked to main (PR #24 closed — merge conflicts resolved via cherry-pick)

### Step 2: Set router passwords
- [x] Alpha (10.47.41.1): password set
- [x] Beta (192.168.244.1): password set

### Step 3: Add password to mint-health/routers.env
- [x] `ROUTER_PASSWORD=c03rad0r123` and `TOLLGATE_LUCI_PASSWORD=c03rad0r123` appended

### Step 4: Add admin SPA project to playwright.config.mjs
- [x] Added `desktop-admin` project matching `admin_spa.spec.mjs`

### Step 5: Generate nsec for nsite publishing
- [x] Generated (stored in session only, not committed)

### Step 6: Install nsyte / prepare publish script
- [x] `scripts/publish-nsite.py` created (Python-only, no deno needed)

### Step 7: Run Playwright on Alpha
- [ ] `TOLLGATE_LUCI_URL=http://10.47.41.1 npx playwright test tests/browser/admin_spa.spec.mjs --config tests/playwright.config.mjs --project=desktop-admin`

### Step 8: Run Playwright on Beta
- [ ] `TOLLGATE_LUCI_URL=http://192.168.244.1 npx playwright test tests/browser/admin_spa.spec.mjs --config tests/playwright.config.mjs --project=desktop-admin`

### Step 9: Render dashboard
- [ ] Run `render_dashboard.py` on Playwright results

### Step 10: Publish dashboard to nsite
- [ ] `python3 scripts/publish-nsite.py --dashboard-dir /tmp/e2e-dashboard --nsec $NSEC`

### Step 11: Comment nsite URL on all PRs + create issue
- [ ] Comment on `OpenTollGate/tollgate-module-basic-go` PRs: #124, #137, #138, #139, #140, #141, #142, #143
- [ ] Comment on `OpenTollGate/tollgate-captive-portal-site` PRs: #10, #11
- [ ] Create issue on `net4sats/configurationwizzard` with test results

## Key Files
- `scripts/publish-nsite.py` — Python-only nsite publisher via Blossom (no deno/nsyte needed)
- `mint-health/routers.env` — Router IPs, credentials, SSIDs
- `tests/playwright.config.mjs` — Playwright project definitions (admin, portal, luci, protocol, destructive)
- `tests/browser/admin_spa.spec.mjs` — 8 admin SPA tests (login, dashboard, settings, wallet, wifi, devices, nav, logout)
- `/home/c03rad0r/plebeian-testing-nsite-actions/.github/actions/render-dashboard/render_dashboard.py` — Dashboard renderer

## nsite Publishing Flow
1. Playwright generates `report/report.json` + screenshots
2. `render_dashboard.py` reads results, generates HTML dashboard with inline screenshots
3. `publish-nsite.py` uploads all files to Blossom, publishes Kind 34128 nsite event
4. Dashboard available at `https://nsite.orangesync.tech/<npub>/`
5. Post nsite URL as PR comments
