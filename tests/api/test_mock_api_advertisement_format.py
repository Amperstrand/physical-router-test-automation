"""Regression test: the recording mock API advertisement must match the real
TollGate backend schema.

The mock served by ``scripts/record-portal-highlight.mjs`` (its ``GET /``
response) must produce the same NIP-01 advertisement event that the real
backend emits via ``merchant.go:CreateAdvertisement()``:

  * ``kind == 10021``
  * a ``metric`` tag          -> ``["metric", "milliseconds" | "bytes"]``
  * a ``step_size`` tag       -> ``["step_size", "<digits>"]``
  * one or more ``price_per_step`` tags
      -> ``["price_per_step", "cashu", "<price>", "<unit>", "<url>", "<minSteps>"]``

If this test fails, the recording mock has drifted from the backend contract.
Historically that drift surfaced as ``TG003`` (the captive portal could not
find the pricing tags it expects, breaking the recorded demo).

The test reads the mock **directly from the script source** -- no Node runtime
and (in its own logic) no router are required; it only reads a local file. It
mirrors the assertions in ``tests/api/test_info_endpoint.py`` (which validates
the *live* backend) so the mock and real contracts cannot diverge.
"""
import json
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.story("P7"),pytest.mark.api, pytest.mark.smoke]

# scripts/record-portal-highlight.mjs, resolved from this test file
# (tests/api/test_mock_api_advertisement_format.py -> repo root).
REPO_ROOT = Path(__file__).resolve().parents[2]
MOCK_SCRIPT = REPO_ROOT / "scripts" / "record-portal-highlight.mjs"

# Source of truth for the advertisement schema. If the Go struct changes,
# this docstring is the breadcrumb back to the implementation.
SCHEMA_SOURCE = "tollgate-module-basic-go/src/merchant/merchant.go:CreateAdvertisement()"


def _extract_balanced_object(text, open_index):
    """Return the substring of ``text`` covering the ``{...}`` block that
    starts at ``open_index``.

    A simple depth counter would miscount braces that appear inside JS string
    literals (e.g. a URL or description containing ``}``), so we track string
    context (single/double/backtick quotes, with escape handling) while
    scanning.
    """
    assert text[open_index] == "{", "extractor must start at an opening brace"
    depth = 0
    i = open_index
    in_str = False
    quote = ""
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":  # skip the escaped character
                i += 2
                continue
            if ch == quote:
                in_str = False
        else:
            if ch in ("'", '"', "`"):
                in_str = True
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[open_index : i + 1]
        i += 1
    raise ValueError("unbalanced braces while extracting mock object literal")


def _normalize_js_object_literal(text):
    """Best-effort convert a JS object/array literal to valid JSON text.

    JS object literals deviate from JSON in ways that break ``json.loads``:
      * bare (unquoted) object keys        -> ``"key"``
      * single-quoted string delimiters    -> ``"..."`` (double-quoted)
      * trailing commas before ``}``/``]`` -> removed

    The transforms only fire on quote *delimiters* and bare identifiers that
    sit in key position (immediately after ``{``/``,``), so string *contents*
    such as the ``:`` and ``//`` inside a mint URL are left untouched. This
    keeps the test resilient to the mock being edited in either single- or
    double-quote style -- the schema, not the quote style, is what we guard.
    """
    # 1) Quote bare keys: an identifier following '{' or ',' (with optional
    #    whitespace/newlines) and followed by ':'.
    text = re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)(\s*:)", r'\1"\2"\3', text)
    # 2) Convert single-quoted strings to double-quoted JSON strings.
    def _dq(m):
        raw = m.group(1).replace("\\'", "'")
        return json.dumps(raw)

    text = re.sub(r"'((?:[^'\\]|\\.)*)'", _dq, text)
    # 3) Strip trailing commas before ] or }.
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def _load_mock_advertisement():
    """Parse ``scripts/record-portal-highlight.mjs`` and return the JSON object
    served at the mock ``GET /`` endpoint (the advertisement event).

    Supports both ``'/': JSON.stringify({...})`` and a bare ``'/': {...}``,
    and tolerates JS-style single quotes / bare keys via normalization.
    """
    if not MOCK_SCRIPT.is_file():
        pytest.skip(f"recording mock script not found: {MOCK_SCRIPT}")
    src = MOCK_SCRIPT.read_text()

    # Scope the search to the MOCK_API declaration so we never match a stray
    # '/' key elsewhere in the file.
    api_start = src.find("MOCK_API")
    if api_start == -1:
        pytest.fail("MOCK_API table not found in the recording script")

    key_re = re.compile(r"['\"]\/['\"]\s*:\s*")
    m = key_re.search(src, api_start)
    if m is None:
        pytest.fail(
            "could not locate the '/' (advertisement) entry in MOCK_API; "
            "the recording mock no longer simulates GET /"
        )

    tail = src[m.end():]
    strcall = re.match(r"JSON\.stringify\(\s*", tail)
    offset = strcall.end() if strcall else 0
    try:
        brace = tail.index("{", offset)
    except ValueError:
        pytest.fail("the '/' mock entry is not a JSON object literal")

    obj_text = _extract_balanced_object(tail, brace)
    # Try strict JSON first (double-quoted mock); fall back to a JS-literal
    # normalization (single quotes / bare keys) so the test survives either
    # quote style.
    for candidate in (obj_text, _normalize_js_object_literal(obj_text)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    pytest.fail(
        f"the '/' mock entry is not valid JSON (or a JS object literal):\n{obj_text}"
    )


@pytest.fixture(scope="module")
def advertisement():
    """The mock advertisement event parsed from the recording script."""
    return _load_mock_advertisement()


def test_mock_advertisement_is_object(advertisement):
    """The '/' response must be a JSON object (a NIP-01 event), not a flat
    status blob like ``{success, price, unit}`` -- that shape is the TG003
    drift."""
    assert isinstance(advertisement, dict), (
        f"mock '/' response must be a JSON object, got {type(advertisement).__name__}"
    )


def test_mock_advertisement_kind(advertisement):
    assert advertisement.get("kind") == 10021, (
        f"mock advertisement kind must be 10021 (NIP advertisement event), "
        f"got kind={advertisement.get('kind')!r}"
    )


def test_mock_advertisement_has_metric_tag(advertisement):
    tags = advertisement.get("tags", [])
    metric_tags = [
        t for t in tags if isinstance(t, list) and len(t) >= 2 and t[0] == "metric"
    ]
    assert metric_tags, f"missing 'metric' tag in mock advertisement: {tags}"
    assert metric_tags[0][1] in ("milliseconds", "bytes"), (
        f"invalid metric value {metric_tags[0][1]!r} "
        "(expected 'milliseconds' or 'bytes')"
    )


def test_mock_advertisement_has_step_size_tag(advertisement):
    tags = advertisement.get("tags", [])
    step_tags = [
        t for t in tags if isinstance(t, list) and len(t) >= 2 and t[0] == "step_size"
    ]
    assert step_tags, f"missing 'step_size' tag in mock advertisement: {tags}"
    assert str(step_tags[0][1]).isdigit(), (
        f"step_size must be an integer string, got {step_tags[0][1]!r}"
    )


def test_mock_advertisement_has_price_per_step(advertisement):
    tags = advertisement.get("tags", [])
    price_tags = [
        t
        for t in tags
        if isinstance(t, list) and len(t) >= 2 and t[0] == "price_per_step"
    ]
    assert price_tags, f"missing 'price_per_step' tag in mock advertisement: {tags}"
    # CreateAdvertisement() emits the full 6-element form:
    # [price_per_step, bearer_asset_type, price, unit, mint_url, min_purchase_steps]
    price = price_tags[0]
    assert len(price) == 6, (
        "price_per_step must have 6 elements "
        "(price_per_step, cashu, price, unit, url, min_steps), "
        f"got {len(price)}: {price}"
    )
    assert price[1] == "cashu", (
        f"price_per_step bearer asset must be 'cashu', got {price[1]!r}"
    )


def test_mock_advertisement_has_tags_array(advertisement):
    tags = advertisement.get("tags")
    assert isinstance(tags, list) and tags, (
        f"advertisement must expose a non-empty 'tags' array, got {tags!r}"
    )
