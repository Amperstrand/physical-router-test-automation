"""Unit tests for scripts/mint_allowlist_rpc.py (pure helpers)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "scripts" / "mint_allowlist_rpc.py"


import pytest

pytestmark = [pytest.mark.story("P11")]

def _load():
    spec = importlib.util.spec_from_file_location("mint_allowlist_rpc", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mod = _load()

# Known vector: pubkey hex -> npub (derive with `nak encode npub <hex>`).
PK = "4dbb8879162d779166c1cf59fc3339d54e316076b16ff626e80b8f5943ba6fb9"
NPUB = "npub1fkacs7gk94mezekpeavlcvee648rzcrkk9hlvfhgpw84jsa6d7usgsavvq"


def test_pubkey_hex_to_npub_vector():
    assert mod.pubkey_hex_to_npub(PK) == NPUB


def _event(pk=PK, ts=None, kind=27235, tags=None):
    return {"kind": kind, "pubkey": pk,
            "created_at": int(ts if ts is not None else __import__("time").time()),
            "tags": tags if tags is not None else [["u", "http://x/mark-paid"],
                                                   ["method", "POST"]],
            "content": "", "id": "0" * 64, "sig": "0" * 128}


POL = {"allow_npubs": [NPUB], "max_age_s": 300, "future_skew_s": 60}


TS = 1_700_000_000


def test_nip98_allows_whitelisted():
    ok, why, npub = mod.nip98_ok(_event(ts=TS), "http://x/mark-paid", "POST",
                                 TS, POL, lambda e: True)
    assert ok and npub == NPUB


def test_nip98_rejects_non_whitelisted():
    other = "aa" * 32
    ok, why, _ = mod.nip98_ok(_event(pk=other), "http://x/mark-paid", "POST",
                              1_700_000_000, POL, lambda e: True)
    assert not ok and "not allowlisted" in why


def test_nip98_rejects_bad_signature():
    ok, why, _ = mod.nip98_ok(_event(ts=TS), "http://x/mark-paid", "POST",
                              TS, POL, lambda e: False)
    assert not ok and "signature" in why


def test_nip98_rejects_stale_and_wrong_kind():
    ts = 1_700_000_000
    ok, why, _ = mod.nip98_ok(_event(ts=ts), "http://x/mark-paid", "POST",
                              ts + 10_000, POL, lambda e: True)
    assert not ok and "stale" in why
    ok, why, _ = mod.nip98_ok(_event(kind=1), "http://x/mark-paid", "POST",
                              1_700_000_000, POL, lambda e: True)
    assert not ok and "kind" in why
