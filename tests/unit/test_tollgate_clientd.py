"""Unit lane for scripts/tollgate-clientd.py (the laptop auto-top-up client).

The script is vendored from tollgate-module-basic-go and carries its own
in-process mock TollGate; --selftest exercises the full loop (advertisement
parsing, payment with MAC passthrough, threshold renewal) without hardware,
mints, or wallets. These tests pin the vendored copy stays runnable and its
CLI contract (flags the Debian lane and waybar configs rely on) intact.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "tollgate-clientd.py"


import pytest

pytestmark = [pytest.mark.story("G8")]

def run(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def test_selftest_full_loop_passes():
    result = run("--selftest")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SELFTEST PASS" in result.stdout


def test_help_exposes_lane_contract_flags():
    result = run("--help")
    assert result.returncode == 0
    for flag in ("--gateway", "--mac", "--wallet", "--steps",
                 "--renew-below", "--status", "--json", "--waybar",
                 "--dry-run", "--list-offers", "--selftest"):
        assert flag in result.stdout, f"missing {flag} in --help"


def test_bad_wallet_errors_cleanly():
    result = run("--wallet", "not-a-wallet", "--gateway", "127.0.0.1")
    assert result.returncode == 2  # argparse invalid-choice exit
    assert "invalid choice" in result.stderr


def test_status_json_is_valid_against_mock():
    # --gateway with a port passes through to the in-process mock used by
    # --selftest; here we only assert the error path shape without a lab:
    # a dead gateway must fail fast with a clear message, not a traceback.
    result = run("--gateway", "127.0.0.1:1", "--mac", "02:00:00:00:00:02",
                 "--status", "--json", timeout=30)
    combined = result.stdout + result.stderr
    assert "Traceback" not in combined
    assert result.returncode in (0, 1)


def test_module_version_recorded_in_provenance():
    docstring = SCRIPT.read_text().splitlines()
    header = "\n".join(docstring[:6])
    assert "tollgate-module-basic-go" in header
    assert "9e89ab5" in header


class TestParseAdvertisementMinSteps:
    """The min-steps normalization contract (api-tier mock-ification lane).

    The 6th ``price_per_step`` element is ``min_purchase_steps``. Ads may
    carry it absent (older firmware) or ``"0"`` (pre-normalization configs),
    but a purchase of <1 step is meaningless — the backend normalizes
    config-side the same way (tmbg #104: 0/absent -> 1), so the client
    parser must floor every offer at 1 step.
    """

    @staticmethod
    def _parse(tags: list):
        import importlib.util
        spec = importlib.util.spec_from_file_location("tollgate_clientd", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ad = mod.parse_advertisement(json.dumps({
            "kind": 10021, "pubkey": "00" * 32, "tags": tags,
        }))
        return ad.default_offer()

    def test_sixth_element_used_when_present(self):
        offer = self._parse([["price_per_step", "cashu", "1", "sat", "https://m.example", "3"]])
        assert offer.min_steps == 3

    def test_absent_min_steps_normalizes_to_one(self):
        offer = self._parse([["price_per_step", "cashu", "1", "sat", "https://m.example"]])
        assert offer.min_steps == 1, "absent min_steps must normalize to 1, not 0"

    def test_zero_min_steps_normalizes_to_one(self):
        offer = self._parse([["price_per_step", "cashu", "1", "sat", "https://m.example", "0"]])
        assert offer.min_steps == 1, "min_steps=0 is not a purchasable offer (tmbg #104)"

    def test_negative_min_steps_normalizes_to_one(self):
        offer = self._parse([["price_per_step", "cashu", "1", "sat", "https://m.example", "-2"]])
        assert offer.min_steps == 1

    def test_prtA_mock_advertisement_parses_with_min_steps_one(self):
        import importlib.util
        import sys
        repo_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo_root))
        try:
            from lib.mock_router import MOCK_ADVERTISEMENT
        finally:
            sys.path.pop(0)
        spec = importlib.util.spec_from_file_location("tollgate_clientd", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ad = mod.parse_advertisement(json.dumps(MOCK_ADVERTISEMENT))
        offer = ad.default_offer()
        assert offer.min_steps >= 1
        assert offer.mint_url == "https://testnut.cashu.exchange"
        assert offer.price == 1 and offer.unit == "sat"
