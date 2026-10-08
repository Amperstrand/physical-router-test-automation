"""Root conftest: the story/tip marker vocabulary + value selection.

Tests declare the user story they prove and the TollGate TIP (spec item,
upstream: OpenTollGate/tollgate TIP-*.md) they exercise:

    pytestmark = [pytest.mark.story("G2"), pytest.mark.tip("TIP-02")]

Selection (pytest's -m has no value comparison, so these are real options):

    pytest --story G2          # every test proving story G2
    pytest --story G1,G6       # any of several stories
    pytest --tip TIP-01        # the conformance battery for a TIP
    pytest --story G2 --tip TIP-02   # AND-combined

The generated docs (docs/user-stories.generated.md) come from these marks
via scripts/story-coverage.py — the marks are the single source of truth.
"""

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "story(id): user story this test proves (docs/user-stories.md)"
    )
    config.addinivalue_line(
        "markers", "tip(id): TollGate TIP spec item exercised (OpenTollGate/tollgate)"
    )


def pytest_addoption(parser):
    group = parser.getgroup("stories")
    group.addoption(
        "--story",
        action="store",
        default=None,
        metavar="IDS",
        help="Run only tests proving these user story ids (comma-separated, e.g. G2,O1)",
    )
    group.addoption(
        "--tip",
        action="store",
        default=None,
        metavar="TIPS",
        help="Run only tests exercising these TIPs (comma-separated, e.g. TIP-01)",
    )


def _marker_values(item, name):
    return [arg for mark in item.iter_markers(name) for arg in mark.args]


def pytest_collection_modifyitems(config, items):
    stories = config.getoption("--story")
    tips = config.getoption("--tip")
    if not stories and not tips:
        return
    want_stories = {s.strip().upper() for s in stories.split(",")} if stories else None
    want_tips = {t.strip().upper() for t in tips.split(",")} if tips else None
    remaining, deselected = [], []
    for item in items:
        has_story = want_stories is None or bool(
            want_stories & {s.upper() for s in _marker_values(item, "story")}
        )
        has_tip = want_tips is None or bool(
            want_tips & {t.upper() for t in _marker_values(item, "tip")}
        )
        (remaining if has_story and has_tip else deselected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = remaining
