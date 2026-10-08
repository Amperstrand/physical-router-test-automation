"""Unit tests for lib.cloud_lab.pulumi_runner.PulumiSHCProvider.

These tests mock the Pulumi Automation API layer so they run without a live
SHC API key or the pulumi package's network behavior. They verify the provider
wiring (stack.up/destroy called, outputs mapped to VMInfo, fallback paths)
rather than real provisioning — that is covered by the spike's run-up.sh /
run-destroy.sh against the live API.
"""
from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# pulumi + pulumi.automation may not be importable in the test environment;
# inject a stub so importing pulumi_runner doesn't fail at collection time.
pytestmark = [pytest.mark.story("P11")]

def _ensure_pulumi_stub():
    if "pulumi" not in sys.modules:
        stub = types.ModuleType("pulumi")
        stub.automation = types.ModuleType("pulumi.automation")
        stub.automation.LocalWorkspaceOptions = MagicMock()
        stub.automation.create_or_select_stack = MagicMock()
        sys.modules["pulumi"] = stub
        sys.modules["pulumi.automation"] = stub.automation


_ensure_pulumi_stub()

from lib.cloud_lab.pulumi_runner import (  # noqa: E402
    PulumiSHCProvider,
    _sanitize_stack_name,
    _DEFAULT_SIZE,
)
from lib.cloud_lab.provider import SHCProvider  # noqa: E402


# -- pure helpers ---------------------------------------------------------


class TestSanitizeStackName:
    def test_alphanumeric_passthrough(self):
        assert _sanitize_stack_name("tollgate-run-abc123") == "tollgate-run-abc123"

    def test_strips_invalid_chars(self):
        assert _sanitize_stack_name("tollgate/run@2026") == "tollgate-run-2026"

    def test_prepends_alpha_if_leading_digit(self):
        out = _sanitize_stack_name("123abc")
        assert out.startswith("tollgate-")
        assert "123abc" in out

    def test_empty_falls_back(self):
        assert _sanitize_stack_name("---") == "tollgate-runner"

    def test_truncates_to_90(self):
        out = _sanitize_stack_name("a" * 200)
        assert len(out) == 90


class TestSizeMapping:
    def test_default_when_empty(self):
        assert PulumiSHCProvider._size_for_machine_type("") == _DEFAULT_SIZE

    def test_known_mappings(self):
        assert PulumiSHCProvider._size_for_machine_type("2C/8GB") == "dev-2c-8gb"
        assert PulumiSHCProvider._size_for_machine_type("n1-standard-4") == "dev-4c-16gb"

    def test_unknown_falls_back_to_default(self):
        assert PulumiSHCProvider._size_for_machine_type("bogus-xl") == _DEFAULT_SIZE


# -- create/destroy wiring (mocked automation) ----------------------------


def _make_mock_stack(service_id=999, ip="10.0.0.1", hostname="tollgate-runner"):
    """A MagicMock standing in for a Pulumi Automation stack."""
    stack = MagicMock(name="stack")
    out_sid = MagicMock(); out_sid.value = service_id
    out_ip = MagicMock(); out_ip.value = ip
    out_host = MagicMock(); out_host.value = hostname
    out_user = MagicMock(); out_user.value = "debian"
    stack.outputs.return_value = {
        "service_id": out_sid, "ip": out_ip,
        "hostname": out_host, "os_user": out_user,
    }
    stack.name = "tollgate-cloud-lab-test"
    stack.workspace.return_value = MagicMock()
    return stack


class TestCreateVm:
    def test_create_calls_up_and_maps_outputs(self):
        provider = PulumiSHCProvider()
        mock_stack = _make_mock_stack(service_id=777, ip="66.92.1.1", hostname="h")
        with patch.object(provider, "_get_stack", return_value=mock_stack):
            vm = provider.create_vm(name="runner-x", machine_type="2C/8GB")

        mock_stack.up.assert_called_once()
        assert vm.service_id == 777
        assert vm.ip == "66.92.1.1"
        assert vm.hostname == "h"
        assert vm.provider == "pulumi"
        assert vm.raw["service_id"] == 777
        assert provider._stack is mock_stack

    def test_create_passes_hostname_and_size_to_program(self):
        provider = PulumiSHCProvider()
        mock_stack = _make_mock_stack()
        with patch.object(provider, "_get_stack", return_value=mock_stack) as gs:
            provider.create_vm(name="abc", machine_type="4C/16GB")
        gs.assert_called_once()
        _args, kwargs = gs.call_args
        assert kwargs["hostname"] == "abc"
        assert kwargs["size"] == "dev-4c-16gb"


class TestWaitForReady:
    def test_noop_when_ip_already_set(self):
        provider = PulumiSHCProvider()
        provider._stack = _make_mock_stack(ip="1.2.3.4")
        from lib.cloud_lab.provider import VMInfo
        vm = VMInfo(name="x", service_id=1, ip="1.2.3.4", hostname="h")
        out = provider.wait_for_ready(vm)
        assert out is vm
        assert out.ip == "1.2.3.4"

    def test_populates_from_stack_when_missing(self):
        provider = PulumiSHCProvider()
        provider._stack = _make_mock_stack(ip="9.9.9.9", hostname="fromstack")
        from lib.cloud_lab.provider import VMInfo
        vm = VMInfo(name="x", service_id=1, ip="", hostname="")
        provider.wait_for_ready(vm)
        assert vm.ip == "9.9.9.9"
        assert vm.hostname == "fromstack"


class TestDestroyVm:
    def test_destroy_calls_stack_destroy_and_remove(self):
        provider = PulumiSHCProvider()
        mock_stack = _make_mock_stack()
        provider._stack = mock_stack
        from lib.cloud_lab.provider import VMInfo
        vm = VMInfo(name="x", service_id=42)
        provider.destroy_vm(vm)

        mock_stack.destroy.assert_called_once()
        mock_stack.workspace.return_value.remove_stack.assert_called_once()
        assert provider._stack is None

    def test_destroy_falls_back_to_imperative_when_no_stack(self):
        """If the process restarted (no live stack), cancel via shc-toolkit."""
        provider = PulumiSHCProvider()
        provider._stack = None
        provider._client = MagicMock()  # short-circuit the inherited client property
        from lib.cloud_lab.provider import VMInfo
        vm = VMInfo(name="x", service_id=42)
        provider.destroy_vm(vm)
        provider._client.cancel_vm.assert_called_once_with(42, immediate=True)


class TestCleanupStale:
    def test_calls_super_then_cleans_old_stack_files(self, tmp_path):
        import json
        import os
        import time as _time
        provider = PulumiSHCProvider()
        with patch.object(SHCProvider, "cleanup_stale", return_value=2):
            with patch.dict(os.environ, {"PULUMI_WORKDIR": str(tmp_path)}):
                stacks_dir = tmp_path / ".pulumi" / "stacks" / "tollgate-cloud-lab"
                stacks_dir.mkdir(parents=True)
                old_file = stacks_dir / "old-stack.json"
                new_file = stacks_dir / "new-stack.json"
                old_file.write_text(json.dumps({"old": True}))
                new_file.write_text(json.dumps({"new": True}))
                old_time = _time.time() - (5 * 3600)
                os.utime(old_file, (old_time, old_time))

                count = provider.cleanup_stale(max_age_hours=2)

                assert count == 2
                assert not old_file.exists()
                assert new_file.exists()

    def test_no_workdir_is_safe(self):
        provider = PulumiSHCProvider()
        with patch.object(SHCProvider, "cleanup_stale", return_value=0):
            with patch.dict(os.environ, {"PULUMI_WORKDIR": "/nonexistent-path-xyz"}):
                count = provider.cleanup_stale(max_age_hours=1)
                assert count == 0


# -- provider registration -----------------------------------------------


class TestProviderRegistry:
    def test_shc_pulumi_key_present_for_discoverability(self):
        from lib.cloud_lab.provider import _PROVIDERS
        assert "pulumi" in _PROVIDERS

    def test_get_provider_populates_and_returns_pulumi_instance(self):
        from lib.cloud_lab.provider import get_provider, _PROVIDERS
        p = get_provider("pulumi")
        assert isinstance(p, PulumiSHCProvider)
        assert p.provider_name == "pulumi"
        assert _PROVIDERS["pulumi"] is PulumiSHCProvider
