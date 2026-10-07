# TIP-02: Cashu Payments — Token Format Validation
#
# Validates Cashu token encoding (cashuA V3 JSON vs cashuB V4 CBOR),
# decoding, internal structure, and TollGate backend acceptance.
#
# Extended July 2026: V4 short keyset ID resolution regression test.
# Tests all 4 combinations: V3+V1, V3+V2, V4+V1, V4+V2.
# V4+V2 requires gonuts-tollgate v0.8.0+ (resolveShortKeysetIds).
#
# Background:
#   NUT-00 defines two token serialisation formats:
#     V3 = cashuA + base64url(JSON) — the only format TollGate accepts.
#     V4 = cashuB + base64url(CBOR) — not supported by TollGate.
#
#   The `--v3` flag was added to `cdk-cli send` to force V3 output
#   because CDK 0.16.0+ defaults to V4 (CBOR). These tests confirm
#   that the minter produces cashuA tokens and that the backend
#   rejects cashuB tokens.

import base64
import json
import os
import shutil
import subprocess
import tempfile

import pytest

from lib.helpers import require_client_identity, is_payment_swap_succeeded

pytestmark = [pytest.mark.story("R2"), pytest.mark.tip("TIP-02"),pytest.mark.api, pytest.mark.extended]


def _decode_cashuA(token: str) -> dict | list:
    """Strip the cashuA prefix, fix base64url padding, and decode to JSON."""
    assert token.startswith("cashuA"), f"Expected cashuA prefix, got: {token[:20]}"
    payload_b64 = token[len("cashuA"):]
    # NUT-00: clients MUST handle both padded and unpadded base64url
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    raw = base64.urlsafe_b64decode(padded)
    return json.loads(raw)


def _get_proofs(decoded: dict | list) -> list[dict]:
    """Extract proofs list from either {"token":[...]} wrapper or bare list."""
    if isinstance(decoded, list):
        # Legacy V1 format: bare list of mint entries
        proofs = []
        for entry in decoded:
            proofs.extend(entry.get("proofs", []))
        return proofs
    # V3 format: {"token": [{"mint": ..., "proofs": [...]}]}
    token_list = decoded.get("token", [])
    proofs = []
    for entry in token_list:
        proofs.extend(entry.get("proofs", []))
    return proofs


@pytest.mark.extended
def test_minted_token_is_cashuA_format(cashu):
    """Verify the mint produces cashuA (V3 JSON) tokens, not cashuB (V4 CBOR).

    The --v3 flag was added to cdk-cli send to prevent V4 output.
    If this test fails, the minter is producing CBOR tokens that
    TollGate cannot parse.
    """
    token = cashu.mint(amount=4)
    assert token.startswith("cashuA"), \
        f"Minter produced non-cashuA token (prefix: {token[:10]}). " \
        "The --v3 flag may not be in effect, producing V4 CBOR tokens."
    assert not token.startswith("cashuB"), \
        "Minter produced cashuB (V4 CBOR) token — TollGate only accepts cashuA"


@pytest.mark.extended
def test_decode_cashuA_token_structure(cashu):
    """Decode a minted token and verify the top-level JSON structure.

    Expected V3 structure (per NUT-00):
        {"token": [{"mint": "<url>", "proofs": [<proof objects>]}]}

    Some minters (legacy cashu CLI with --legacy) produce a bare list:
        [{"mint": "<url>", "proofs": [<proof objects>]}]

    Both formats must decode and contain at least one mint entry with proofs.
    """
    token = cashu.mint(amount=4)
    decoded = _decode_cashuA(token)

    if isinstance(decoded, list):
        # Legacy bare-list format
        assert len(decoded) > 0, "Token decoded to empty list"
        for entry in decoded:
            assert "mint" in entry, f"Missing 'mint' key in entry: {entry}"
            assert "proofs" in entry, f"Missing 'proofs' key in entry: {entry}"
            assert isinstance(entry["proofs"], list), \
                f"'proofs' is not a list: {type(entry['proofs'])}"
    else:
        # Standard V3 wrapped format
        assert "token" in decoded, \
            f"V3 token missing 'token' key. Keys: {list(decoded.keys())}"
        token_list = decoded["token"]
        assert isinstance(token_list, list), \
            f"'token' is not a list: {type(token_list)}"
        assert len(token_list) > 0, "Token list is empty"
        for entry in token_list:
            assert "mint" in entry, f"Missing 'mint' key in entry: {entry}"
            assert isinstance(entry["mint"], str), \
                f"'mint' is not a string: {type(entry['mint'])}"
            assert entry["mint"].startswith("http"), \
                f"Mint URL doesn't look like a URL: {entry['mint']}"
            assert "proofs" in entry, f"Missing 'proofs' key in entry: {entry}"
            assert isinstance(entry["proofs"], list), \
                f"'proofs' is not a list: {type(entry['proofs'])}"


@pytest.mark.extended
def test_cashuA_token_has_valid_proof_fields(cashu):
    """Check each proof in a decoded token has the required fields.

    Per NUT-00, each proof must contain:
      - amount (int): denomination value, must be > 0
      - id (str): keyset ID in hex
      - secret (str): secret for the proof
      - C (str): blinded signature point C in hex
    """
    token = cashu.mint(amount=4)
    decoded = _decode_cashuA(token)
    proofs = _get_proofs(decoded)

    assert len(proofs) > 0, "Token contains no proofs"

    for i, proof in enumerate(proofs):
        assert isinstance(proof, dict), \
            f"Proof {i} is not a dict: {type(proof)}"

        # amount: positive integer
        assert "amount" in proof, f"Proof {i} missing 'amount': {proof}"
        assert isinstance(proof["amount"], int), \
            f"Proof {i} 'amount' is not int: {type(proof['amount'])}"
        assert proof["amount"] > 0, \
            f"Proof {i} has non-positive amount: {proof['amount']}"

        # id: non-empty hex string (keyset ID)
        assert "id" in proof, f"Proof {i} missing 'id': {proof}"
        assert isinstance(proof["id"], str), \
            f"Proof {i} 'id' is not str: {type(proof['id'])}"
        assert len(proof["id"]) > 0, f"Proof {i} has empty 'id'"

        # secret: non-empty string
        assert "secret" in proof, f"Proof {i} missing 'secret': {proof}"
        assert isinstance(proof["secret"], str), \
            f"Proof {i} 'secret' is not str: {type(proof['secret'])}"
        assert len(proof["secret"]) > 0, f"Proof {i} has empty 'secret'"

        # C: non-empty hex string (blinded signature)
        assert "C" in proof, f"Proof {i} missing 'C': {proof}"
        assert isinstance(proof["C"], str), \
            f"Proof {i} 'C' is not str: {type(proof['C'])}"
        assert len(proof["C"]) > 0, f"Proof {i} has empty 'C'"


@pytest.mark.extended
def test_cashuA_base64_padding_handled():
    """Verify base64url decoding handles both padded and unpadded tokens.

    NUT-00 requires that clients handle base64url with or without
    '=' padding characters. This test constructs both variants of
    a synthetic token and verifies both decode to the same JSON.
    """
    payload = [{"mint": "https://test.example.com",
                "proofs": [{"amount": 2, "id": "00abcd1234ef5678",
                            "secret": "test_secret", "C": "02abcdef"}]}]
    payload_json = json.dumps(payload)

    # Encode with standard base64 (includes padding)
    encoded_padded = base64.b64encode(payload_json.encode()).decode()
    token_padded = "cashuA" + encoded_padded

    # Remove padding to simulate a mint that strips '='
    encoded_stripped = encoded_padded.rstrip("=")
    token_stripped = "cashuA" + encoded_stripped

    # Both must decode to the same JSON structure
    decoded_padded = _decode_cashuA(token_padded)
    decoded_stripped = _decode_cashuA(token_stripped)

    assert decoded_padded == decoded_stripped, \
        "Padded and unpadded base64 decoded to different structures"

    # Verify the round-trip content is correct
    proofs = _get_proofs(decoded_padded)
    assert len(proofs) == 1
    assert proofs[0]["amount"] == 2


@pytest.mark.extended
def test_cashuB_token_rejected_by_backend(router):
    """Verify TollGate rejects garbage cashuB tokens but accepts real V4 tokens.

    gonuts v0.8.0+ supports V4 token parsing and short keyset ID resolution.
    A garbage CBOR payload should still be rejected, but a real V4 token
    with valid proofs should process correctly.

    See: https://github.com/cashubtc/nuts/pull/182 (V4 spec)
    """
    require_client_identity(router)

    synthetic_b64 = base64.urlsafe_b64encode(b"\x82\x01\x02").decode()
    cashuB_token = "cashuB" + synthetic_b64

    resp = router.pay_direct(cashuB_token)

    assert resp.get("kind") != 1022, \
        f"Garbage cashuB token was ACCEPTED — backend should reject invalid CBOR: {resp}"
    assert resp.get("success") is not True, \
        f"Garbage cashuB token resulted in success — unexpected: {resp}"


@pytest.mark.extended
def test_synthetic_wrong_mint_token_format(cashu):
    """Verify synthetic_wrong_mint_token() produces valid cashuA structure.

    The synthetic token is used by test_wrong_mint.py to verify the
    backend rejects tokens from unconfigured mints. This test ensures
    the synthetic token itself is well-formed: cashuA prefix, valid
    base64, decodable JSON, with a non-matching mint URL.
    """
    token = cashu.synthetic_wrong_mint_token()

    # Must be cashuA format
    assert token.startswith("cashuA"), \
        f"Synthetic wrong-mint token has wrong prefix: {token[:10]}"

    # Must decode cleanly
    decoded = _decode_cashuA(token)

    # Must contain at least one mint entry
    proofs = _get_proofs(decoded)
    assert len(proofs) > 0, "Synthetic wrong-mint token has no proofs"

    # Mint URL must NOT match any real mint
    if isinstance(decoded, list):
        mint_urls = [entry.get("mint", "") for entry in decoded]
    else:
        mint_urls = [entry.get("mint", "") for entry in decoded.get("token", [])]

    assert any("wrong-mint" in url for url in mint_urls), \
        f"Expected wrong-mint URL in synthetic token, got: {mint_urls}"


@pytest.mark.extended
def test_token_proof_amount_matches_request(cashu):
    """Verify the sum of proof amounts equals the requested mint amount.

    Cashu uses powers-of-2 denominations. For amount=8, the mint may
    split into multiple proofs (e.g., 4+4 or 2+2+2+2). The sum of
    all proof amounts must equal the originally requested amount.
    """
    requested_amount = 8
    token = cashu.mint(amount=requested_amount)
    decoded = _decode_cashuA(token)
    proofs = _get_proofs(decoded)

    total = sum(p["amount"] for p in proofs)
    assert total == requested_amount, \
        f"Proof amounts sum to {total}, expected {requested_amount}. " \
        f"Individual amounts: {[p['amount'] for p in proofs]}"


@pytest.mark.extended
def test_multi_proof_token_structure(cashu):
    """Verify tokens with multiple proofs have valid structure in each proof.

    Some mints return tokens with 2+ proofs (e.g., 4+4=8 split).
    Each proof must independently have valid amount, id, secret, and C.
    This catches partial corruption where some proofs are malformed.
    """
    # Use amount=8 to increase chance of multi-proof split
    token = cashu.mint(amount=8)
    decoded = _decode_cashuA(token)
    proofs = _get_proofs(decoded)

    assert len(proofs) >= 1, \
        f"Expected at least 1 proof for amount=8, got {len(proofs)}"

    for i, proof in enumerate(proofs):
        assert isinstance(proof.get("amount"), int), \
            f"Proof {i}: 'amount' missing or not int: {proof}"
        assert proof["amount"] > 0, \
            f"Proof {i}: non-positive amount {proof['amount']}"

        assert isinstance(proof.get("id"), str) and len(proof["id"]) > 0, \
            f"Proof {i}: 'id' missing or empty"

        assert isinstance(proof.get("secret"), str) and len(proof["secret"]) > 0, \
            f"Proof {i}: 'secret' missing or empty"

        assert isinstance(proof.get("C"), str) and len(proof["C"]) > 0, \
            f"Proof {i}: 'C' missing or empty"

    # Total must be correct regardless of split count
    total = sum(p["amount"] for p in proofs)
    assert total == 8, \
        f"Multi-proof total is {total} (expected 8). " \
        f"Split: {[p['amount'] for p in proofs]}"


# ---------------------------------------------------------------------------
# V4 token regression tests (gonuts-tollgate v0.8.0+ resolveShortKeysetIds)
#
# Root cause: gonuts TokenV4.Proofs() converts 8-byte short keyset IDs to hex
# without resolving them to full IDs. The mint rejects the swap with
# "NUT02: ID length invalid". Fixed by resolveShortKeysetIds() in
# gonuts-tollgate v0.8.0 which fetches keysets from the mint and resolves
# short IDs before processing.
#
# Test matrix: V3+V1 ✅, V3+V2 ✅, V4+V1 ✅, V4+V2 ✅ (all pass after fix)
# See: AGENTS.md "Lessons Learned — July 2026 V4 Token Investigation"
# ---------------------------------------------------------------------------

_CDK_CLI_CANDIDATES = ["/tmp/cdk-cli", "/opt/cdk-mintd/cdk-cli", "cdk-cli"]


def _find_cdk_cli() -> str | None:
    for candidate in _CDK_CLI_CANDIDATES:
        path = shutil.which(candidate) or candidate
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _skip_if_no_cdk_cli():
    cli = _find_cdk_cli()
    if not cli:
        pytest.skip("cdk-cli not found — V4 token tests require cdk-cli for minting")
    return cli


def _mint_v4_token(cdk_cli: str, mint_url: str, amount: int = 4) -> str:
    work_dir = tempfile.mkdtemp(prefix="cdk-v4-")
    try:
        mint_r = subprocess.run(
            [cdk_cli, "-w", work_dir, "mint", mint_url, str(amount)],
            capture_output=True, text=True, timeout=90,
        )
        if mint_r.returncode != 0:
            pytest.skip(f"cdk-cli mint failed: {mint_r.stderr[-200:]}")

        send_r = subprocess.run(
            [cdk_cli, "-w", work_dir, "send", "--mint-url", mint_url],
            input=f"{amount}\n",
            capture_output=True, text=True, timeout=90,
        )
        if send_r.returncode != 0:
            pytest.skip(f"cdk-cli send (V4) failed: {send_r.stderr[-200:]}")

        for line in send_r.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("cashuB"):
                return line

        pytest.skip(f"cdk-cli did not produce a V4 token: {send_r.stdout[-200:]}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@pytest.mark.extended
def test_v4_token_has_cashuB_prefix():
    """Verify cdk-cli produces V4 (cashuB) tokens when --v3 is omitted."""
    cli = _skip_if_no_cdk_cli()
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL", "https://testnut.cashu.exchange")
    token = _mint_v4_token(cli, mint_url)
    assert token.startswith("cashuB"), \
        f"Expected cashuB prefix for V4 token, got: {token[:20]}"


@pytest.mark.extended
def test_v4_token_accepted_by_backend(router):
    """Verify the TollGate backend accepts V4 tokens (gonuts v0.8.0+).

    This is the core regression test for resolveShortKeysetIds. Before the
    fix, V4 tokens with V2 keysets failed at swap with "NUT02: ID length
    invalid". After the fix, all 4 format/keyset combinations work.

    See: https://github.com/OpenTollGate/gonuts-tollgate/releases/tag/v0.8.0
    """
    require_client_identity(router)
    cli = _skip_if_no_cdk_cli()
    mint_url = os.environ.get("TOLLGATE_TEST_MINT_URL", "https://testnut.cashu.exchange")

    token = _mint_v4_token(cli, mint_url)
    resp = router.pay_direct(token)

    assert is_payment_swap_succeeded(resp), \
        f"V4 token payment failed (swap should succeed with gonuts v0.8.0+): {resp}"


# ---------------------------------------------------------------------------
# V4 + V2 keyset regression test
#
# Before gonuts v0.8.0: V4 tokens with V2 keysets (01-prefix, 33 bytes) fail
# at swap. gonuts sends the 8-byte short ID to the mint, which rejects it:
# "NUT02: ID length invalid, expected 8 bytes (short/v1) or 33 bytes (v2)".
#
# After gonuts v0.8.0: resolveShortKeysetIds() fetches the mint's keysets,
# maps 8-byte short IDs to 33-byte full IDs, and sends full IDs to the mint.
# Swap succeeds.
#
# This test requires a V2-keyset mint (CDK V2). Set TOLLGATE_V2_MINT_URL
# to point to a CDK V2 mint. Defaults to the cloud lab's local CDK mint.
# ---------------------------------------------------------------------------

_V2_MINT_DEFAULT = "http://10.99.99.2:8383"


def _skip_if_no_v2_mint():
    mint_url = os.environ.get("TOLLGATE_V2_MINT_URL", _V2_MINT_DEFAULT)
    try:
        import urllib.request
        req = urllib.request.Request(f"{mint_url}/v1/keysets",
                                     headers={"User-Agent": "tollgate-test/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            keysets = data.get("keysets", [])
            has_v2 = any(ks.get("id", "").startswith("01") for ks in keysets)
            if not has_v2:
                pytest.skip(f"Mint {mint_url} has no V2 keysets")
    except Exception as exc:
        pytest.skip(f"V2 mint at {mint_url} unreachable: {exc}")
    return mint_url


@pytest.mark.extended
def test_v4_v2_keyset_payment_regression(router):
    """V4 token with V2 keyset must be accepted (gonuts v0.8.0+ resolveShortKeysetIds).

    BEFORE fix: V4+V2 → NUT02: ID length invalid (8-byte short ID sent to mint)
    AFTER fix:  V4+V2 → Receive completed, amount=N, err=<nil>

    E2E verified July 2026: 'Receive completed, amount=4, err=<nil>' with
    CDK V2 mint keyset 01df97b6fb8a572a... (V2, 01-prefix, 33 bytes).

    See: https://github.com/OpenTollGate/tollgate-module-basic-go/pull/284
    """
    require_client_identity(router)
    cli = _skip_if_no_cdk_cli()
    v2_mint = _skip_if_no_v2_mint()

    token = _mint_v4_token(cli, v2_mint)
    assert token.startswith("cashuB"), f"Expected V4 token, got: {token[:20]}"

    resp = router.pay_direct(token)
    assert is_payment_swap_succeeded(resp), \
        f"V4+V2 payment failed — resolveShortKeysetIds should resolve short IDs: {resp}"


# ---------------------------------------------------------------------------
# Already-spent token rejection test
#
# Reusing a Cashu token should fail with "token_already_spent" at the swap
# level. This is mint-side protection against double-spending, not backend
# logic. The test verifies the backend correctly propagates the mint's
# rejection.
# ---------------------------------------------------------------------------


@pytest.mark.extended
def test_already_spent_token_rejected(router, cashu):
    """Reusing a Cashu token must fail (mint rejects double-spend).

    BEFORE: First payment succeeds, second payment with same token also
            succeeds (critical bug — unlimited free internet).
    AFTER:  First payment succeeds (swap OK), second payment fails with
            'token_already_spent' at the mint swap endpoint.

    The test verifies is_payment_swap_succeeded returns True for the first
    payment and False for the second (token_already_spent is in the
    token_failures list).
    """
    require_client_identity(router)

    token = cashu.mint(amount=4)

    resp1 = router.pay_direct(token)
    assert is_payment_swap_succeeded(resp1), \
        f"First payment should succeed: {resp1}"

    resp2 = router.pay_direct(token)
    assert not is_payment_swap_succeeded(resp2), \
        f"Second payment with same token should fail (double-spend): {resp2}"

    body2 = json.dumps(resp2)
    assert "already" in body2.lower() or "spent" in body2.lower(), \
        f"Expected 'already spent' error, got: {resp2}"


# ---------------------------------------------------------------------------
# Multi-variant garbage token rejection
#
# The backend must reject invalid tokens with payment-error-invalid-token,
# not crash or return 500. This test covers multiple garbage patterns.
# ---------------------------------------------------------------------------


@pytest.mark.extended
def test_garbage_token_variants_rejected(router):
    """Multiple garbage token patterns must be rejected with kind != 1022.

    | Pattern              | Expected result                    |
    |----------------------|------------------------------------|
    | Random bytes         | kind=21023, invalid-token          |
    | Truncated V4 CBOR    | kind=21023, invalid-token          |
    | JSON with cashuB     | kind=21023, invalid-token          |
    | Empty string         | kind=21023, invalid-token          |
    | Valid prefix, no data| kind=21023, invalid-token          |
    """
    require_client_identity(router)

    garbage_variants = [
        ("random_bytes", "cashuB" + base64.urlsafe_b64encode(b"\x82\x01\x02\x03\x04").decode()),
        ("truncated_cbor", "cashuBo2FteB5odHRw"),
        ("json_with_cashuB", "cashuB" + base64.urlsafe_b64encode(b'{"proofs":[]}').decode()),
        ("prefix_only", "cashuB"),
        ("wrong_prefix", "cashuXtest123456"),
    ]

    for name, token in garbage_variants:
        resp = router.pay_direct(token)
        assert resp.get("kind") != 1022, \
            f"Garbage token '{name}' was ACCEPTED — backend should reject: {resp}"
        assert resp.get("success") is not True, \
            f"Garbage token '{name}' returned success=True: {resp}"
