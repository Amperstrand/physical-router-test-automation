"""Router.wait_for_backend_ad requires the kind=10021 ad, not any HTTP answer.

Live evidence (2026-10-01, m5 commission on router 326D): after
restart_backend, :2121 answers HTTP immediately but serves a
'This TollGate is starting up… retry_after: 5' body while the wallet and
mint health load — the old status-code wait returned against that body.
"""
import pytest

from lib.router import Router

pytestmark = [pytest.mark.story("P3")]


AD_BODY = '{"kind":10021,"content":"ad","tags":[["price_per_step","1"]]}'
STARTING_BODY = '{"retry_after":5,"message":"This TollGate is starting up…"}'
GATEWAY_HTML = "<html>502 Bad Gateway</html>"


def make_router(responses):
    router = Router("10.99.99.1", "10.99.99.186", "aa:bb:cc:dd:ee:ff", "test.lan")
    remaining = list(responses)

    def fake_ssh(cmd, timeout=None):
        return remaining.pop(0) if remaining else ""

    router.ssh = fake_ssh
    return router


def test_ad_body_returns_immediately():
    calls = []
    router = make_router([])

    def ssh_once(cmd, timeout=None):
        calls.append(cmd)
        return AD_BODY

    router.ssh = ssh_once
    router.wait_for_backend_ad(timeout=1, interval=0)
    assert len(calls) == 1


def test_starting_body_then_ad_returns():
    router = make_router([STARTING_BODY, STARTING_BODY, AD_BODY])
    router.wait_for_backend_ad(timeout=5, interval=0)


def test_starting_body_times_out():
    router = make_router([STARTING_BODY])
    with pytest.raises(TimeoutError):
        router.wait_for_backend_ad(timeout=0.1, interval=0.01)


def test_non_json_body_times_out():
    router = make_router([GATEWAY_HTML])
    with pytest.raises(TimeoutError):
        router.wait_for_backend_ad(timeout=0.1, interval=0.01)


def test_empty_body_times_out():
    router = make_router([""])
    with pytest.raises(TimeoutError):
        router.wait_for_backend_ad(timeout=0.1, interval=0.01)


def test_ssh_error_is_retried_not_raised():
    router = make_router([AD_BODY])
    calls = []

    def flaky_ssh(cmd, timeout=None):
        calls.append(cmd)
        if len(calls) == 1:
            raise ConnectionResetError("ssh blip mid-restart")
        return AD_BODY

    router.ssh = flaky_ssh
    router.wait_for_backend_ad(timeout=2, interval=0)


def test_wait_for_backend_warns_instead_of_raising():
    router = make_router([STARTING_BODY])
    router._wait_for_backend(timeout=0.1)
