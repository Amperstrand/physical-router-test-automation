"""Unit tests for lib/cashu_fixture.py — auto-minting fixture resolution and cleanup.

These run without a mint or router: they exercise the parameter resolution
logic (_resolve_amount, _resolve_mint_url), MintedToken bookkeeping, and
cleanup_token teardown paths with mock request and minter objects.
"""
from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import pytest

from lib.cashu_fixture import (
    DEFAULT_MINT_AMOUNT,
    MintedToken,
    _resolve_amount,
    _resolve_mint_url,
    cleanup_token,
)
from lib.cashu import _encode_c


# --------------------------------------------------------------------------- #
# Mock request / minter helpers
# --------------------------------------------------------------------------- #


class FakeMarker:
    def __init__(self, args):
        self.args = args


class FakeNode:
    def __init__(self, markers=None):
        self._markers = markers or {}

    def get_closest_marker(self, name):
        return self._markers.get(name)


class FakeRequest:
    """Stands in for pytest's FixtureRequest in unit tests."""

    def __init__(self, param=None, markers=None, fixture_value=None):
        self.param = param
        self.node = FakeNode(markers or {})
        self._fixture_value = fixture_value

    def getfixturevalue(self, name):
        if name == "cashu" and self._fixture_value is not None:
            return self._fixture_value
        raise Exception(f"fixture {name} not available")


from lib.cashu import CdkCliWallet, TokenPool


class FakeCdkWallet(CdkCliWallet):
    """CdkCliWallet subclass with __init__ overridden to avoid CLI lookup."""

    def __init__(self, work_dir, mint_url="https://testnut.cashu.exchange"):
        self.mint_url = mint_url
        self._cli = "/bin/true"
        self._work_dir = work_dir


class FakeTokenPool(TokenPool):
    """TokenPool subclass with __init__ overridden to avoid real minter."""

    def __init__(self, inner_minter):
        self._minter = inner_minter
        self._minter_url = getattr(inner_minter, "mint_url", "<unknown>")


class FakeMinterNoWorkDir:
    """Stands in for HttpMinter — no _work_dir, not a CashuMint/CdkCliWallet."""

    def __init__(self, mint_url="https://testnut.cashu.exchange"):
        self.mint_url = mint_url


# --------------------------------------------------------------------------- #
# _resolve_amount
# --------------------------------------------------------------------------- #


class TestResolveAmount:
    def test_indirect_int(self):
        req = FakeRequest(param=8)
        assert _resolve_amount(req) == 8

    def test_indirect_dict_with_amount(self):
        req = FakeRequest(param={"amount": 12})
        assert _resolve_amount(req) == 12

    def test_indirect_dict_without_amount(self):
        """Dict without 'amount' key falls through to other sources."""
        req = FakeRequest(param={"mint": "https://example.com"})
        assert _resolve_amount(req) == DEFAULT_MINT_AMOUNT

    def test_marker(self):
        req = FakeRequest(markers={"mint_amount": FakeMarker([6])})
        assert _resolve_amount(req) == 6

    def test_env_var(self, monkeypatch):
        monkeypatch.setenv("TOLLGATE_MINT_AMOUNT", "20")
        req = FakeRequest()
        assert _resolve_amount(req) == 20

    def test_default(self, monkeypatch):
        monkeypatch.delenv("TOLLGATE_MINT_AMOUNT", raising=False)
        req = FakeRequest()
        assert _resolve_amount(req) == DEFAULT_MINT_AMOUNT

    def test_indirect_takes_precedence_over_marker(self):
        req = FakeRequest(param=8, markers={"mint_amount": FakeMarker([6])})
        assert _resolve_amount(req) == 8

    def test_indirect_takes_precedence_over_env(self, monkeypatch):
        monkeypatch.setenv("TOLLGATE_MINT_AMOUNT", "20")
        req = FakeRequest(param=2)
        assert _resolve_amount(req) == 2

    def test_marker_takes_precedence_over_env(self, monkeypatch):
        monkeypatch.setenv("TOLLGATE_MINT_AMOUNT", "20")
        req = FakeRequest(markers={"mint_amount": FakeMarker([3])})
        assert _resolve_amount(req) == 3

    def test_indirect_dict_amount_string_converted(self):
        req = FakeRequest(param={"amount": "16"})
        assert _resolve_amount(req) == 16

    def test_env_string_converted(self, monkeypatch):
        monkeypatch.setenv("TOLLGATE_MINT_AMOUNT", "32")
        req = FakeRequest()
        assert _resolve_amount(req) == 32

    def test_marker_no_args_falls_through(self):
        """Marker with no args falls through to env/default."""
        req = FakeRequest(markers={"mint_amount": FakeMarker(())})
        assert _resolve_amount(req) == DEFAULT_MINT_AMOUNT


# --------------------------------------------------------------------------- #
# _resolve_mint_url
# --------------------------------------------------------------------------- #


class TestResolveMintUrl:
    def test_marker(self):
        req = FakeRequest(markers={"mint_url": FakeMarker(["https://custom.mint"])})
        assert _resolve_mint_url(req) == "https://custom.mint"

    def test_env_var(self, monkeypatch):
        monkeypatch.setenv("TOLLGATE_TEST_MINT_URL", "https://env.mint")
        req = FakeRequest()
        assert _resolve_mint_url(req) == "https://env.mint"

    def test_default(self, monkeypatch):
        monkeypatch.delenv("TOLLGATE_TEST_MINT_URL", raising=False)
        req = FakeRequest()
        result = _resolve_mint_url(req)
        # Default is TEST_MINT_URL from lib.constants, which itself reads the env
        assert isinstance(result, str)
        assert result.startswith("http")

    def test_marker_takes_precedence_over_env(self, monkeypatch):
        monkeypatch.setenv("TOLLGATE_TEST_MINT_URL", "https://env.mint")
        req = FakeRequest(markers={"mint_url": FakeMarker(["https://marker.mint"])})
        assert _resolve_mint_url(req) == "https://marker.mint"

    def test_marker_no_args_falls_through(self, monkeypatch):
        monkeypatch.delenv("TOLLGATE_TEST_MINT_URL", raising=False)
        req = FakeRequest(markers={"mint_url": FakeMarker(())})
        result = _resolve_mint_url(req)
        assert isinstance(result, str)


# --------------------------------------------------------------------------- #
# MintedToken dataclass
# --------------------------------------------------------------------------- #


class TestMintedToken:
    def test_defaults(self):
        mt = MintedToken(token="cashuA123", amount=4, mint_url="https://mint", minter=None)
        assert mt.token == "cashuA123"
        assert mt.amount == 4
        assert mt.mint_url == "https://mint"
        assert mt.consumed is False
        assert mt.minted_at > 0

    def test_mark_consumed(self):
        mt = MintedToken(token="cashuA123", amount=4, mint_url="https://mint", minter=None)
        assert mt.consumed is False
        mt.mark_consumed()
        assert mt.consumed is True

    def test_minted_at_is_monotonic(self):
        before = time.monotonic()
        mt = MintedToken(token="x", amount=1, mint_url="u", minter=None)
        after = time.monotonic()
        assert before <= mt.minted_at <= after

    def test_each_instance_unique_timestamp(self):
        mt1 = MintedToken(token="x", amount=1, mint_url="u", minter=None)
        time.sleep(0.001)
        mt2 = MintedToken(token="y", amount=1, mint_url="u", minter=None)
        assert mt2.minted_at >= mt1.minted_at


# --------------------------------------------------------------------------- #
# cleanup_token
# --------------------------------------------------------------------------- #


class TestCleanupToken:
    def test_consumed_token_logs_info(self, caplog):
        minter = FakeMinterNoWorkDir()
        mt = MintedToken(token="cashuA123", amount=4, mint_url="https://mint", minter=minter)
        mt.mark_consumed()
        with caplog.at_level("INFO", logger="tollgate.cashu_fixture"):
            cleanup_token(mt)
        assert any("consumed by test" in r.message for r in caplog.records)

    def test_unconsumed_token_logs_warning(self, caplog):
        minter = FakeMinterNoWorkDir()
        mt = MintedToken(token="cashuA123", amount=4, mint_url="https://mint", minter=minter)
        with caplog.at_level("WARNING", logger="tollgate.cashu_fixture"):
            cleanup_token(mt)
        assert any("NOT consumed" in r.message for r in caplog.records)

    def test_work_dir_removed_for_cdk_wallet(self, tmp_path):
        """When minter has _work_dir, it is rmtree'd at teardown."""
        work_dir = tmp_path / "cdk-session"
        work_dir.mkdir()
        (work_dir / "wallet.db").write_text("fake")

        minter = FakeCdkWallet(work_dir=str(work_dir))
        mt = MintedToken(token="cashuA123", amount=4, mint_url="https://mint", minter=minter)
        mt.mark_consumed()
        cleanup_token(mt)

        assert not work_dir.exists()

    def test_no_work_dir_no_crash(self):
        """Minters without _work_dir (HttpMinter) don't crash cleanup."""
        minter = FakeMinterNoWorkDir()
        mt = MintedToken(token="cashuA123", amount=4, mint_url="https://mint", minter=minter)
        mt.mark_consumed()
        # Should not raise
        cleanup_token(mt)

    @patch("lib.cashu_fixture.shutil.rmtree")
    def test_cashu_mint_no_work_dir_is_noop(self, mock_rmtree):
        """CashuMint doesn't have _work_dir, so rmtree is never called."""
        from lib.cashu import CashuMint
        minter = MagicMock(spec=CashuMint)
        # CashuMint doesn't have _work_dir attribute
        mt = MintedToken(token="x", amount=4, mint_url="u", minter=minter)
        mt.mark_consumed()
        cleanup_token(mt)
        mock_rmtree.assert_not_called()

    def test_token_pool_unwrapped(self, tmp_path):
        """TokenPool wrapping a CdkCliWallet is unwrapped to reach _work_dir."""
        work_dir = tmp_path / "pool-session"
        work_dir.mkdir()
        (work_dir / "state").write_text("fake")

        inner = FakeCdkWallet(work_dir=str(work_dir))
        pool = FakeTokenPool(inner)

        mt = MintedToken(token="cashuA123", amount=4, mint_url="https://mint", minter=pool)
        mt.mark_consumed()
        cleanup_token(mt)

        assert not work_dir.exists()

    def test_rmtree_ignore_errors(self, tmp_path):
        """rmtree with ignore_errors=True doesn't crash on missing dir."""
        work_dir = str(tmp_path / "nonexistent")
        minter = FakeCdkWallet(work_dir=work_dir)
        mt = MintedToken(token="x", amount=4, mint_url="u", minter=minter)
        mt.mark_consumed()
        # Should not raise even though dir doesn't exist
        cleanup_token(mt)


class TestEncodeC:
    """_encode_c dialect knob: hex (backend) default, base64 (portal) opt-in."""

    POINT = bytes([2]) + bytes(range(1, 33))  # 33-byte compressed point

    def test_default_is_hex(self, monkeypatch):
        monkeypatch.delenv("TOLLGATE_TOKEN_C_BASE64", raising=False)
        assert _encode_c(self.POINT) == self.POINT.hex()

    def test_base64_opt_in(self, monkeypatch):
        monkeypatch.setenv("TOLLGATE_TOKEN_C_BASE64", "1")
        import base64 as _b64
        assert _encode_c(self.POINT) == _b64.b64encode(self.POINT).decode()

    def test_hex_form_round_trips_to_33_bytes(self, monkeypatch):
        monkeypatch.delenv("TOLLGATE_TOKEN_C_BASE64", raising=False)
        assert len(bytes.fromhex(_encode_c(self.POINT))) == 33
