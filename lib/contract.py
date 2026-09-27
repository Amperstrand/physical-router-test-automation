"""Contract-driven test assertions.

Reads config/behavior-contract.json and provides helpers for asserting
against the contract. Tests use these instead of hardcoded values, so
when behavior changes, the contract changes in the same PR.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_CONTRACT_PATH = Path(__file__).parent.parent / "config" / "behavior-contract.json"
_contract_cache = None


def get_contract() -> dict:
    """Load and cache the behavior contract."""
    global _contract_cache
    if _contract_cache is None:
        with open(_CONTRACT_PATH) as f:
            _contract_cache = json.load(f)
    return _contract_cache


def portal_port(name: str = "portal_assets") -> int:
    return get_contract()["portal"]["ports"][name]


def backend_port() -> int:
    return portal_port("backend_api")


def response_kind(name: str = "response_success") -> int:
    return get_contract()["payment"][name]


def ssid_prefix() -> str:
    return get_contract()["wireless"]["ssid_prefix"]


def probe_host() -> str:
    return get_contract()["internet"]["probe_host"]


def min_token_sats() -> int:
    return get_contract()["payment"]["min_token_sats"]
