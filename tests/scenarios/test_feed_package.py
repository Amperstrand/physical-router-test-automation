"""Verify the FreedomTechFeed feed release candidate installs and runs on the
attached physical router — for BOTH package formats:

  * OpenWrt 24.x -> opkg -> ``.ipk``
  * OpenWrt 25.x -> apk  -> ``.apk``

Reproducible: the artifact is supplied via ``TOLLGATE_PACKAGE_PATH`` (see
``make verify-feed-rc``, which downloads it from the feed release with
``scripts/download-feed-release.sh`` and sha256-verifies it). This test asserts
the router's package manager matches the artifact format, installs it via the
standard harness path (``lib.deploy.deploy``), and checks version + build
identity + service health.

Usage:
    TOLLGATE_PACKAGE_PATH=/tmp/feed-rc/tollgate-wrt_0.6.0_alpha2_pre_aarch64_cortex-a53.ipk \\
    TOLLGATE_SSH_HOST=192.168.1.1 TOLLGATE_SSH_PASSWORD=... \\
    pytest tests/scenarios/test_feed_package.py -v -s
"""
import os
import time

import pytest

from lib import deploy as deploylib

pytestmark = [pytest.mark.api, pytest.mark.hardware, pytest.mark.timeout(600)]

# Defaults pin the current feed RC; override via env for a new release.
EXPECT_VERSION = os.environ.get("FEED_EXPECT_VERSION", "0.6.0_alpha2_pre-r1")
EXPECT_COMMIT = os.environ.get(
    "FEED_EXPECT_COMMIT", "089e876cb24fd2fa8bd9d36323edb71e347e825b"
)


@pytest.fixture(scope="module")
def feed_package():
    path = os.environ.get("TOLLGATE_PACKAGE_PATH", "")
    if not path or not os.path.isfile(path):
        pytest.skip(
            "Set TOLLGATE_PACKAGE_PATH to the feed .ipk/.apk "
            "(make verify-feed-rc downloads it)"
        )
    ext = os.path.splitext(path)[1].lstrip(".")
    if ext not in ("ipk", "apk"):
        pytest.fail(f"TOLLGATE_PACKAGE_PATH must be .ipk or .apk, got {path}")
    return path, ext


class TestFeedPackage:
    """Install the feed RC over the router's own package manager and verify it."""

    def test_format_matches_router_package_manager(self, router, feed_package):
        _, ext = feed_package
        pm = deploylib.detect_package_manager(router)
        expected_ext = "apk" if pm == "apk" else "ipk"
        assert ext == expected_ext, (
            f"artifact is .{ext} but the router uses {pm} (expects .{expected_ext}) "
            "— wrong OpenWrt major for this artifact"
        )

    def test_install_from_feed(self, router, feed_package):
        path, _ = feed_package
        result = deploylib.deploy(router, path)
        assert result.get("success", True) is not False, f"deploy() failed: {result}"

    def test_installed_version(self, router, feed_package):
        pm = deploylib.detect_package_manager(router)
        if pm == "apk":
            # apk: `apk info -e` prints only the name; `apk list -I` prints
            # "tollgate-wrt-<version> <arch> ... [installed]".
            out = router.ssh(
                "apk list -I tollgate-wrt 2>/dev/null || "
                "apk list --installed 2>/dev/null | grep tollgate-wrt",
                timeout=15,
            )
        else:
            out = router.ssh("opkg list-installed tollgate-wrt 2>/dev/null", timeout=15)
        assert EXPECT_VERSION in out, f"expected {EXPECT_VERSION} in router package list: {out!r}"

    def test_build_identity_commit(self, router, feed_package):
        out = router.ssh(
            "/usr/bin/tollgate version --json 2>/dev/null || "
            "/usr/bin/tollgate version 2>/dev/null",
            timeout=15,
        )
        assert EXPECT_COMMIT in out, (
            f"installed build commit != feed pin {EXPECT_COMMIT}: {out[:300]!r}"
        )

    def test_api_healthy(self, router, feed_package):
        # tollgate-wrt can take ~30-60s on first boot (mint probes + wallet).
        last = ""
        for _ in range(30):
            last = router.ssh(
                "wget -qO- http://127.0.0.1:2121/ 2>/dev/null | head -c 200", timeout=10
            )
            if "kind" in last or "metric" in last or "pubkey" in last:
                return
            time.sleep(3)
        assert False, f"tollgate API on :2121 never responded: {last[:200]!r}"

    def test_services_running(self, router, feed_package):
        services = {"tollgate-wrt": "pidof tollgate-wrt", "nodogsplash": "pidof nodogsplash"}
        for name, cmd in services.items():
            out = router.ssh(f"{cmd} 2>/dev/null || echo DOWN", timeout=10)
            assert "DOWN" not in out, f"{name} is DOWN after installing the feed package"
