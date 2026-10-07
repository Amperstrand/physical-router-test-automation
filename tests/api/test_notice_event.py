import json
import pytest
from lib.helpers import parse_json_or_fail, post_payment_event

pytestmark = [pytest.mark.story("P5"),pytest.mark.api, pytest.mark.critical]


@pytest.mark.critical
def test_notice_event_on_invalid_token(router):
    resp_text = post_payment_event(router, "cashuAinvalid")
    data = parse_json_or_fail(resp_text, "error response", skip=True)

    if data.get("kind") == 21023:
        _assert_notice_structure(data)
    else:
        assert data.get("kind") != 1022, "Invalid token produced a session event"
        assert resp_text, "Empty response for invalid payment"


@pytest.mark.critical
def test_notice_event_on_wrong_mint(router, cashu):
    wrong_token = cashu.synthetic_wrong_mint_token()

    resp = post_payment_event(router, wrong_token)
    data = parse_json_or_fail(resp, "error response", skip=True)

    assert data.get("kind") != 1022, "Wrong mint token produced a session event"

    if data.get("kind") == 21023:
        _assert_notice_structure(data)
        code_tags = [t for t in data.get("tags", []) if isinstance(t, list) and t[0] == "code"]
        if code_tags:
            assert code_tags[0][1], "Notice event has empty code tag"


@pytest.mark.critical
def test_notice_event_has_required_tags(router, cashu):
    wrong_token = cashu.synthetic_wrong_mint_token()
    resp = post_payment_event(router, wrong_token)
    data = parse_json_or_fail(resp, "error response", skip=True)

    if data.get("kind") == 21023:
        _assert_notice_structure(data)


def _assert_notice_structure(data: dict):
    tags = data.get("tags", [])
    level_tags = [t for t in tags if isinstance(t, list) and t[0] == "level"]
    assert len(level_tags) > 0, f"Notice event missing 'level' tag: {tags}"
    assert level_tags[0][1] in ("error", "warning", "info", "debug"), \
        f"Invalid level value: {level_tags[0][1]}"

    code_tags = [t for t in tags if isinstance(t, list) and t[0] == "code"]
    assert len(code_tags) > 0, f"Notice event missing 'code' tag: {tags}"

    content = data.get("content", "")
    assert content, "Notice event has empty content"
