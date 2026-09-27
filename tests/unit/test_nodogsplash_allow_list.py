"""Offline tests for the nodogsplash pre-auth allow-list parsers.

No router and no network: these tests exist because the live check for
"nodogsplash allows port 443" was a substring search over the whole
/etc/config/nodogsplash text, and ``"443"`` is satisfied by the
``list users_to_router 'allow tcp port 8443'`` entry that every router with the
admin board enabled carries. The check therefore passed on the 0.6.0-alpha3
pre14 build, where :443 was NOT allow-listed and the operator's
``:8080 -> https://<router>/`` redirect dead-ended (connection refused) for an
unauthenticated captive-LAN client.

The config text below is the real serialisation observed on a GL-MT3000
running 0.6.0-alpha3, with the :443 entry removed to reproduce pre14.
"""

from lib.helpers import nodogsplash_allow_entries, nodogsplash_allows_port

# Real /etc/config/nodogsplash from a 0.6.0-alpha3 pre14 router: note :8443 is
# present (the admin board) and :443 is not.
PRE14_CONFIG = """config nodogsplash
\toption enabled '1'
\toption gatewayname 'c08r4d0r-1A2B Portal'
\toption gatewayinterface 'br-lan'
\toption gatewayport '2050'
\toption sessiontimeout '86400'
\toption authidletimeout '3600'
\tlist users_to_router 'allow tcp port 22'
\tlist users_to_router 'allow tcp port 23'
\tlist users_to_router 'allow tcp port 53'
\tlist users_to_router 'allow udp port 53'
\tlist users_to_router 'allow udp port 67'
\tlist users_to_router 'allow tcp port 80'
\tlist users_to_router 'allow tcp port 2121'
\tlist users_to_router 'allow tcp port 8080'
\tlist users_to_router 'allow tcp port 2050'
\tlist users_to_router 'allow tcp port 8090'
\tlist users_to_router 'allow tcp port 8443'
\tlist users_to_router 'allow tcp port 2051'
"""

FIXED_CONFIG = PRE14_CONFIG + "\tlist users_to_router 'allow tcp port 443'\n"


def test_pre14_does_not_allow_443():
    """The defect: pre14 never allow-listed :443."""
    assert nodogsplash_allows_port(PRE14_CONFIG, 443) is False


def test_pre14_does_allow_8443():
    """Sanity: the same config does carry the admin board's :8443."""
    assert nodogsplash_allows_port(PRE14_CONFIG, 8443) is True


def test_substring_search_was_satisfied_by_8443():
    """The regression guard.

    8443 contains "443", so ``"443" in config_text`` — the old assertion —
    returns True on the defective pre14 config. Any future check written as a
    substring search fails this test.
    """
    assert "443" in PRE14_CONFIG, "precondition: substring search finds 8443"
    assert not nodogsplash_allows_port(PRE14_CONFIG, 443)


def test_fixed_config_allows_443():
    assert nodogsplash_allows_port(FIXED_CONFIG, 443) is True


def test_8443_does_not_satisfy_443_request():
    for port in (443, 4430, 8443, 2051):
        assert nodogsplash_allows_port(FIXED_CONFIG, port) is (
            port in (443, 8443, 2051)
        ), "port %d reported the wrong answer" % port


def test_entries_are_parsed_in_file_order():
    assert nodogsplash_allow_entries(PRE14_CONFIG)[:2] == [
        "allow tcp port 22",
        "allow tcp port 23",
    ]
    assert nodogsplash_allow_entries(PRE14_CONFIG)[-1] == "allow tcp port 2051"


def test_only_users_to_router_lines_count():
    cfg = (
        "config nodogsplash\n"
        "\toption note_about_443 'allow tcp port 443'\n"
        "\tlist authenticated_users_to_router 'allow tcp port 443'\n"
    )
    assert nodogsplash_allow_entries(cfg) == []
    assert nodogsplash_allows_port(cfg, 443) is False


def test_double_quoted_and_bare_entries():
    cfg = (
        'list users_to_router "allow tcp port 443"\n'
        "list users_to_router allow tcp port 2051\n"
    )
    assert nodogsplash_allows_port(cfg, 443) is True
    assert nodogsplash_allows_port(cfg, 2051) is True


def test_empty_config_allows_nothing():
    assert nodogsplash_allow_entries("") == []
    assert nodogsplash_allows_port("", 443) is False
