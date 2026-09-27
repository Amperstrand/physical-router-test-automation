"""Unit tests for Router.fix_nodogsplash_auth_marks (NDS 5.0.2 mark bug).

See AGENTS.md "Nodogsplash 5.0.2 auth-mark bug": NDS marks authenticated
clients' packets 0x30000, but ndsNET only accepts 0x20000/0x30000 — the
client cannot open new connections. The helper inserts one client-agnostic
auth-bit accept rule in ndsNET (rewriting the per-client ndsOUT rule leaks:
NDS cannot delete it at deauth, granting permanent access).
"""

import pytest

from lib.router import Router

BUGGY = (
    "-A ndsOUT -s 10.99.99.186/32 -m mac --mac-source aa:bb:cc:dd:ee:ff "
    "-j MARK --set-xmark 0x30000/0x30000"
)
NET_CLEAN = "-P ndsNET ACCEPT"
ACCEPT = "-A ndsNET -m mark --mark 0x20000/0x20000 -j ACCEPT"


def make_router(responses, calls):
    router = Router("10.99.99.1", "10.99.99.186", "aa:bb:cc:dd:ee:ff", "test.lan")
    remaining = list(responses)

    def fake_ssh(cmd, timeout=None):
        calls.append(cmd)
        return remaining.pop(0) if remaining else ""

    router.ssh = fake_ssh
    return router


def test_inserts_ndsnet_accept_rule_when_bug_present():
    calls = []
    router = make_router([NET_CLEAN, f"-N ndsOUT\n{BUGGY}"], calls)

    router.fix_nodogsplash_auth_marks()

    assert calls[0].startswith("iptables -S ndsNET")
    assert calls[1].startswith("iptables -t mangle -S ndsOUT")
    assert calls[2] == "iptables -I ndsNET 1 -m mark --mark 0x20000/0x20000 -j ACCEPT"
    assert len(calls) == 3


def test_noop_when_accept_rule_already_present():
    calls = []
    router = make_router([f"{NET_CLEAN}\n{ACCEPT}"], calls)

    router.fix_nodogsplash_auth_marks()

    assert calls == ["iptables -S ndsNET 2>/dev/null"]


def test_noop_when_no_buggy_client_rules():
    calls = []
    router = make_router([NET_CLEAN, "-N ndsOUT"], calls)

    router.fix_nodogsplash_auth_marks()

    assert len(calls) == 2
    assert not any("-I ndsNET" in c for c in calls)


def test_ip_mac_filter_gates_detection():
    calls = []
    router = make_router([NET_CLEAN, f"-N ndsOUT\n{BUGGY}"], calls)

    router.fix_nodogsplash_auth_marks(ip="10.99.99.42", mac="11:22:33:44:55:66")

    assert len(calls) == 2
    assert not any("-I ndsNET" in c for c in calls)


def test_ssh_failure_is_swallowed():
    router = Router("10.99.99.1", "10.99.99.186", "aa:bb:cc:dd:ee:ff", "test.lan")

    def failing_ssh(cmd, timeout=None):
        raise RuntimeError("connection lost")

    router.ssh = failing_ssh

    router.fix_nodogsplash_auth_marks()


def test_wait_for_auth_repairs_marks_once_authed(monkeypatch):
    router = Router("10.99.99.1", "10.99.99.186", "aa:bb:cc:dd:ee:ff", "test.lan")
    monkeypatch.setattr(router, "get_nds_state", lambda mac=None: "Authenticated")
    repaired = []
    monkeypatch.setattr(router, "fix_nodogsplash_auth_marks", lambda: repaired.append(True))
    assert router.wait_for_auth(timeout=1) is True
    assert repaired == [True]


def test_wait_for_auth_no_repair_when_not_authed(monkeypatch):
    router = Router("10.99.99.1", "10.99.99.186", "aa:bb:cc:dd:ee:ff", "test.lan")
    monkeypatch.setattr(router, "get_nds_state", lambda mac=None: "Preauthenticated")
    repaired = []
    monkeypatch.setattr(router, "fix_nodogsplash_auth_marks", lambda: repaired.append(True))
    assert router.wait_for_auth(timeout=1) is False
    assert repaired == []
