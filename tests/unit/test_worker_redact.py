"""Unit tests for log redaction."""

from __future__ import annotations

from lib.cloud_lab.worker.shell import _redact


import pytest

pytestmark = [pytest.mark.story("P5")]

def test_redact_github_token():
    text = "GH_TOKEN=gho_abcdefghijklmnopqrstuvwxyz1234567890"
    redacted = _redact(text)
    assert "gho_abcdefghijklmnopqrstuvwxyz1234567890" not in redacted
    assert "***" in redacted


def test_redact_password_in_sshpass():
    text = "sshpass -p secretpassword ssh root@10.0.0.1"
    redacted = _redact(text)
    assert "secretpassword" not in redacted


def test_redact_preserves_safe_text():
    text = "TollGate backend healthy (attempt 1, http=200)"
    assert _redact(text) == text
