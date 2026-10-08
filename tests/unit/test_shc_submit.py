"""Unit tests for SHC submit pipeline — shc_submit.py."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


pytestmark = [pytest.mark.story("P11")]

def test_build_bootstrap_script_has_lease():
    from lib.cloud_lab.shc_submit import _build_bootstrap_script
    script = _build_bootstrap_script(
        bootstrap_env="TOLLGATE_RUN_ID=test",
        overlay_b64="",
        test_dir="/opt/tollgate-test",
        suite_repo_url="https://github.com/test/repo.git",
        lease_minutes=45,
    )
    assert "LEASE_MINUTES=45" in script
    assert "shutdown" in script


def test_build_bootstrap_script_unsets_secrets():
    from lib.cloud_lab.shc_submit import _build_bootstrap_script
    script = _build_bootstrap_script(
        bootstrap_env="TOLLGATE_RUN_ID=test BOT_NSEC_HEX=secret GH_TOKEN=token",
        overlay_b64="",
        test_dir="/opt/tollgate-test",
        suite_repo_url="https://github.com/test/repo.git",
    )
    assert "unset BOT_NSEC_HEX" in script


def test_build_bootstrap_script_has_cloud_init_seed():
    from lib.cloud_lab.shc_submit import _build_bootstrap_script
    script = _build_bootstrap_script(
        bootstrap_env="TOLLGATE_RUN_ID=test",
        overlay_b64="",
        test_dir="/opt/tollgate-test",
        suite_repo_url="https://github.com/test/repo.git",
    )
    assert "genericcloud" in script
    assert "debian-seed.iso" in script
    assert "genisoimage" in script


def test_build_bootstrap_script_step_count():
    from lib.cloud_lab.shc_submit import _build_bootstrap_script
    script = _build_bootstrap_script(
        bootstrap_env="TOLLGATE_RUN_ID=test",
        overlay_b64="",
        test_dir="/opt/tollgate-test",
        suite_repo_url="https://github.com/test/repo.git",
    )
    assert "N_STEPS=15" in script
    for i in range(1, 16):
        assert f"step {i} \"" in script


def test_build_bootstrap_script_completes_with_marker():
    from lib.cloud_lab.shc_submit import _build_bootstrap_script
    script = _build_bootstrap_script(
        bootstrap_env="TOLLGATE_RUN_ID=test",
        overlay_b64="",
        test_dir="/opt/tollgate-test",
        suite_repo_url="https://github.com/test/repo.git",
    )
    assert "touch /tmp/tollgate-done" in script
    assert "BOOTSTRAP_DONE" in script


def test_constants_are_defined():
    from lib.cloud_lab.shc_submit import SHC_PACKAGE_ID_STANDARD, SHC_PRICING_ID_STANDARD
    assert SHC_PACKAGE_ID_STANDARD == 81
    assert SHC_PRICING_ID_STANDARD == 245


def test_wait_for_shc_run_signature():
    from lib.cloud_lab.shc_submit import wait_for_shc_run
    import inspect
    sig = inspect.signature(wait_for_shc_run)
    params = sig.parameters
    assert "use_sshpass" in params
    assert "vm_password" in params
    assert params["use_sshpass"].default is False
    assert params["vm_password"].default == ""


def test_build_bootstrap_script_overlay_applied():
    from lib.cloud_lab.shc_submit import _build_bootstrap_script
    script = _build_bootstrap_script(
        bootstrap_env="TOLLGATE_RUN_ID=test",
        overlay_b64="dGVzdA==",
        test_dir="/opt/tollgate-test",
        suite_repo_url="https://github.com/test/repo.git",
    )
    assert "dGVzdA==" in script
    assert "base64 -d /tmp/overlay.b64" in script


def test_build_bootstrap_script_logs_publisher_npub():
    from lib.cloud_lab.shc_submit import _build_bootstrap_script
    script = _build_bootstrap_script(
        bootstrap_env="TOLLGATE_RUN_ID=test",
        overlay_b64="",
        test_dir="/opt/tollgate-test",
        suite_repo_url="https://github.com/test/repo.git",
    )
    assert 'nak key public "$(cat /root/nsec)"' in script
    assert "Publisher npub:" in script
    assert "EXPECTED_NPUB" in script
    assert "STRICT_NPUB_CHECK" in script
    assert "npub mismatch" in script
