"""Unit tests for worker config metadata loading."""

from __future__ import annotations

from unittest.mock import patch

from lib.cloud_lab.worker.config import WorkerConfig, load_config_from_metadata


import pytest

pytestmark = [pytest.mark.story("P11")]

def test_load_config_from_metadata_parses_flags():
    values = {
        "tollgate-run-id": "20260101T120000Z-abc1234",
        "tollgate-sut-branch": "feat/x",
        "tollgate-sut-commit": "abc1234567890",
        "tollgate-pr": "104",
        "tollgate-artifact-run-id": "999",
        "tollgate-artifact-repo": "OpenTollGate/tollgate-module-basic-go",
        "tollgate-pr-repo": "OpenTollGate/tollgate-module-basic-go",
        "tollgate-suite-ref": "deadbeef",
        "tollgate-backend": "go",
        "tollgate-reseller-scenarios": "false",
        "tollgate-two-router": "false",
        "tollgate-secondary-router-host": "",
        "tollgate-secondary-router-port": "",
        "tollgate-keep-vm-on-failure": "true",
        "tollgate-publish": "true",
        "tollgate-project": "tollgate-test-lab",
        "tollgate-zone": "us-central1-a",
        "tollgate-vm-name": "tollgate-run-test",
        "tollgate-gh-token": "gho_test_token",
        "tollgate-mint": "auto",
        "tollgate-portal": "builtin",
        "tollgate-hwsim": "false",
        "tollgate-vwifi": "false",
        "tollgate-quick": "false",
        "tollgate-smoke": "true",
        "tollgate-wifi-plane": "tap",
    }

    def fake_get(key: str) -> str:
        return values[key]

    def fake_optional(key: str, default: str = "") -> str:
        return values.get(key, default)

    with patch("lib.cloud_lab.worker.config._metadata_get", side_effect=fake_get):
        with patch("lib.cloud_lab.worker.config._metadata_get_optional", side_effect=fake_optional):
            cfg = load_config_from_metadata()

    assert isinstance(cfg, WorkerConfig)
    assert cfg.run_id == "20260101T120000Z-abc1234"
    assert cfg.sut_pr == "104"
    assert cfg.smoke is True
    assert cfg.publish is True
    assert cfg.keep_vm_on_failure is True
    assert cfg.artifact_repo == "OpenTollGate/tollgate-module-basic-go"
    assert cfg.pr_repo == "OpenTollGate/tollgate-module-basic-go"
