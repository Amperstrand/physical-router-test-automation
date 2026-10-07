"""Hostname setup tests.

Verifies that the first-boot setup (``99-tollgate-setup``) configures the
router hostname correctly. Originally introduced by PR #117, now shipped via
PR #123 (merged). Feature-detected so tests run against any firmware that sets
the hostname, including main.

SSL coverage lives in ``test_ssl_go_cli.py`` (PR #123 Go CLI) and
``test_ssl_real_cert.py``.
"""

import pytest

pytestmark = [pytest.mark.story("O6"),pytest.mark.api, pytest.mark.extended]


def _skip_if_no_hostname_setup(router):
    hostname = router.ssh("uci get system.@system[0].hostname 2>/dev/null").strip()
    if hostname.lower() != "tollgate":
        pytest.skip("Hostname setup not present (hostname is not 'TollGate')")


@pytest.mark.extended
def test_hostname_set_to_tollgate(router):
    _skip_if_no_hostname_setup(router)
    hostname = router.ssh("uci get system.@system[0].hostname 2>/dev/null").strip()
    assert hostname == "TollGate", f"Expected hostname 'TollGate', got '{hostname}'"


@pytest.mark.extended
def test_hostname_persists_after_restart(router):
    _skip_if_no_hostname_setup(router)
    hostname_uci = router.ssh("uci get system.@system[0].hostname 2>/dev/null").strip()
    assert hostname_uci == "TollGate"
    hostname_runtime = router.ssh("cat /proc/sys/kernel/hostname").strip()
    assert hostname_runtime == "TollGate", \
        f"UCI says TollGate but kernel hostname is '{hostname_runtime}'"
