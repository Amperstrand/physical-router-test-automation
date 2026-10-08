"""Unit tests for conwrt deploy host-command execution."""

from __future__ import annotations

from unittest.mock import patch

from lib.cloud_lab.worker.conwrt_deploy import _execute_host_commands


import pytest

pytestmark = [pytest.mark.story("P11")]

def test_host_commands_pipe_via_stdin_not_and_join():
    with patch("lib.cloud_lab.worker.conwrt_deploy._run") as mock_run:
        _execute_host_commands(["scp one", "scp two"], timeout=5)
    cmd = mock_run.call_args[0][0]
    assert "printf %s" in cmd
    assert cmd.rstrip().endswith("| bash")
    assert " && " not in cmd


def test_host_commands_script_is_fail_fast():
    with patch("lib.cloud_lab.worker.conwrt_deploy._run") as mock_run:
        _execute_host_commands(["scp one"], timeout=5)
    cmd = mock_run.call_args[0][0]
    assert "set -e" in cmd


def test_blank_host_commands_are_a_noop():
    with patch("lib.cloud_lab.worker.conwrt_deploy._run") as mock_run:
        _execute_host_commands(["", "   "])
    mock_run.assert_not_called()


def test_heredoc_lines_survive_without_joining():
    with patch("lib.cloud_lab.worker.conwrt_deploy._run") as mock_run:
        _execute_host_commands(["cat > /tmp/x << 'EOF'", "body", "EOF"], timeout=5)
    cmd = mock_run.call_args[0][0]
    assert " && " not in cmd
    assert "EOF" in cmd


def test_ensure_conwrt_updates_existing_clone():
    with patch("lib.cloud_lab.worker.conwrt_deploy._run") as mock_run:
        mock_run.return_value.stdout = "EXISTS"
        from lib.cloud_lab.worker.conwrt_deploy import ensure_conwrt
        ensure_conwrt()
    cmds = [c.args[0] for c in mock_run.call_args_list]
    assert any("reset -q --hard origin/master" in c for c in cmds), cmds
    assert not any("git clone" in c for c in cmds), cmds
