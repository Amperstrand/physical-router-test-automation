"""Unit tests for lib/router_env.py — env file parsing and router label resolution."""
from __future__ import annotations

import os
import pytest
from pathlib import Path

from lib.router_env import (
    _parse_env_file,
    router_prefix,
    apply_cli_overrides,
    resolve_secondary_for_two_router,
)


pytestmark = [pytest.mark.story("P11")]

class TestParseEnvFile:
    def test_simple_key_value(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text("ROUTER_ALPHA_HOST=192.168.1.1\nROUTER_ALPHA_PASS=secret\n")
        result = _parse_env_file(f)
        assert result["ROUTER_ALPHA_HOST"] == "192.168.1.1"
        assert result["ROUTER_ALPHA_PASS"] == "secret"

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.env"
        f.write_text("")
        assert _parse_env_file(f) == {}

    def test_ignores_comments(self, tmp_path):
        f = tmp_path / "comments.env"
        f.write_text("# comment\nROUTER_HOST=1.2.3.4\n# another\n")
        result = _parse_env_file(f)
        assert result == {"ROUTER_HOST": "1.2.3.4"}

    def test_ignores_blank_lines(self, tmp_path):
        f = tmp_path / "blanks.env"
        f.write_text("\nROUTER_HOST=1.2.3.4\n\n")
        result = _parse_env_file(f)
        assert result == {"ROUTER_HOST": "1.2.3.4"}

    def test_quoted_values(self, tmp_path):
        f = tmp_path / "quoted.env"
        f.write_text('PASS="my pass"\nHOST=1.2.3.4\n')
        result = _parse_env_file(f)
        assert "my pass" in result.get("PASS", "")


class TestRouterPrefix:
    def test_alpha(self):
        assert router_prefix("alpha") == "ROUTER_ALPHA"

    def test_beta(self):
        assert router_prefix("beta") == "ROUTER_BETA"

    def test_lab_router_a(self):
        assert router_prefix("lab-router-a") == "ROUTER_LAB-ROUTER-A"

    def test_uppercase_input(self):
        assert router_prefix("ALPHA") == "ROUTER_ALPHA"


class TestApplyCliOverrides:
    def test_ssid_override(self, monkeypatch):
        monkeypatch.delenv("TOLLGATE_UPSTREAM_WIFI_SSID", raising=False)
        apply_cli_overrides(ssid="MyNetwork")
        assert os.environ["TOLLGATE_UPSTREAM_WIFI_SSID"] == "MyNetwork"

    def test_password_override(self, monkeypatch):
        monkeypatch.delenv("TOLLGATE_UPSTREAM_WIFI_PASSWORD", raising=False)
        apply_cli_overrides(password="secret123")
        assert os.environ["TOLLGATE_UPSTREAM_WIFI_PASSWORD"] == "secret123"

    def test_no_overrides(self):
        apply_cli_overrides()
