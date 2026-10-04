"""Unit tests for Router read-only enforcement (F1) and replace_mints guard (F3).

Incident context: docs/live-commission-incident-2026-10-01.md.
Run: python3 -m pytest tests/unit/test_readonly_router.py -v
"""

import inspect
import json
import os

import pytest

from lib.router import ReadOnlyViolation, Router

CFG_3_MINTS = json.dumps({"accepted_mints": [
    {"url": f"https://mint{i}.example.com"} for i in range(3)
]})


def make_router(readonly=False):
    r = Router(host="192.0.2.1", phone_ip="", phone_mac="", domain="",
               readonly=readonly)
    return r


MUTATORS = [
    ("write_remote_text", ("/tmp/x", "data"), {}),
    ("write_remote_json", ("/tmp/x", {"a": 1}), {}),
    ("ssh_stdin", ("cat > /tmp/x", "data"), {}),
    ("scp_to", ("/tmp/local", "/tmp/remote"), {}),
    ("fix_nodogsplash_dhcp", (), {}),
    ("fix_nodogsplash_auth_marks", (), {}),
    ("remove_nds_auth_marks", (), {}),
    ("disable_ipv6_on_lan", (), {}),
    ("pay_direct", ("cashuA...",), {}),
    ("pay_direct_mac", ("cashuA...",), {}),
    ("pay_via_header", ("cashuA...",), {}),
    ("ensure_dhcp_lease", (), {}),
    ("reset_state", (), {}),
    ("apply_pricing", (), {}),
    ("restore_pricing", (), {}),
    ("restart_backend", (), {}),
    ("clear_portal_log", (), {}),
    ("enable_debug_portal", (), {}),
    ("disable_debug_portal", (), {}),
    ("ensure_test_mint", (), {}),
    ("replace_mints", (["https://m.example.com"],), {"force": True}),
    ("uci_set", ("wireless.@wifi[0].ssid", "x"), {}),
    ("uci_commit", ("wireless",), {}),
    ("block_mint", (), {}),
    ("unblock_mint", (), {}),
    ("upstream_connect", ("SSID",), {}),
    ("upstream_remove", ("SSID",), {}),
]


@pytest.mark.parametrize("name,args,kwargs", MUTATORS,
                         ids=[m[0] for m in MUTATORS])
def test_mutator_raises_in_readonly_mode(name, args, kwargs):
    router = make_router(readonly=True)
    with pytest.raises(ReadOnlyViolation, match=name):
        getattr(router, name)(*args, **kwargs)


def test_readonly_default_is_off():
    assert make_router().readonly is False


def test_readonly_guards_do_not_fire_when_disabled():
    router = make_router()
    calls = []
    router.ssh = lambda cmd, timeout=30: calls.append(cmd) or "NO\n"
    # enable_debug_portal with readonly=False reaches ssh (mutation proceeds)
    router.enable_debug_portal()
    assert any("debug-portal" in c for c in calls)


def test_replace_mints_requires_explicit_list():
    router = make_router()
    router.ssh = lambda cmd, timeout=30: CFG_3_MINTS
    with pytest.raises(TypeError):
        router.replace_mints()


def test_replace_mints_refuses_destructive_shrink():
    router = make_router()
    router.ssh = lambda cmd, timeout=30: CFG_3_MINTS
    with pytest.raises(ValueError, match="force=True"):
        router.replace_mints(["https://solo.example.com"])


def test_replace_mints_shrink_allowed_with_force():
    router = make_router()
    router.ssh = lambda cmd, timeout=30: CFG_3_MINTS
    written = []
    router.write_remote_json = lambda path, payload, **kw: written.append((path, payload))
    restarted = []
    router.restart_backend = lambda **kw: restarted.append(1)
    router.replace_mints(["https://solo.example.com"], force=True)
    assert written[0][0] == "/etc/tollgate/config.json"
    assert [m["url"] for m in written[0][1]["accepted_mints"]] == ["https://solo.example.com"]
    assert restarted == [1]


def test_replace_mints_noop_when_already_correct():
    router = make_router()
    router.ssh = lambda cmd, timeout=30: CFG_3_MINTS
    router.write_remote_json = lambda *a, **kw: pytest.fail("should not write")
    router.restart_backend = lambda **kw: pytest.fail("should not restart")
    urls = [f"https://mint{i}.example.com" for i in range(3)]
    router.replace_mints(urls)


def test_mock_router_signature_parity():
    from lib.mock_router import MockRouter
    sig = str(inspect.signature(MockRouter.replace_mints))
    assert "mint_urls" in sig
    assert "force" in sig
    assert "None" not in sig.split("force")[0].split("mint_urls")[1]


class _FakeConfig:
    def __init__(self, readonly_flag):
        self._readonly_flag = readonly_flag

    def getoption(self, name):
        assert name == "--read-only"
        return self._readonly_flag


class _FakeRequest:
    def __init__(self, readonly_flag):
        self.config = _FakeConfig(readonly_flag)


_CONFTEST_CACHE = {}


def _load_conftest():
    import importlib.util
    if "m" not in _CONFTEST_CACHE:
        spec = importlib.util.spec_from_file_location(
            "prta_conftest",
            os.path.join(os.path.dirname(__file__), "..", "conftest.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CONFTEST_CACHE["m"] = module
    return _CONFTEST_CACHE["m"]


@pytest.mark.parametrize("flag,live,allow,expected", [
    (True,  None,  None,  True),
    (False, None,  None,  False),
    (False, "1",   None,  True),
    (False, "1",   "1",   False),
    (False, None,  "1",   False),
    (False, "0",   None,  False),
])
def test_read_only_mode_truth_table(flag, live, allow, expected, monkeypatch):
    for var, val in (("TOLLGATE_LIVE_COMMISSION", live),
                     ("TOLLGATE_ALLOW_STATE_MUTATION", allow)):
        monkeypatch.delenv(var, raising=False)
        if val is not None:
            monkeypatch.setenv(var, val)
    module = _load_conftest()
    assert module._read_only_mode(_FakeRequest(flag)) is expected
