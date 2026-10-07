"""Unit tests for Makefile → pytest migration registry."""

from lib.migration_registry import get_entry, load_registry


import pytest

pytestmark = [pytest.mark.story("P11")]

def test_registry_loads():
    reg = load_registry()
    assert "smoke-degraded" in reg
    assert reg["smoke-degraded"].is_migrated


def test_smoke_degraded_points_at_scenario():
    entry = get_entry("smoke-degraded")
    assert entry is not None
    assert "test_mint_health.py" in entry.pytest
    assert "test_full_degraded_lifecycle" in entry.pytest


def test_ssl_comprehensive_uses_go_cli_module():
    entry = get_entry("test-ssl-comprehensive")
    assert entry is not None
    assert "test_ssl_go_cli.py" in entry.pytest
