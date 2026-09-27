# PRTA as the Authoritative Test Suite — Design Direction

## The Problem: Cross-Implementation Drift

We have multiple TollGate implementations (net4sats, tollgate-module-basic-go,
tollgate-module-basic-rust, ESP32/tollgate-core, NR7101 builds) that are
drifting apart in behavior:

- **Portal ports differ**: :80 vs :2050 vs :2051 per build
- **Portal copy differs**: "Generate Invoice" vs "Purchase Internet Access"
- **Default tab differs**: Lightning-first vs Cashu-first per build
- **Pricing UI differs**: .size-btn preset tiers vs amount stepper
- **Token format acceptance differs**: V3-only vs V3+V4, cashuA vs cashuB
- **Preauth rules differ**: some allow mint pre-payment, some don't
- **NDS chain behavior differs**: mark rules, auth-mark bug presence

Nobody notices until a manual test fails on one rig but passes on another,
and nobody can say WHICH behavior is correct.

## The Solution: PRTA as the Single Point of Authority

PRTA should be the **behavior contract** — the place you go to answer
"what is SUPPOSED to happen when a user joins a TollGate WiFi and pays
a token?" Every implementation must pass the same user-story tests.

## Design Principles

### 1. User Stories, Not Implementation Details

Tests should read like a user's experience, not a developer's debug session:

```gherkin
Feature: Phone payment through captive portal
  Scenario: User pays with a Cashu token
    Given the phone joins the open TollGate SSID
    And the captive portal appears
    When the user submits a valid Cashu token
    Then internet access is granted within 30 seconds
    And the phone can load a real webpage
    And an external IP is visible
```

NOT:
```
Test: POST to :2121 with Content-Type text/plain, expect kind:1022
```
(That's an API test, not a user story.)

### 2. Behavior-Based Assertions, Not Selector-Based

Assert WHAT the user sees, not WHICH CSS class renders it:

```javascript
// ✅ Behavior: "the portal shows Lightning payment copy"
await expect(page.getByText(/pay with.*lightning/i)).toBeVisible();

// ❌ Implementation: "the .tollgate-captive-portal-method-lightning class has an h2"
const payLine = document.querySelector('.tollgate-captive-portal-method-lightning .tollgate-captive-portal-method-header h2');
```

This makes tests survive UI refactors across implementations.

### 3. Film Well (Evidence-First)

Every test should produce evidence that a stakeholder can review:

- Screen recording (video)
- Step-by-step screenshots with claim tags
- Vision-AI validation of each screenshot against its claim
- External IP capture for internet-verification tests

This is already built (lib/clients/evidence.py). Extend it to browser tests.

### 4. Contract JSON: The Expected-Behavior Registry

A versioned file that declares what "correct" looks like per capability:

```json
{
  "portal": {
    "entry_url": "/splash.html",
    "default_tab": "implementation-defined",
    "tabs": ["lightning", "cashu"],
    "ports": {"portal": 2051, "nds_gateway": 2050, "backend": 2121}
  },
  "payment": {
    "token_formats": ["cashuA (V3)", "cashuB (V4)"],
    "content_type": "text/plain",
    "response_success": "kind:1022",
    "auth_timeout_seconds": 30
  },
  "internet": {
    "validation": "Android VALIDATED capability",
    "probe_host": "8.8.8.8",
    "external_ip_service": "ifconfig.me"
  }
}
```

Tests read this and assert against it. When an implementation changes
behavior, the contract JSON changes in the same PR, and the drift is
visible in the diff.

### 5. Cross-Implementation Test Matrix

Run the same user stories against every implementation:

| Test | Go | Rust | NR7101 | ESP32 |
|------|----|------|--------|-------|
| Phone joins SSID | ✓ | ✓ | ✓ | ✓ |
| Portal appears | ✓ | ✓ | ✓ | ✓ |
| Token payment accepted | ✓ | ✓ | ✓ | partial |
| Internet granted | ✓ | ✓ | ✓ | ✗ (no upstream) |
| Tab copy correctness | ✓ | ? | ✓ | n/a |

The matrix IS the drift detector. A cell that flips from ✓ to ✗ is a
regression; a cell that was always ✗ is a known gap.

## Concrete Next Steps

### Phase 1: Consolidate existing tests into user stories
- Refactor phone tests (pytest) into named user-story scenarios
- Refactor browser tests (Playwright) to use behavior selectors
- Add contract JSON files per capability

### Phase 2: Add the missing user stories
- "User pays with a V4/CBOR token" (cashuB)
- "User's session expires and they must re-pay"
- "User sees degraded mode when mints are unreachable"
- "User switches between Lightning and Cashu tabs without copy bleed"
- "Router reboots and the user's session persists" (or doesn't)

### Phase 3: Wire into CI
- Every PR to any TollGate repo triggers PRTA tests
- Test matrix published as an artifact
- Drift = red cell in the matrix = blocked PR

### Phase 4: ESP32 and phone coverage expansion
- ESP32: portal copy, token acceptance, NDS gating
- Phone: more Android versions, more browsers, PWA install flow
- Multi-device: two phones paying simultaneously

## Anti-Patterns to Avoid

1. **Don't test implementation internals** (chain names, iptables rules)
   — those change per build and aren't user-visible
2. **Don't hardcode build-specific selectors** — use behavior/text selectors
3. **Don't skip tests on "known-different" builds** — surface the difference
   in the matrix instead
4. **Don't assert on logs** — assert on user-visible behavior
5. **Don't make tests that only developers can interpret** — a product
   manager should be able to read the test name and know what broke

## Existing Assets to Leverage

- `lib/clients/evidence.py` — video + screenshots + vision validation
- `lib/clients/u2phone.py` — Android phone automation
- `lib/clients/cdp_wallet.py` — Chrome DevTools Protocol wallet driving
- `tests/browser/portal-tab-copy.spec.mjs` — first behavior-based browser test
- `tests/phone/test_rig_phone_payment.py` — phone E2E (needs user-story naming)
- `docs/release-cache-purge.md` — cross-implementation design doc precedent
