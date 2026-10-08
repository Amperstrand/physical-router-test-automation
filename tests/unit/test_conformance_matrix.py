"""Conformance-matrix contract guards.

The matrix under tests/conformance/ is co-owned by
OpenTollGate/tollgate-module-basic-go#503 and
Amperstrand/tollgate-module-basic-rust#15 — scenario IDs are join keys
for every lane and historical differential report. PR #16 rewrote the
file (renamed IDs, dropped ``subset:``) and broke the contract silently;
these tests make any future drift fail loudly.
"""

import os

import pytest
import yaml

MATRIX = os.path.join(os.path.dirname(__file__), "..", "conformance",
                      "matrix.yaml")

KNOWN_INVARIANTS = {
    "no-fund-loss", "no-double-count", "no-output-reuse",
    "service-or-refund", "operator-spendable", "retry-safe",
    "restart-converges",
}

# Stable IDs published by the original shared matrix (join keys).
STABLE_IDS = {
    "pay-kill-pre-receive",
    "pay-kill-post-receive-pre-session",
    "pay-kill-post-session-pre-gate",
    "pay-kill-post-gate-pre-response",
    "swap-timeout-retry",
    "duplicate-post-sequential",
    "duplicate-post-concurrent",
    "mint-alias-spellings",
    "keyset-rotation-mid-session",
    "keyset-rotation-final-expiry-held-balance",
    "http-429-burst",
    "http-500-burst",
    "dns-failure",
    "delayed-response-30s",
    "mint-restart-quote-to-mint",
    "vm-reboot-pending-payment",
    "partial-drain-two-mints",
    "payout-failure-after-owner-paid",
    "hard-power-loss",
}

VALID_ACTIONS = {"drop", "drop_response", "delay", "status", "reset",
                 "pass", "notify"}


@pytest.fixture(scope="module")
def matrix():
    with open(MATRIX) as f:
        return yaml.safe_load(f)


def ids(matrix):
    return [sc["id"] for sc in matrix["scenarios"]]


def test_loads_and_versioned(matrix):
    assert matrix["version"] >= 1
    assert matrix["scenarios"], "matrix must carry scenarios"


def test_ids_unique(matrix):
    got = ids(matrix)
    assert len(got) == len(set(got)), "duplicate scenario ids"


def test_stable_ids_never_removed(matrix):
    """The join keys of every existing lane/report — removing or renaming
    any of these orphans historical differentials (ownership rule 3)."""
    missing = STABLE_IDS - set(ids(matrix))
    assert not missing, f"stable scenario ids removed/renamed: {sorted(missing)}"


def test_every_scenario_declares_subset(matrix):
    """PR/nightly lanes select by subset: fast | full (README contract)."""
    for sc in matrix["scenarios"]:
        assert sc.get("subset") in ("fast", "full"), \
            f"{sc['id']}: missing/invalid subset (got {sc.get('subset')!r})"


def test_every_scenario_declares_valid_invariants(matrix):
    for sc in matrix["scenarios"]:
        asserts = sc.get("asserts")
        assert isinstance(asserts, list) and asserts, \
            f"{sc['id']}: no invariant asserts"
        unknown = set(asserts) - KNOWN_INVARIANTS
        assert not unknown, f"{sc['id']}: unknown invariants {sorted(unknown)}"


def test_proxy_rules_use_the_shared_schema(matrix):
    """fault.proxy rules must speak the shared faultproxy.py schema — a
    private rule vocabulary silently no-ops in co-owned lanes."""
    for sc in matrix["scenarios"]:
        for rule in (sc.get("fault") or {}).get("proxy") or []:
            assert rule.get("action") in VALID_ACTIONS, \
                f"{sc['id']}: rule action {rule.get('action')!r} not in shared schema"
            if rule["action"] == "status":
                assert "status_code" in rule, f"{sc['id']}: status rule needs status_code"
            if rule["action"] == "delay":
                assert "delay_ms" in rule, f"{sc['id']}: delay rule needs delay_ms"
            if rule["action"] == "notify":
                assert "notify_on" in rule, f"{sc['id']}: notify rule needs notify_on"


def test_fast_subset_is_nonempty_and_covered_by_host_lanes(matrix):
    fast = [sc["id"] for sc in matrix["scenarios"] if sc["subset"] == "fast"]
    assert fast, "fast subset must exist for PR lanes"
