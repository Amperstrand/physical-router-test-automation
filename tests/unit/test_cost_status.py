"""Unit tests for scripts/cost-status.py (pure logic only — no network).

Run: python3 -m pytest tests/unit/test_cost_status.py -v
"""

import datetime as dt
import importlib.util
import os
import sys

SCRIPTS_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
_spec = importlib.util.spec_from_file_location(
    "cost_status",
    os.path.join(SCRIPTS_DIR, "cost-status.py"),
)
_cs = importlib.util.module_from_spec(_spec)
sys.modules["cost_status"] = _cs  # dataclasses introspection requires registration
_spec.loader.exec_module(_cs)

Billable = _cs.Billable
Rule = _cs.Rule

NOW = dt.datetime(2026, 8, 25, 21, 0, 0, tzinfo=dt.timezone.utc)


import pytest

pytestmark = [pytest.mark.story("P11")]

def _vm(name="tollgate-main-1", state="running", provider="shc", kind="vm", labels=None):
    return Billable(provider=provider, kind=kind, name=name, state=state,
                    daily_cost=0.26, cost_basis="exact", labels=labels or {})


# ── Rule matching ────────────────────────────────────────────────────────

class TestRuleMatching:
    def test_regex_pattern_matches_substring(self):
        rule = Rule(pattern="^tollgate-main-", labels={}, reason="main runner")
        assert rule.matches(_vm("tollgate-main-abc123"))

    def test_regex_pattern_rejects_other(self):
        rule = Rule(pattern="^tollgate-main-", labels={}, reason="main runner")
        assert not rule.matches(_vm("lightning-playground"))

    def test_label_match_requires_all_pairs(self):
        rule = Rule(pattern=None, labels={"team": "core"}, reason="labelled")
        assert rule.matches(_vm("anything", labels={"team": "core"}))
        assert not rule.matches(_vm("anything", labels={"team": "other"}))
        assert not rule.matches(_vm("anything", labels={}))

    def test_pattern_or_label_is_enough(self):
        rule = Rule(pattern="^x-", labels={"team": "core"}, reason="either")
        assert rule.matches(_vm("x-1", labels={}))
        assert rule.matches(_vm("other", labels={"team": "core"}))
        assert not rule.matches(_vm("other", labels={}))


# ── Classification ───────────────────────────────────────────────────────

class TestClassify:
    RULES = [Rule(pattern="^tollgate-main-", labels={}, reason="main runner"),
             Rule(pattern="^europa-vpn-vps$", labels={}, reason="infra")]

    def test_approved_resource(self):
        rows = _cs.classify([_vm("tollgate-main-x")], self.RULES)
        assert rows[0]["approved"] and rows[0]["reason"] == "main runner"
        assert not rows[0]["stopped_warning"]

    def test_unapproved_resource(self):
        rows = _cs.classify([_vm("lightning-playground")], self.RULES)
        assert not rows[0]["approved"]
        assert _cs.worst_problem(rows) == "1 unapproved billable resource(s)"

    def test_stopped_but_billable_warns_even_when_approved(self):
        rows = _cs.classify([_vm("tollgate-main-x", state="stopped")], self.RULES)
        assert rows[0]["approved"] and rows[0]["stopped_warning"]

    def test_stopped_and_unapproved_both_flags(self):
        rows = _cs.classify([_vm("lightning-playground", state="stopped")], self.RULES)
        assert not rows[0]["approved"] and rows[0]["stopped_warning"]

    def test_gcp_terminated_instance_warns(self):
        rows = _cs.classify(
            [_vm("whatever", state="terminated", provider="gcp", kind="instance")], self.RULES)
        assert rows[0]["stopped_warning"]

    def test_stopped_near_renewal_flags_renewal_warning(self):
        vm = _vm("tollgate-main-x", state="stopped")
        vm.renews = (NOW + dt.timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
        rows = _cs.classify([vm], self.RULES, now=NOW)
        assert rows[0]["renewal_warning"]
        assert "renewing within" in _cs.worst_problem(rows)

    def test_stopped_far_from_renewal_is_only_a_warning(self):
        vm = _vm("tollgate-main-x", state="stopped")
        vm.renews = (NOW + dt.timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
        rows = _cs.classify([vm], self.RULES, now=NOW)
        assert rows[0]["stopped_warning"] and not rows[0]["renewal_warning"]
        assert _cs.worst_problem(rows) == ""

    def test_renewal_warning_applies_even_when_approved(self):
        vm = _vm("tollgate-main-x", state="stopped")
        vm.renews = (NOW + dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        rows = _cs.classify([vm], self.RULES, now=NOW)
        assert rows[0]["approved"] and rows[0]["renewal_warning"]

    def test_running_vm_near_renewal_never_flags(self):
        vm = _vm("tollgate-main-x", state="running")
        vm.renews = (NOW + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        rows = _cs.classify([vm], self.RULES, now=NOW)
        assert not rows[0]["renewal_warning"]

    def test_already_past_renewal_time_not_flagged_as_imminent(self):
        vm = _vm("tollgate-main-x", state="stopped")
        vm.renews = (NOW - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        rows = _cs.classify([vm], self.RULES, now=NOW)
        assert not rows[0]["renewal_warning"]

    def test_non_vm_kinds_never_stopped_warning(self):
        rows = _cs.classify(
            [Billable(provider="gcp", kind="snapshot", name="snap", state="active",
                      daily_cost=0.01, cost_basis="estimate:snapshot-gb")],
            self.RULES)
        assert not rows[0]["stopped_warning"]

    def test_no_rules_means_everything_unapproved(self):
        rows = _cs.classify([_vm("tollgate-main-x")], [])
        assert not rows[0]["approved"]


# ── SHC ledger credits + spend reconstruction ───────────────────────────

class TestWindowCredits:
    def _txn(self, hours_ago, amount):
        ts = NOW - dt.timedelta(hours=hours_ago)
        return {"amount": str(amount), "date_added": ts.strftime("%Y-%m-%d %H:%M:%S")}

    def test_bucketing_by_window(self):
        items = [self._txn(1, 0.45), self._txn(3 * 24, 0.25),
                 self._txn(29 * 24, 0.25), self._txn(40 * 24, 0.25)]
        sums = _cs.window_credits(items, NOW)
        assert sums["24h"] == 0.45 and sums["7d"] == 0.70
        assert sums["30d"] == 0.95  # 40d item excluded

    def test_bad_rows_ignored(self):
        assert _cs.window_credits([{"amount": "x"}, {"date_added": "garbage"}, {}], NOW)["24h"] == 0.0

    def test_parse_shc_time_assumes_utc(self):
        parsed = _cs.parse_shc_time("2026-08-25 21:00:00")
        assert parsed is not None and parsed.tzinfo == dt.timezone.utc
        assert _cs.parse_shc_time("nope") is None


class TestReconstructSpend:
    def test_capped_by_age_and_window(self):
        # created 3 days ago at $0.26/day: 24h→0.26, 7d/30d→0.78
        r = _vm("x")
        r.created = (NOW - dt.timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        spend = _cs.reconstruct_spend([r], NOW)
        assert abs(spend["24h"] - 0.26) < 1e-9
        assert abs(spend["7d"] - 0.78) < 1e-9
        assert abs(spend["30d"] - 0.78) < 1e-9

    def test_zero_cost_and_missing_created_skipped(self):
        free = _vm("free")
        free.daily_cost = 0.0
        free.created = "2026-08-01 00:00:00"
        nocreate = _vm("nocreate")
        assert _cs.reconstruct_spend([free, nocreate], NOW) == {"24h": 0.0, "7d": 0.0, "30d": 0.0}

    def test_iso_gcp_timestamp_accepted(self):
        r = _vm("gcpvm", provider="gcp", kind="instance")
        r.created = "2026-08-20T16:19:19.743-07:00"
        spend = _cs.reconstruct_spend([r], NOW)
        assert spend["30d"] > 0


# ── Order attribution (SHC key comment) ─────────────────────────────────

class TestParseOrderTag:
    KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIM/CoI0W macbook@old-host"

    def test_prefers_shc_order_tag(self):
        tagged = _cs.parse_order_tag(self.KEY + " #shc-order=opencode:ses_9")
        assert tagged == "opencode:ses_9"

    def test_falls_back_to_raw_comment(self):
        assert _cs.parse_order_tag(self.KEY) == "macbook@old-host"

    def test_keyless_and_garbage_return_empty(self):
        assert _cs.parse_order_tag("") == ""
        assert _cs.parse_order_tag("not a key at all") == ""

    def test_bare_key_no_comment_returns_empty(self):
        assert _cs.parse_order_tag("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAabc") == ""


# ── Reaper env export ────────────────────────────────────────────────────

class TestReaperExport:
    def test_anchors_stripped_comma_joined(self):
        rules = [Rule(pattern="^tollgate-main-$", labels={}, reason=""),
                 Rule(pattern="^europa-vpn-vps$", labels={}, reason="")]
        out = _cs.export_reaper_env(rules)
        assert out == "export SHC_REAPER_EXTRA_KEEP_PATTERNS='tollgate-main-,europa-vpn-vps'"

    def test_label_only_rules_skipped(self):
        rules = [Rule(pattern=None, labels={"team": "core"}, reason="")]
        assert _cs.export_reaper_env(rules).endswith("=''")
