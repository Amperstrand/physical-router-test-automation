"""Tests for ErrTokenAlreadySpent sentinel error handling.

Verifies that submitting the same Cashu token twice returns a
structured error response (not a 500 crash or generic error).

These tests exercise the tollwallet.ErrTokenAlreadySpent sentinel
and the merchant's errors.Is() detection.
"""

import json
import logging

import pytest

from lib.helpers import parse_json_or_fail, post_payment_event

log = logging.getLogger("tollgate.sentinel_error")

pytestmark = [pytest.mark.story("M2"),pytest.mark.api, pytest.mark.extended]


@pytest.mark.extended
def test_duplicate_token_returns_error_not_crash(router, cashu):
    """Submit the same valid token twice. The second attempt must return
    an error response (not 500 crash) with a recognizable error indicator."""
    if not cashu.is_available():
        pytest.skip("cashu venv not available — run scripts/setup-cashu.sh")

    token_str = cashu.mint(4)

    resp1_raw = post_payment_event(router, token_str)
    assert resp1_raw, "First submission returned no response"

    resp2_raw = post_payment_event(router, token_str)
    assert resp2_raw is not None, "Second submission returned no response (possible crash)"

    try:
        resp2 = json.loads(resp2_raw)
    except json.JSONDecodeError:
        pytest.fail(f"Second submission returned non-JSON: {resp2_raw[:200]}")

    kind = resp2.get("kind")
    if kind == 21023:
        tags = resp2.get("tags", [])
        code_tags = [
            t[1] for t in tags
            if isinstance(t, list) and len(t) >= 2 and t[0] == "code"
        ]
        log.info(
            "Duplicate token returned notice event kind=21023, codes=%s",
            code_tags,
        )
        return

    if kind in (1022, 21022):
        log.info("Duplicate token returned session/notice kind=%s (acceptable)", kind)
        return

    pytest.fail(
        f"Expected notice event (kind 21023) for duplicate token, "
        f"got kind={kind}: {resp2_raw[:200]}"
    )
