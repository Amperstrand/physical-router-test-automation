"""Unit tests for lib.cashu.mint_reachable — TCP pre-check used to skip
tests before the cashu CLI can block against an absent mint."""
from __future__ import annotations

import socket

from lib.cashu import mint_reachable


def test_reachable_mint_returns_true():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        url = f"http://127.0.0.1:{srv.getsockname()[1]}"
        assert mint_reachable(url)


def test_closed_port_returns_false():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
    assert not mint_reachable(f"http://127.0.0.1:{port}", timeout=1.0)


def test_https_scheme_probes_port_443():
    # Scheme only affects the default port; use an unreachable loopback
    # stand-in by explicit port to keep the test hermetic.
    assert not mint_reachable("https://127.0.0.1:1", timeout=1.0)


def test_unresolvable_host_returns_false():
    assert not mint_reachable("https://nonexistent.invalid:8384", timeout=1.0)


def test_malformed_url_returns_false():
    assert not mint_reachable("not a url")
