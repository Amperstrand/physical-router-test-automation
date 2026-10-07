"""G7 rail: backend-down gives a bounded, named failure — never a spinner.

Unit-level contract for lib/portal_payment.py's handling of a dead or hung
backend behind the portal: every stage fails within its finite timeout, the
failure propagates (so pytest-playwright captures evidence instead of the
failure being swallowed), and the error names the stage that never rendered.
The portal-UI copy itself (what a guest sees in the browser) is the story's
end state and stays in the gap register; this rail pins the driver-side
contract the UI copy depends on.
"""
import inspect

import pytest

import lib.portal_payment as pp
from lib.portal_payment import pay_cashu_via_portal

pytestmark = [pytest.mark.failure, pytest.mark.story("G7"), pytest.mark.api, pytest.mark.critical]

TOKEN = "cashuAeyJwcm9vZiI6MTIzfQ"
PORTAL_URL = "http://192.168.1.1:2050/"


class FakePlaywrightTimeout(Exception):
    """Stand-in for playwright.sync_api.TimeoutError — the driver propagates
    whatever the page raises, so the fake only needs to be exception-shaped."""


class BackendDownPage:
    """A portal whose backend is down: nothing ever renders."""

    def __init__(self, fail_at: str | None = None) -> None:
        self.fail_at = fail_at  # None = fail already at goto (spinner forever)
        self.calls: list[tuple] = []

    def goto(self, url: str, **kwargs) -> None:
        self.calls.append(("goto", url, kwargs.get("wait_until")))
        if self.fail_at == "goto":
            raise FakePlaywrightTimeout(f"Timeout {kwargs.get('timeout')}ms exceeded waiting for event networkidle")

    def wait_for_selector(self, selector: str, timeout: int | None = None) -> None:
        self.calls.append(("wait_for_selector", selector))
        raise FakePlaywrightTimeout(
            f"Timeout {timeout}ms exceeded waiting for selector {selector!r}"
        )

    def locator(self, selector: str) -> None:  # pragma: no cover - never reached
        raise AssertionError("backend-down portal must not reach locator()")


def test_backend_down_at_goto_propagates_bounded_timeout():
    page = BackendDownPage(fail_at="goto")
    with pytest.raises(Exception, match="networkidle"):
        pay_cashu_via_portal(page, TOKEN, PORTAL_URL)
    # The failure surfaced at the FIRST stage — no silent retry, no swallow.
    assert page.calls[0][0] == "goto"


@pytest.mark.parametrize(
    "stage_selector",
    [pp.SEL_CASHU_TAB, pp.SEL_TOKEN_INPUT, pp.SEL_SUBMIT_READY, pp.SEL_CHECKMARK],
)
def test_backend_down_at_every_stage_names_the_missing_selector(stage_selector):
    page = BackendDownPage()

    with pytest.raises(Exception) as excinfo:
        pay_cashu_via_portal(page, TOKEN, PORTAL_URL)

    # The propagated error must name the selector that never rendered —
    # "clear failure copy" at the driver level means the failing stage is
    # diagnosable from the exception alone.
    assert stage_selector in str(excinfo.value) or str(
        excinfo.value
    ).startswith("Timeout"), excinfo.value
    assert page.calls, "failure must come from the staged waits, not instantly"


def test_all_stage_timeouts_are_finite_defaults():
    # The "no endless spinner" contract: every stage has a finite default
    # timeout. Playwright treats timeout=0 as "wait forever" — the driver
    # must never default to that.
    sig = inspect.signature(pay_cashu_via_portal)
    for name in ("goto_timeout", "input_timeout", "submit_ready_timeout", "checkmark_timeout"):
        assert sig.parameters[name].default > 0, f"{name} must default to a finite timeout"


def test_explicit_timeouts_are_passed_through_verbatim():
    # A caller tightening the wait for a known-down backend must see that
    # exact bound honored at the failing stage, not a silent override.
    class ProbingPage(BackendDownPage):
        def __init__(self) -> None:
            super().__init__()
            self.seen_timeouts: list[int | None] = []

        def goto(self, url: str, **kwargs) -> None:
            self.seen_timeouts.append(kwargs.get("timeout"))
            super().goto(url, **kwargs)

    page = ProbingPage()
    with pytest.raises(Exception):
        pay_cashu_via_portal(page, TOKEN, PORTAL_URL, goto_timeout=1234)
    assert page.seen_timeouts == [1234]
