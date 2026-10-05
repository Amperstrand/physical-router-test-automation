"""replace_mints destructive-default guards (incident 2026-10-01, fix F3).

A bare replace_mints() call on a commissioned router silently rewrote an
8-mint accepted_mints to [TEST_MINT_URL] and restarted the backend twice.
The guards: mint_urls is required, and a >1 -> 1 shrink needs force=True.
"""
import json

import pytest

from lib.router import Router

CONFIG_TMPL = '{{"accepted_mints": [{mints}]}}'


def mint(url):
    return json.dumps({"url": url})


def make_router(config_json):
    router = Router("10.99.99.1", "10.99.99.186", "aa:bb:cc:dd:ee:ff", "test.lan")
    router.ssh = lambda cmd, timeout=None: config_json
    writes = []
    router.write_remote_json = lambda path, payload: writes.append(payload)
    router.restart_backend = lambda timeout=30: None
    return router, writes


def test_bare_call_raises_before_touching_the_router():
    router = Router("10.99.99.1", "10.99.99.186", "aa:bb:cc:dd:ee:ff", "test.lan")
    ssh_calls = []
    router.ssh = lambda cmd, timeout=None: ssh_calls.append(cmd) or "{}"
    with pytest.raises(ValueError, match="explicit mint_urls"):
        router.replace_mints()
    assert not ssh_calls


def test_shrinking_many_mints_to_one_requires_force():
    cfg = CONFIG_TMPL.format(
        mints=f"{mint('https://a.example')}, {mint('https://b.example')}, {mint('https://c.example')}"
    )
    router, writes = make_router(cfg)
    with pytest.raises(ValueError, match="force=True"):
        router.replace_mints(["https://testnut.cashu.exchange"])
    assert not writes


def test_shrink_with_force_writes_the_single_mint():
    cfg = CONFIG_TMPL.format(mints=f"{mint('https://a.example')}, {mint('https://b.example')}")
    router, writes = make_router(cfg)
    router.replace_mints(["https://testnut.cashu.exchange"], force=True)
    assert [m["url"] for m in writes[0]["accepted_mints"]] == ["https://testnut.cashu.exchange"]


def test_one_to_one_needs_no_force():
    cfg = CONFIG_TMPL.format(mints=mint("https://a.example"))
    router, writes = make_router(cfg)
    router.replace_mints(["https://b.example"])
    assert [m["url"] for m in writes[0]["accepted_mints"]] == ["https://b.example"]


def test_growing_or_multi_target_needs_no_force():
    cfg = CONFIG_TMPL.format(mints=mint("https://a.example"))
    router, writes = make_router(cfg)
    router.replace_mints(["https://b.example", "https://c.example"])
    assert len(writes[0]["accepted_mints"]) == 2


def test_identical_mints_skip_write():
    cfg = CONFIG_TMPL.format(mints=mint("https://a.example"))
    router, writes = make_router(cfg)
    router.replace_mints(["https://a.example"])
    assert not writes
