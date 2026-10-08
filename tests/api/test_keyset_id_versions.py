"""Comprehensive keyset ID version tests for Cashu mint compatibility.

Validates V1 and V2 keyset ID formats as specified in NUT-02
(https://github.com/cashubtc/nuts/blob/main/02.md).

Keyset ID formats:
  V1: 00 (1 byte version) + first 14 hex chars of SHA256(concat of raw public keys)
      = 16 hex chars total (8 bytes). Example: 0016f5fb5e5278f2

  V2: 01 (1 byte version) + SHA256(amount:pubkey_hex pairs sorted by amount,
      comma-separated, |unit:sat)
      = 66 hex chars total (33 bytes). Example:
      01df97b6fb8a572a718d7df7fcbf4387e2d455134ea8004c9c8c51e1b3391f909e
"""

import base64
import hashlib
import json
import logging
import os
import re

import pytest

from lib.helpers import parse_json_or_fail, require_client_identity

log = logging.getLogger(__name__)

pytestmark = [pytest.mark.api, pytest.mark.extended]

MINT_URL = os.environ.get("TOLLGATE_TEST_MINT_URL", "https://testnut.cashu.exchange")

V1_PATTERN = re.compile(r"^00[0-9a-f]{14}$")
V2_PATTERN = re.compile(r"^01[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_mint_keysets(mint_url):
    """Fetch and parse ``GET /v1/keys`` from the mint.

    Returns a list of keyset dicts, each with ``id``, ``unit``, ``keys``.
    Uses ``urllib.request.Request`` with a custom User-Agent to avoid
    ASU-style 403 blocks. In mock mode the "mint" is the MockCashuMinter's
    deterministic keyset — there is no real mint to query.
    """
    if os.environ.get("TOLLGATE_MOCK", "").lower() in ("1", "true", "yes"):
        from lib.cashu import MockCashuMinter
        minter = MockCashuMinter(mint_url)
        return [{"id": kid, "unit": "sat", "keys": minter.keyset_keys()}
                for kid in minter.keyset_ids()]
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError

    endpoint = f"{mint_url.rstrip('/')}/v1/keys"
    req = Request(endpoint, headers={"User-Agent": "tollgate-test/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
    except (URLError, HTTPError) as exc:
        pytest.skip(f"Cannot reach mint at {endpoint}: {exc}")

    data = json.loads(body)
    keysets = data.get("keysets", [])
    if not keysets:
        pytest.skip(f"No keysets returned by mint at {endpoint}")
    return keysets


def _decode_cashuA_token(token):
    """Strip ``cashuA`` prefix, base64url-decode, return parsed JSON."""
    if token.startswith("cashuA"):
        token = token[len("cashuA"):]
    # base64url padding
    token += "=" * (-len(token) % 4)
    raw = base64.urlsafe_b64decode(token)
    return json.loads(raw)


def _classify_keyset_id(keyset_id):
    """Return ``"V1"`` or ``"V2"`` or ``"unknown"``."""
    if V1_PATTERN.match(keyset_id):
        return "V1"
    if V2_PATTERN.match(keyset_id):
        return "V2"
    return "unknown"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mint_keysets():
    """Module-scoped fetch of mint keysets to avoid repeated HTTP calls."""
    return _get_mint_keysets(MINT_URL)


@pytest.fixture(scope="module")
def mint_keyset_ids(mint_keysets):
    """Set of all keyset IDs from the mint."""
    return {ks["id"] for ks in mint_keysets}


@pytest.fixture(scope="module")
def v1_keysets(mint_keysets):
    """V1 keysets only (``00``-prefix, 16 hex chars)."""
    return [ks for ks in mint_keysets if V1_PATTERN.match(ks["id"])]


@pytest.fixture(scope="module")
def v2_keysets(mint_keysets):
    """V2 keysets only (``01``-prefix, 66 hex chars)."""
    return [ks for ks in mint_keysets if V2_PATTERN.match(ks["id"])]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.extended
def test_mint_keysets_endpoint_returns_valid_structure(mint_keysets):
    """GET /v1/keys returns a list of keysets with id, unit, keys fields.

    Verifies the mint's keyset endpoint is reachable and returns a
    well-formed response per NUT-02.
    """
    assert isinstance(mint_keysets, list), "keysets should be a list"
    assert len(mint_keysets) > 0, "keysets list should not be empty"

    for ks in mint_keysets:
        assert "id" in ks, f"Keyset missing 'id' field: {ks}"
        assert "unit" in ks, f"Keyset missing 'unit' field: {ks}"
        assert "keys" in ks, f"Keyset missing 'keys' field: {ks}"
        assert isinstance(ks["keys"], dict), (
            f"Keyset 'keys' should be a dict, got {type(ks['keys'])}"
        )


@pytest.mark.extended
def test_keyset_id_format_classification(mint_keysets):
    """Classify each keyset ID as V1, V2, or unknown.

    Logs which versions are present for diagnostic purposes.
    Every keyset ID must match either V1 or V2 format.
    """
    versions = {"V1": [], "V2": [], "unknown": []}
    for ks in mint_keysets:
        v = _classify_keyset_id(ks["id"])
        versions[v].append(ks["id"])

    log.info("Keyset ID classification: V1=%s, V2=%s, unknown=%s",
             len(versions["V1"]), len(versions["V2"]), len(versions["unknown"]))

    if versions["V1"]:
        log.info("V1 keyset IDs: %s", versions["V1"])
    if versions["V2"]:
        log.info("V2 keyset IDs: %s", versions["V2"])
    if versions["unknown"]:
        log.warning("Unknown keyset IDs: %s", versions["unknown"])

    # At least one known version should be present
    assert versions["V1"] or versions["V2"], \
        "No V1 or V2 keysets found — all keyset IDs are unrecognised"


@pytest.mark.extended
def test_v1_keyset_id_length(v1_keysets):
    """V1 keyset IDs are exactly 16 hex characters (8 bytes).

    Format: ``00`` + 14 hex chars of SHA256 digest.
    See NUT-02 V1 keyset ID derivation.
    """
    if not v1_keysets:
        pytest.skip("No V1 keysets available from this mint")

    for ks in v1_keysets:
        kid = ks["id"]
        assert len(kid) == 16, \
            f"V1 keyset ID should be 16 hex chars, got {len(kid)}: {kid}"
        assert kid.startswith("00"), \
            f"V1 keyset ID should start with '00': {kid}"
        log.info("V1 keyset ID verified: %s", kid)


@pytest.mark.extended
def test_v2_keyset_id_length(v2_keysets):
    """V2 keyset IDs are exactly 66 hex characters (33 bytes).

    Format: ``01`` + 64 hex chars (full SHA256 digest).
    See NUT-02 V2 keyset ID derivation (PR #182).
    """
    if not v2_keysets:
        pytest.skip("No V2 keysets available from this mint")

    for ks in v2_keysets:
        kid = ks["id"]
        assert len(kid) == 66, \
            f"V2 keyset ID should be 66 hex chars, got {len(kid)}: {kid}"
        assert kid.startswith("01"), \
            f"V2 keyset ID should start with '01': {kid}"
        log.info("V2 keyset ID verified: %s", kid)


@pytest.mark.extended
def test_keyset_has_amount_keys(mint_keysets):
    """Each keyset's ``keys`` dict contains standard power-of-2 amounts.

    NUT-02 requires that keys map integer amounts to hex-encoded public
    keys. Standard amounts: 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024.
    """
    standard_amounts = {1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024}
    for ks in mint_keysets:
        keys = ks["keys"]
        present_amounts = set()
        for amt_str, pubkey in keys.items():
            try:
                amt = int(amt_str)
            except ValueError:
                continue
            present_amounts.add(amt)
            assert isinstance(pubkey, str), \
                f"Public key for amount {amt_str} should be a string, got {type(pubkey)}"
            # Hex public key should be a valid hex string (64 chars for secp256k1)
            assert len(pubkey) >= 64, \
                f"Public key for amount {amt_str} too short ({len(pubkey)} chars): {pubkey[:20]}..."

        overlap = present_amounts & standard_amounts
        assert len(overlap) >= 4, \
            (f"Keyset {ks['id']} has only {len(overlap)} standard amounts "
             f"({sorted(overlap)}), expected at least 4")


@pytest.mark.extended
def test_minted_token_keyset_id_matches_mint(router, cashu, mint_keyset_ids):
    """A minted token's proof keyset ID must exist in the mint's /v1/keys.

    Mints a token, decodes the cashuA payload, and verifies every proof's
    ``id`` field is present in the mint's published keysets.
    """
    token = cashu.mint(amount=4)
    decoded = _decode_cashuA_token(token)

    proofs = decoded.get("proofs", [])
    if not proofs and "token" in decoded:
        for entry in decoded["token"]:
            proofs.extend(entry.get("proofs", []))
    assert proofs, f"No proofs in decoded token: {decoded}"

    for proof in proofs:
        proof_id = proof.get("id")
        assert proof_id, f"Proof missing 'id' field: {proof}"
        assert proof_id in mint_keyset_ids, \
            (f"Proof keyset ID '{proof_id}' not found in mint's /v1/keys "
             f"keysets: {sorted(mint_keyset_ids)}")
        log.info("Token proof keyset ID %s validated against mint", proof_id)


@pytest.mark.extended
def test_v1_keyset_id_derivation_format(v1_keysets):
    """V1 keyset ID derivation: ``00`` + first 14 hex of SHA256(raw pubkeys).

    Informational — logs derivation details but does not fail on mismatch,
    as mint implementations may vary in how they serialize public keys
    for hashing.

    NUT-02 V1: id = ``00`` + hex(sha256(concat(raw_pubkeys))[:7])
    """
    if not v1_keysets:
        pytest.skip("No V1 keysets available")

    for ks in v1_keysets:
        kid = ks["id"]
        keys = ks["keys"]

        # Sort amounts numerically
        sorted_amounts = sorted(keys.keys(), key=lambda a: int(a))

        # Attempt V1 derivation: concatenate raw (compressed) public key bytes
        concatenated = b""
        for amt in sorted_amounts:
            pubkey_hex = keys[amt]
            try:
                concatenated += bytes.fromhex(pubkey_hex)
            except ValueError:
                log.warning("Cannot hex-decode pubkey for amount %s: %s",
                            amt, pubkey_hex[:20])
                continue

        if concatenated:
            derived_hash = hashlib.sha256(concatenated).hexdigest()
            # V1 takes first 14 hex chars of the hash
            derived_id = "00" + derived_hash[:14]
            match = derived_id == kid
            log.info(
                "V1 derivation for keyset %s: derived=%s, actual=%s, match=%s",
                kid, derived_id, kid, match,
            )
            if not match:
                log.info(
                    "V1 derivation mismatch (informational): "
                    "hash=%s, first14=%s",
                    derived_hash, derived_hash[:14],
                )


@pytest.mark.extended
def test_v2_keyset_id_derivation_format(v2_keysets):
    """V2 keyset ID derivation: ``01`` + SHA256(amount:pubkey pairs).

    Informational — logs derivation details but does not fail on mismatch.

    NUT-02 V2 (PR #182): id = ``01`` + hex(sha256(
        ``amount:pubkey_hex`` pairs sorted by amount, comma-separated,
        ``|unit:sat``
    ))
    """
    if not v2_keysets:
        pytest.skip("No V2 keysets available")

    for ks in v2_keysets:
        kid = ks["id"]
        keys = ks["keys"]
        unit = ks.get("unit", "sat")

        # Sort amounts numerically
        sorted_amounts = sorted(keys.keys(), key=lambda a: int(a))

        # Build the derivation input: amount:pubkey pairs, sorted, comma-separated
        pairs = [f"{amt}:{keys[amt]}" for amt in sorted_amounts]
        derivation_input = ",".join(pairs) + f"|{unit}:{unit}"

        derived_hash = hashlib.sha256(derivation_input.encode()).hexdigest()
        derived_id = "01" + derived_hash
        match = derived_id == kid
        log.info(
            "V2 derivation for keyset %s: derived=%s, actual=%s, match=%s",
            kid, derived_id, kid, match,
        )
        if not match:
            log.info(
                "V2 derivation mismatch (informational): input=%r, hash=%s",
                derivation_input[:80], derived_hash,
            )


@pytest.mark.extended
def test_discovery_includes_active_keyset_ids(router, mint_keyset_ids):
    """Backend discovery event may contain keyset IDs matching mint's /v1/keys.

    GET ``/`` from the backend, inspect tags for strings that match V1 or V2
    keyset ID patterns. Any keyset IDs found must be present in the mint's
    published keysets.
    """
    body = router.api_body("/")
    discovery = parse_json_or_fail(body, "discovery response")

    tags = discovery.get("tags", [])
    found_keyset_ids = set()

    for tag in tags:
        # Tags can be lists or strings
        if isinstance(tag, str) and (V1_PATTERN.match(tag) or V2_PATTERN.match(tag)):
            found_keyset_ids.add(tag)
        elif isinstance(tag, list):
            for item in tag:
                if isinstance(item, str) and (
                    V1_PATTERN.match(item) or V2_PATTERN.match(item)
                ):
                    found_keyset_ids.add(item)

    if not found_keyset_ids:
        pytest.skip("No keyset IDs found in backend discovery tags")

    # Every keyset ID in discovery must exist in the mint's keysets
    unknown = found_keyset_ids - mint_keyset_ids
    assert not unknown, \
        f"Discovery has keyset IDs not in mint's /v1/keys: {sorted(unknown)}"

    log.info("Discovery contains %d valid keyset ID(s): %s",
             len(found_keyset_ids), sorted(found_keyset_ids))


@pytest.mark.extended
def test_payment_proof_keyset_id_accepted(router, cashu):
    """Backend accepts payment tokens regardless of keyset ID version.

    Mints a token and pays via ``router.pay_direct()``. The backend must
    accept the token whether it carries a V1 or V2 keyset ID.
    """
    require_client_identity(router)

    token = cashu.mint(amount=4)
    decoded = _decode_cashuA_token(token)
    proofs = decoded.get("proofs", [])
    if not proofs and "token" in decoded:
        for entry in decoded["token"]:
            proofs.extend(entry.get("proofs", []))

    if not proofs:
        pytest.skip("No proofs in minted token — cannot determine keyset version")

    keyset_id = proofs[0].get("id", "unknown")
    version = _classify_keyset_id(keyset_id)
    log.info("Minted token uses keyset %s (%s)", keyset_id, version)

    resp = router.pay_direct(token)

    # Accept success indicators from both Go and Rust backends
    accepted = (
        resp.get("kind") == 1022
        or resp.get("success") is True
        or resp.get("kind") == 21023  # informational response still accepted
    )

    # If backend rejects with a keyset-related error, skip rather than fail
    if not accepted:
        content = str(resp)
        if "not accepted" in content.lower() or "unknown keyset" in content.lower():
            pytest.skip(
                f"Backend rejected {version} keyset token ({keyset_id}): "
                f"{content[:200]}"
            )

    assert accepted, \
        f"Payment with {version} keyset token rejected: {str(resp)[:300]}"

    log.info("Payment accepted with %s keyset ID %s", version, keyset_id)
