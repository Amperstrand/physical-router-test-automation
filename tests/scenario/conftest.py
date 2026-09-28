"""Scenario-layer test fixtures.

Re-exports the stories' labgrid phone mutex so phone-profile lifecycle
runs coordinate with every other lane that drives the bench phone, and
stamps phone-tier timeouts on these tests (they drive the physical
phone; the global 60s cap kills mid-flow).
"""

from __future__ import annotations

import pytest

from tests.stories.conftest import labgrid_phone_mutex  # noqa: F401

labgrid_phone_mutex = labgrid_phone_mutex


def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.timeout(300))
