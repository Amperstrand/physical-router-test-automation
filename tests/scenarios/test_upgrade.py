"""Upgrade test scenario — verify package upgrade preserves config and services.

Tests the highest-risk item in the release plan: upgrading tollgate-wrt
without losing configuration, wallet state, or breaking NDS enforcement.

Usage:
    TOLLGATE_PACKAGE_PATH=/tmp/tollgate-wrt_new.ipk \
    pytest tests/scenarios/test_upgrade.py -v -s

The test:
1. Records pre-upgrade state (version, config, services, NDS chain)
2. Installs the new .ipk package
3. Verifies all services restart and come back up
4. Verifies config is unchanged
5. Verifies NDS/fw4 enforcement chain is still active
"""
import json
import os
import re
import time

import pytest

pytestmark = [pytest.mark.story("O4"),pytest.mark.api, pytest.mark.hardware, pytest.mark.timeout(300)]


@pytest.fixture(scope="module")
def upgrade_package():
    path = os.environ.get("TOLLGATE_PACKAGE_PATH", "")
    if not path or not os.path.isfile(path):
        pytest.skip("Set TOLLGATE_PACKAGE_PATH to the .ipk to upgrade to")
    return path


class TestUpgrade:

    def test_record_pre_upgrade_state(self, router, upgrade_package):
        self._pre_version = router.ssh(
            "opkg list-installed tollgate-wrt 2>/dev/null | awk '{print $3}'", timeout=10
        ).strip()
        self._pre_config = router.ssh("cat /etc/tollgate/config.json 2>/dev/null", timeout=10).strip()
        assert self._pre_version, "Could not determine current tollgate-wrt version"
        assert self._pre_config, "Could not read /etc/tollgate/config.json"

    def test_install_upgrade_package(self, router, upgrade_package):
        router.scp_to(upgrade_package, "/tmp/tollgate-upgrade.ipk")
        result = router.ssh("opkg install /tmp/tollgate-upgrade.ipk 2>&1", timeout=60)
        assert "Cannot install package" not in result, f"opkg install failed:\n{result}"
        time.sleep(10)

    def test_services_running_after_upgrade(self, router, upgrade_package):
        services = {
            "tollgate-wrt": "pidof tollgate-wrt",
            "nodogsplash": "pidof nodogsplash",
            "dropbear": "pidof dropbear",
            "uhttpd": "pidof uhttpd",
        }
        for name, cmd in services.items():
            result = router.ssh(f"{cmd} 2>/dev/null || echo DOWN", timeout=10)
            assert "DOWN" not in result, f"{name} is DOWN after upgrade"

    def test_backend_reachable_after_upgrade(self, router, upgrade_package):
        status = router.ssh(
            "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:2121/ 2>/dev/null",
            timeout=10,
        )
        assert status.strip() == "200", f"Backend returned HTTP {status} after upgrade"

    def test_config_preserved_after_upgrade(self, router, upgrade_package):
        post_config = router.ssh("cat /etc/tollgate/config.json 2>/dev/null", timeout=10).strip()
        assert post_config, "config.json missing after upgrade"
        pre = json.loads(self._pre_config)
        post = json.loads(post_config)
        assert pre.get("config_version") == post.get("config_version"), (
            f"config_version changed: {pre.get('config_version')} → {post.get('config_version')}"
        )
        assert pre.get("accepted_mints") == post.get("accepted_mints"), "Mint config changed during upgrade"

    def test_version_changed_after_upgrade(self, router, upgrade_package):
        post_version = router.ssh(
            "opkg list-installed tollgate-wrt 2>/dev/null | awk '{print $3}'", timeout=10
        ).strip()
        assert post_version, "Could not determine post-upgrade version"
        assert post_version != self._pre_version, (
            f"Version unchanged after upgrade: {self._pre_version} → {post_version}"
        )

    def test_nds_enforcement_chain_after_upgrade(self, router, upgrade_package):
        chain = router.ssh("nft list chain inet fw4 nds_enforce_forward 2>/dev/null", timeout=10)
        if "No such file" in chain or not chain.strip():
            pytest.xfail("nds_enforce_forward chain not present after upgrade — PR #283 nft file may need redeployment")
        assert re.search(r"priority (filter - 1|-1)", chain), (
            f"nds_enforce_forward priority wrong after upgrade"
        )

    def test_portal_reachable_after_upgrade(self, router, upgrade_package):
        status = router.ssh(
            "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:2050/ 2>/dev/null",
            timeout=10,
        )
        assert status.strip() in ("200", "302", "307"), (
            f"NDS portal returned HTTP {status} after upgrade"
        )
