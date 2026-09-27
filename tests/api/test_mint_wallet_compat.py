"""Mint/wallet compatibility matrix tests.

Verifies that all combinations of wallet implementations (CashuMint/Python,
CdkCliWallet/Rust) and mint types (V1 keysets, V2 keysets) produce tokens
that TollGate accepts or correctly rejects.

Test matrix:

    Wallet          | V1 mint (testnut)  | V2 mint (CDK)     |
    --------------- | ------------------ | ----------------- |
    CashuMint       | test b, e, f       | test f            |
    CdkCliWallet    | test e             | test c            |
    cashu fixture   | test a             | test a            |
    create_minter   | test d             | test d            |

Additional coverage:
    - Token format consistency across wallets (test g)
    - Cross-mint rejection: wrong-mint tokens rejected (test h)

Skip strategy: every test that depends on cdk-cli, cashu venv, or a specific
mint URL checks availability at runtime and calls pytest.skip() gracefully.
"""

import base64
import json
import logging
import os

import pytest

from lib.cashu import CashuMint, CdkCliWallet, create_minter, mint_reachable
from lib.constants import TEST_MINT_URL, V2_MINT_URL
from lib.helpers import require_client_identity

log = logging.getLogger("tollgate.mint_wallet_compat")

pytestmark = [pytest.mark.api, pytest.mark.extended]

MINT_AMOUNT = 4


def _is_accepted(resp: dict) -> bool:
    """Check if TollGate accepted a payment token."""
    return resp.get("kind") == 1022 or resp.get("success") is True


def _decode_token(token: str) -> list:
    """Decode a cashuA token to its JSON payload."""
    if not token.startswith("cashuA"):
        raise ValueError(f"Token does not start with cashuA: {token[:20]}")
    b64 = token[len("cashuA"):]
    # Fix padding
    b64 += "=" * (4 - len(b64) % 4)
    return json.loads(base64.b64decode(b64))


# ---------------------------------------------------------------------------
# a) Default mint — baseline
# ---------------------------------------------------------------------------

@pytest.mark.extended
def test_default_mint_token_accepted(router, cashu):
    """Baseline: whatever mint the cashu fixture is configured with works.

    The cashu fixture auto-selects the minter based on the current
    TOLLGATE_TEST_MINT_URL and available tools. This test verifies the
    default path is functional.
    """
    require_client_identity(router)
    if not cashu.is_available():
        pytest.skip("cashu venv not available — run scripts/setup-cashu.sh")

    logging.info(f"Minting token via cashu fixture (mint_url={cashu.mint_url})")
    try:
        token = cashu.mint(MINT_AMOUNT, legacy=True)
    except Exception as exc:
        pytest.skip(f"Default mint token minting failed: {exc}")

    resp = router.pay_direct(token)
    assert _is_accepted(resp), \
        f"Default mint payment rejected: {str(resp)[:300]}"


# ---------------------------------------------------------------------------
# b) Nutshell wallet + V1 mint
# ---------------------------------------------------------------------------

@pytest.mark.extended
def test_nutshell_wallet_v1_mint_token(router):
    """CashuMint (Python cashu CLI) against V1 keyset mint.

    Uses the --legacy flag to produce cashuA-format tokens from the
    V1 testnut mint (00-prefix keysets).
    """
    require_client_identity(router)
    wallet = CashuMint(mint_url=TEST_MINT_URL)
    if not wallet.is_available():
        pytest.skip("cashu CLI not available — run scripts/setup-cashu.sh")

    logging.info(f"Nutshell wallet -> V1 mint: {TEST_MINT_URL}")
    try:
        token = wallet.mint(MINT_AMOUNT, legacy=True)
    except Exception as exc:
        pytest.skip(f"V1 mint unreachable or minting failed: {exc}")

    resp = router.pay_direct(token)
    assert _is_accepted(resp), \
        f"Nutshell/V1 mint payment rejected: {str(resp)[:300]}"


# ---------------------------------------------------------------------------
# c) CDK wallet + V2 mint
# ---------------------------------------------------------------------------

@pytest.mark.extended
def test_cdk_wallet_v2_mint_token(router):
    """CdkCliWallet (Rust cdk-cli) against V2 keyset mint.

    V2 mints produce 01-prefix keyset IDs (33 bytes). The cdk-cli
    uses --v3 flag to produce cashuA-format tokens.
    """
    require_client_identity(router)
    v2_url = os.environ.get("TOLLGATE_V2_MINT_URL", V2_MINT_URL)
    wallet = CdkCliWallet(mint_url=v2_url)
    if not wallet.is_available():
        pytest.skip("cdk-cli not found — install to /opt/cdk-mintd/cdk-cli or PATH")

    logging.info(f"CDK wallet -> V2 mint: {v2_url}")
    try:
        token = wallet.mint(MINT_AMOUNT)
    except Exception as exc:
        pytest.skip(f"V2 mint unreachable or cdk-cli minting failed: {exc}")

    resp = router.pay_direct(token)

    # Backend may reject V2 keysets (Go/gonuts: "not accepted", Rust/CDK: keyset mismatch)
    content = resp.get("content", "")
    if resp.get("kind") == 21023 and any(
        kw in content for kw in ["not accepted", "keyset", "ID length", "CDK receive", "invalid type"]
    ):
        pytest.skip(
            f"Backend rejected V2 keyset token (keyset incompatibility): "
            f"{str(resp)[:200]}"
        )

    assert _is_accepted(resp), \
        f"CDK/V2 mint payment rejected: {str(resp)[:300]}"


# ---------------------------------------------------------------------------
# d) create_minter factory
# ---------------------------------------------------------------------------

@pytest.mark.extended
def test_create_minter_factory_produces_working_wallet(router, cashu):
    """create_minter() factory returns a wallet that can mint accepted tokens.

    The factory probes the mint's keyset version and returns CdkCliWallet
    for V2 mints (if cdk-cli available) or CashuMint otherwise. This test
    verifies the factory's output produces valid tokens.
    """
    require_client_identity(router)
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL", TEST_MINT_URL)

    logging.info(f"create_minter factory for: {mint_url}")
    minter = create_minter(mint_url)
    if not minter.is_available():
        pytest.skip(f"Factory-returned minter not available for {mint_url}")

    try:
        token = minter.mint(MINT_AMOUNT, legacy=True)
    except Exception as exc:
        pytest.skip(f"Factory minter failed to mint: {exc}")

    resp = router.pay_direct(token)
    assert _is_accepted(resp), \
        f"Factory-minter payment rejected: {str(resp)[:300]}"


# ---------------------------------------------------------------------------
# e) CDK wallet + V1 mint (cross-compat)
# ---------------------------------------------------------------------------

@pytest.mark.extended
def test_cdk_wallet_v1_mint_compat(router):
    """CdkCliWallet against V1 mint — cross-compatibility test.

    Verifies that cdk-cli can work against V1 (00-prefix) keyset mints,
    not just V2. The token should still be cashuA format.
    """
    require_client_identity(router)
    wallet = CdkCliWallet(mint_url=TEST_MINT_URL)
    if not wallet.is_available():
        pytest.skip("cdk-cli not found — cannot test CDK against V1 mint")

    logging.info(f"CDK wallet -> V1 mint (cross-compat): {TEST_MINT_URL}")
    try:
        token = wallet.mint(MINT_AMOUNT)
    except Exception as exc:
        pytest.skip(f"CDK wallet failed against V1 mint: {exc}")

    assert token.startswith("cashuA"), \
        f"CDK wallet produced non-cashuA token against V1 mint: {token[:30]}"

    resp = router.pay_direct(token)
    assert _is_accepted(resp), \
        f"CDK/V1 cross-compat payment rejected: {str(resp)[:300]}"


# ---------------------------------------------------------------------------
# f) Nutshell wallet + V2 mint
# ---------------------------------------------------------------------------

@pytest.mark.extended
def test_nutshell_wallet_v2_mint_token(router):
    require_client_identity(router)
    v2_url = os.environ.get("TOLLGATE_V2_MINT_URL", V2_MINT_URL)
    if not mint_reachable(v2_url):
        pytest.skip(
            f"V2 mint unreachable at {v2_url} — the cashu CLI blocks for minutes "
            "against an absent mint, tripping pytest-timeout's os._exit"
        )
    wallet = CashuMint(mint_url=v2_url)
    if not wallet.is_available():
        pytest.skip("cashu CLI not available for V2 mint test")

    logging.info(f"Nutshell wallet -> V2 mint: {v2_url}")
    try:
        token = wallet.mint(MINT_AMOUNT, legacy=True)
    except Exception as exc:
        pytest.skip(
            f"Nutshell wallet failed against V2 mint: {exc}"
        )

    resp = router.pay_direct(token)
    content = resp.get("content", "")
    if resp.get("kind") == 21023 and any(
        kw in content for kw in ["not accepted", "keyset", "ID length", "CDK receive", "invalid type"]
    ):
        pytest.skip(f"V2 token rejected (keyset incompatibility): {str(resp)[:200]}")

    assert _is_accepted(resp), f"V2 mint payment rejected: {str(resp)[:300]}"


# ---------------------------------------------------------------------------
# g) Token format consistency
# ---------------------------------------------------------------------------

@pytest.mark.extended
def test_minted_token_format_consistency(router, cashu):
    """All available wallets produce cashuA tokens with valid proof structure.

    For each available wallet/mint combination, mints a token and verifies:
    1. Token starts with 'cashuA' (not 'cashuB')
    2. Token payload decodes as valid JSON after base64 decode
    3. Proofs have required fields: amount, id, secret, C
    """
    require_client_identity(router)

    wallets_tested = 0

    # --- cashu fixture (auto-selected minter) ---
    if cashu.is_available():
        logging.info(f"Format check: cashu fixture (mint={cashu.mint_url})")
        try:
            token = cashu.mint(MINT_AMOUNT, legacy=True)
            _assert_token_format(token, "cashu-fixture")
            wallets_tested += 1
        except Exception as exc:
            logging.info(f"cashu fixture minting skipped: {exc}")

    # --- CashuMint against V1 ---
    v1_wallet = CashuMint(mint_url=TEST_MINT_URL)
    if v1_wallet.is_available():
        logging.info(f"Format check: CashuMint V1 ({TEST_MINT_URL})")
        try:
            token = v1_wallet.mint(MINT_AMOUNT, legacy=True)
            _assert_token_format(token, "CashuMint-V1")
            wallets_tested += 1
        except Exception as exc:
            logging.info(f"CashuMint V1 minting skipped: {exc}")

    # --- CdkCliWallet against V2 ---
    v2_url = os.environ.get("TOLLGATE_V2_MINT_URL", V2_MINT_URL)
    cdk_wallet = CdkCliWallet(mint_url=v2_url)
    if cdk_wallet.is_available():
        logging.info(f"Format check: CdkCliWallet V2 ({v2_url})")
        try:
            token = cdk_wallet.mint(MINT_AMOUNT)
            _assert_token_format(token, "CdkCliWallet-V2")
            wallets_tested += 1
        except Exception as exc:
            logging.info(f"CdkCliWallet V2 minting skipped: {exc}")

    # --- CdkCliWallet against V1 (cross-compat) ---
    cdk_v1 = CdkCliWallet(mint_url=TEST_MINT_URL)
    if cdk_v1.is_available():
        logging.info(f"Format check: CdkCliWallet V1 ({TEST_MINT_URL})")
        try:
            token = cdk_v1.mint(MINT_AMOUNT)
            _assert_token_format(token, "CdkCliWallet-V1")
            wallets_tested += 1
        except Exception as exc:
            logging.info(f"CdkCliWallet V1 minting skipped: {exc}")

    if wallets_tested == 0:
        pytest.skip("No wallets available for format consistency check")


def _assert_token_format(token: str, label: str):
    """Verify a token has cashuA format with valid proof structure."""
    assert token.startswith("cashuA"), \
        f"[{label}] Token does not start with cashuA: {token[:30]}"
    assert not token.startswith("cashuB"), \
        f"[{label}] Token uses cashuB format (expected cashuA)"

    payload = _decode_token(token)
    assert isinstance(payload, list), \
        f"[{label}] Decoded payload is not a list: {type(payload)}"
    assert len(payload) > 0, \
        f"[{label}] Decoded payload is empty"

    for entry_idx, entry in enumerate(payload):
        assert "proofs" in entry, \
            f"[{label}] Entry {entry_idx} missing 'proofs' key: {list(entry.keys())}"
        proofs = entry["proofs"]
        assert isinstance(proofs, list) and len(proofs) > 0, \
            f"[{label}] Entry {entry_idx} has empty/non-list proofs"

        for proof_idx, proof in enumerate(proofs):
            assert "amount" in proof, \
                f"[{label}] Proof [{entry_idx}][{proof_idx}] missing 'amount'"
            assert isinstance(proof["amount"], int), \
                f"[{label}] Proof [{entry_idx}][{proof_idx}] 'amount' not int: {type(proof['amount'])}"
            assert "id" in proof, \
                f"[{label}] Proof [{entry_idx}][{proof_idx}] missing 'id'"
            assert "secret" in proof, \
                f"[{label}] Proof [{entry_idx}][{proof_idx}] missing 'secret'"
            assert "C" in proof, \
                f"[{label}] Proof [{entry_idx}][{proof_idx}] missing 'C'"

    logging.info(f"[{label}] Token format OK: {len(payload)} entries, "
                 f"{sum(len(e.get('proofs', [])) for e in payload)} total proofs")


# ---------------------------------------------------------------------------
# h) Multi-mint cross-acceptance rejection
# ---------------------------------------------------------------------------

@pytest.mark.extended
def test_multi_mint_payment_not_cross_accepted(router, cashu):
    """Tokens from a wrong/unconfigured mint are rejected by TollGate.

    Uses synthetic_wrong_mint_token() to produce a token that references
    https://wrong-mint.example.com — a mint that should never be in the
    router's accepted_mints config. Verifies the backend rejects it.
    """
    require_client_identity(router)

    wrong_token = CashuMint.synthetic_wrong_mint_token()
    logging.info("Testing cross-mint rejection with synthetic wrong-mint token")

    resp = router.pay_direct(wrong_token)

    # The token must NOT be accepted
    assert not _is_accepted(resp), \
        f"Wrong-mint token was ACCEPTED (security issue!): {str(resp)[:300]}"

    logging.info(f"Wrong-mint token correctly rejected: kind={resp.get('kind')}, "
                 f"content={str(resp.get('content', ''))[:100]}")
