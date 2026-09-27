# Session State Management — Design and Rationale

## Problem

Tests shared mutable state across three dimensions, causing ordering-dependent failures:

| State | Owner | Failure mode |
|---|---|---|
| NDS session (auth) | Router | Expiry test deauths → payment test finds no session |
| Rate-limit budget | Backend | 5+ payment tests → 10/min limit → kind:21023 |
| Token (bearer instrument) | Mint | Failed test burns token → next test has nothing |

Root cause: **tests assumed state instead of declaring it**.

## Solution: State-as-Fixture (labgrid Strategy pattern)

Borrowed from labgrid's `Strategy` fixtures — each test requests the state
it needs via a function-scoped fixture. The fixture handles the transition
(deauth, re-auth, rate-limit wait) and the teardown. Tests never assume
state — they declare it.

### Fixtures

```
no_session      → device is guaranteed UNAUTHENTICATED (portal visible)
fresh_session   → device is guaranteed AUTHENTICATED (internet working)
rate_limiter    → session-scoped tracker, backs off when approaching 10/min
```

### Usage

```python
def test_user_pays(no_session, ...):        # starts unauthenticated
    assert not no_session.has_internet()     # verify precondition
    token = mint(4)
    assert no_session.submit_token(token)    # pay
    assert no_session.has_internet()         # verify

def test_session_expiry(fresh_session, ...): # starts authenticated
    assert fresh_session.has_internet()      # verify precondition
    deauth(fresh_session.mac)               # force expiry
    assert not fresh_session.has_internet()  # verify blocked
    token = mint(4)                          # re-pay
    assert fresh_session.submit_token(token)
    assert fresh_session.has_internet()      # verify restored
```

### Why this pattern (not alternatives)

| Alternative | Why rejected |
|---|---|
| Test ordering (pytest-ordering) | Fragile — adding a test breaks the order silently |
| Session-scoped auth (current) | The exact bug — state leaks between tests |
| Autouse deauth | Wasteful — deauths even for read-only tests |
| Per-file conftest | Still shared within a file; doesn't solve rate limiting |

The state-as-fixture pattern is what labgrid uses for hardware testing
(Strategy → transition('shell') → guaranteed state). It's the proven
pattern for shared mutable hardware state.

### Rate-limiter design

The backend rate-limits payments to 10/min per client IP. With the
state-as-fixture pattern, each `fresh_session` setup makes a payment.
Running 5+ tests that need `fresh_session` could exhaust the budget.

The rate_limiter fixture tracks payment timestamps and sleeps when
approaching the limit. It's session-scoped (shared across all tests in
a run) and self-cleaning (drops entries older than 60s).

### Token burn protection

Tokens are bearer instruments — once submitted and consumed at the mint,
they cannot be reused. The fixtures mint fresh tokens per invocation,
so a failed test burns one token (4 sats of fakewallet) but doesn't
affect the next test's ability to pay.
