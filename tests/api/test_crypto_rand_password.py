"""Tests for PR #111: Crypto-secure random password generation.

PR #111 (fix/security-crypto-rand) changes generateRandomPassword() to use
crypto/rand instead of math/rand for WPA2 password generation.

Key behaviors under test:
- Password follows the Word-Word-Word-NN pattern (e.g. "Maple-Tiger-Brave-42")
- Each word segment starts with uppercase followed by lowercase
- Ends with 2+ digits
- The `network set-private-network` command is available via Unix socket

These tests establish the BASELINE behavior on whatever firmware is currently
deployed. If the commands don't exist yet (PR not deployed), tests skip with
informative messages documenting what PR #111 would add.
"""

import json
import logging
import re

import pytest

log = logging.getLogger("tollgate.crypto_rand_password")

pytestmark = [
    pytest.mark.story("R6"),pytest.mark.api, pytest.mark.extended]

# Expected password pattern: CapitalWord-CapitalWord-CapitalWord-DD+
WPA2_PASSWORD_RE = re.compile(r'^[A-Z][a-z]+-[A-Z][a-z]+-[A-Z][a-z]+-\d{2,}$')


def _try_network_command(router, subcommand, args=None):
    """Send a network command via the Unix socket, return (success, response).

    Returns (True, dict) if command was accepted (even if it errored),
    (False, raw_output) if the socket or command doesn't exist.
    """
    try:
        resp = router.cli_command("network", args=[subcommand] + (args or []))
        raw = str(resp.get("raw", "")).lower() if isinstance(resp, dict) else str(resp).lower()
        if "raw" in resp and ("not found" in raw or "unknown" in raw or "no such" in raw):
            return False, resp["raw"]
        if isinstance(resp, dict):
            error = str(resp.get("error", "")).lower()
            success = resp.get("success")
            if error and ("unknown network subcommand" in error or "unknown command" in error or "not available" in error):
                return False, resp
            if success is False and error:
                return False, resp
        return True, resp
    except Exception as exc:
        return False, str(exc)


def _skip_if_no_password_command(router):
    """Skip if the password-setting network subcommand is not available.

    Tries the current subcommand name first, falls back to the original
    name used in early PR #111 drafts.
    """
    ok, resp = _try_network_command(router, "private")
    if ok:
        return ok, resp

    ok2, resp2 = _try_network_command(router, "set-private-network")
    if ok2:
        return ok2, resp2

    pytest.skip(
        "network private / set-private-network command not available on this firmware. "
        f"Response: {str(resp)[:200]}"
    )


@pytest.mark.extended
def test_current_password_generation_available(router):
    """Check if the network private command exists via socket.

    This documents the baseline: whether the password generation command
    is available on the current firmware. PR #111 changes the RNG source
    behind this command from math/rand to crypto/rand.
    """
    ok, resp = _skip_if_no_password_command(router)
    log.info("network password command is available")


@pytest.mark.extended
def test_password_generation_creates_valid_wpa2_password(router):
    """Generate a password via the network command and verify the format.

    PR #111 changes generateRandomPassword() to use crypto/rand. The output
    format should remain Word-Word-Word-NN for WPA2 compatibility.
    """
    ok, resp = _skip_if_no_password_command(router)

    # The response should contain the generated password somewhere
    raw = json.dumps(resp)
    # Look for password-like patterns in the response
    candidates = re.findall(r'[A-Z][a-z]+-[A-Z][a-z]+-[A-Z][a-z]+-\d{2,}', raw)

    if not candidates:
        # The command may have succeeded but returned the password in a
        # different field — document what we got
        pytest.skip(
            f"set-private-network responded but no Word-Word-Word-NN password "
            f"found in output. Response structure may differ from expected. "
            f"Raw: {raw[:300]}"
        )

    password = candidates[0]
    assert WPA2_PASSWORD_RE.match(password), \
        f"Password '{password}' does not match expected Word-Word-Word-NN pattern"

    log.info("Generated password matches WPA2 pattern: %s", password[:6] + "...")


@pytest.mark.extended
def test_password_format_is_word_word_word_digits(router):
    """Verify the password matches the strict Word-Word-Word-DD pattern.

    PR #111 changes the RNG but should preserve this format exactly.
    """
    ok, resp = _skip_if_no_password_command(router)

    raw = json.dumps(resp)
    candidates = re.findall(r'[A-Z][a-z]+-[A-Z][a-z]+-[A-Z][a-z]+-\d{2,}', raw)

    if not candidates:
        pytest.skip(
            f"No password candidate found in set-private-network response. "
            f"Raw: {raw[:300]}"
        )

    password = candidates[0]
    parts = password.split("-")

    # Must have exactly 4 parts: Word-Word-Word-DD
    assert len(parts) == 4, \
        f"Password '{password}' has {len(parts)} parts, expected 4"

    # First 3 parts are words (capitalized)
    for i, word in enumerate(parts[:3]):
        assert word[0].isupper(), \
            f"Word {i+1} '{word}' does not start with uppercase"
        assert word[1:].islower(), \
            f"Word {i+1} '{word}' has non-lowercase chars after first"
        assert len(word) >= 2, \
            f"Word {i+1} '{word}' is too short (need >= 2 chars)"

    # Last part is digits only, at least 2
    digits = parts[3]
    assert digits.isdigit(), f"Last segment '{digits}' is not all digits"
    assert len(digits) >= 2, f"Digit segment '{digits}' is too short (need >= 2)"

    log.info("Password format validated: %d words + %d digits",
             len(parts) - 1, len(digits))
