"""Bounded self-destruct gating (lesson 23) in the SHC submit path."""

from __future__ import annotations

from pathlib import Path

import pytest

import lib.cloud_lab.shc_submit as shc_submit


pytestmark = [pytest.mark.story("P11")]

def _script(legacy_killswitch: bool) -> str:
    return shc_submit._build_bootstrap_script(
        bootstrap_env="TOLLGATE_SERVICE_ID=123",
        overlay_b64="",
        test_dir="/t",
        suite_repo_url="https://example.invalid/x",
        lease_minutes=90,
        legacy_killswitch=legacy_killswitch,
    )


class TestBootstrapScriptKillswitchGating:
    def test_legacy_keeps_inline_switch(self):
        s = _script(legacy_killswitch=True)
        assert "self_cancel" in s
        assert "Scheduling self-cancel" in s

    def test_bounded_omits_inline_switch_entirely(self):
        s = _script(legacy_killswitch=False)
        assert "shc-self-destruct.timer (planted separately)" in s
        assert "self_cancel" not in s
        assert "Scheduling self-cancel" not in s


class TestArmSelfdestructContract:
    """Pins the contract between our hardcoded env guards and the toolkit."""

    def test_env_names_match_toolkit_constants(self):
        import inspect

        from shc_toolkit.selfdestruct import (
            ACCOUNT_EMAIL_ENV,
            ACCOUNT_PASSWORD_ENV,
            SUICIDE_KEY_ENV,
        )

        for name in (SUICIDE_KEY_ENV, ACCOUNT_EMAIL_ENV, ACCOUNT_PASSWORD_ENV):
            assert name in inspect.getsource(shc_submit), (
                f"{name} missing from shc_submit — guard drifted from toolkit contract"
            )
            assert name in Path("scripts/shc-run-baked.py").read_text(), (
                f"{name} missing from shc-run-baked.py — guard drifted from toolkit contract"
            )

    def test_no_source_raises_runtime_error(self, monkeypatch):
        from shc_toolkit.selfdestruct import arm_self_destruct

        for var in ("SHC_SUICIDE_KEY", "SHC_ACCOUNT_EMAIL", "SHC_ACCOUNT_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(RuntimeError, match="SHC_SUICIDE_KEY"):
            arm_self_destruct(lambda cmd: "", 123, 90)
