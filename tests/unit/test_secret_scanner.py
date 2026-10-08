"""Unit tests for lib/secret_scanner.py — security-critical publish-path scanner.

Tests the pure functions that prevent secrets from leaking to public Blossom:
  - shannon_entropy / is_high_entropy_hex
  - is_blocked_file (blocklist logic)
  - scan_content (regex patterns: nsec, tokens, passwords, IPs, hex keys)
  - verify_content_clean
"""
from __future__ import annotations

import pytest

from lib.secret_scanner import (
    shannon_entropy,
    is_high_entropy_hex,
    is_blocked_file,
    scan_content,
    verify_content_clean,
    verify_clean,
    scan_file,
)


# --------------------------------------------------------------------------- #
# shannon_entropy
# --------------------------------------------------------------------------- #


pytestmark = [pytest.mark.story("P5")]

class TestShannonEntropy:
    def test_empty_string(self):
        assert shannon_entropy("") == 0.0

    def test_single_char(self):
        assert shannon_entropy("aaaa") == 0.0

    def test_two_equal_chars(self):
        assert shannon_entropy("ab") == pytest.approx(1.0)

    def test_four_distinct_chars(self):
        assert shannon_entropy("abcd") == pytest.approx(2.0)

    def test_high_entropy_hex(self):
        e = shannon_entropy("a3f2b8c1d4e5f6a7b8c9d0e1f2a3b4c5")
        assert e > 3.0

    def test_low_entropy_repeated(self):
        assert shannon_entropy("0000000000000000") == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# is_high_entropy_hex
# --------------------------------------------------------------------------- #


class TestIsHighEntropyHex:
    def test_short_string_returns_false(self):
        assert is_high_entropy_hex("abc123") is False

    def test_31_chars_returns_false(self):
        assert is_high_entropy_hex("a" * 31) is False

    def test_32_zeros_low_entropy_false(self):
        assert is_high_entropy_hex("0" * 32) is False

    def test_32_high_entropy_true(self):
        assert is_high_entropy_hex("a3f2b8c1d4e5f6a7b8c9d0e1f2a3b4c5") is True

    def test_custom_threshold(self):
        high = "a3f2b8c1d4e5f6a7b8c9d0e1f2a3b4c5"
        assert is_high_entropy_hex(high, threshold=3.0) is True
        assert is_high_entropy_hex(high, threshold=5.0) is False


# --------------------------------------------------------------------------- #
# is_blocked_file
# --------------------------------------------------------------------------- #


class TestIsBlockedFile:
    def test_blocked_exact_config_json(self):
        assert is_blocked_file("/path/to/config.json") is True

    def test_blocked_env(self):
        assert is_blocked_file("/app/.env") is True

    def test_blocked_nsec_file(self):
        assert is_blocked_file("/tmp/nsec") is True

    def test_blocked_pem_suffix(self):
        assert is_blocked_file("/certs/server.pem") is True

    def test_blocked_key_suffix(self):
        assert is_blocked_file("/ssh/id_rsa.key") is True

    def test_blocked_secret_substring(self):
        assert is_blocked_file("/tmp/my_secret_data") is True

    def test_blocked_token_substring(self):
        assert is_blocked_file("/tmp/api-token-backup") is True

    def test_allowed_python_file_with_secret_in_name(self):
        assert is_blocked_file("/lib/secret_scanner.py") is False

    def test_allowed_regular_file(self):
        assert is_blocked_file("/tmp/junit.xml") is False
        assert is_blocked_file("/results/screenshot.png") is False
        assert is_blocked_file("/app/main.py") is False

    def test_allowed_md_with_secret(self):
        assert is_blocked_file("/docs/secret-design.md") is False


# --------------------------------------------------------------------------- #
# scan_content — regex patterns
# --------------------------------------------------------------------------- #


class TestScanContentNostrNsec:
    def test_detects_bech32_nsec(self):
        nsec = "nsec1" + "x" * 58
        sanitized, findings = scan_content(f"key={nsec}")
        assert len(findings) == 1
        assert findings[0]["type"] == "nostr-nsec-bech32"
        assert nsec not in sanitized

    def test_detects_hex_nsec_assignment(self):
        hex_key = "a" * 64
        sanitized, findings = scan_content(f"NOSTR_SECRET_KEY={hex_key}")
        assert any(f["type"] == "nostr-nsec-hex" for f in findings)
        assert hex_key not in sanitized

    def test_detects_bot_nsec_hex(self):
        hex_key = "f" * 64
        sanitized, findings = scan_content(f"BOT_NSEC='{hex_key}'")
        assert any(f["type"] == "nostr-nsec-hex" for f in findings)


class TestScanContentTokens:
    def test_detects_cashu_token(self):
        token = "cashuAeyJ0b2tlbiI6W3sibWludCI6Imh0dHAifV19"
        sanitized, findings = scan_content(f"token={token}")
        assert any(f["type"] == "cashu-token" for f in findings)
        assert token not in sanitized

    def test_detects_github_token(self):
        token = "ghp_" + "a" * 36
        sanitized, findings = scan_content(f"GITHUB_TOKEN={token}")
        assert any(f["type"] == "github-token" for f in findings)

    def test_detects_hetzner_token(self):
        token = "A" * 64
        sanitized, findings = scan_content(f"HCLOUD_TOKEN='{token}'")
        assert any(f["type"] == "hetzner-token" for f in findings)


class TestScanContentPasswords:
    def test_detects_sshpass_password(self):
        sanitized, findings = scan_content("sshpass -p 'secretpass123' ssh root@host")
        assert any(f["type"] == "ssh-password-sshpass" for f in findings)
        assert "secretpass123" not in sanitized

    def test_detects_ssh_password_var(self):
        sanitized, findings = scan_content('SSH_PASSWORD="mypassword"')
        assert any(f["type"] == "ssh-password-var" for f in findings)

    def test_detects_router_password(self):
        sanitized, findings = scan_content("router_password: admin1234")
        assert any(f["type"] == "router-password" for f in findings)

    def test_detects_generic_password(self):
        sanitized, findings = scan_content('password = "supersecret"')
        assert any(f["type"] == "hardcoded-password" for f in findings)

    def test_detects_tollgate_hardcoded(self):
        sanitized, findings = scan_content("password is tollgate123")
        assert any(f["type"] == "hardcoded-router-password" for f in findings)


class TestScanContentPEM:
    def test_detects_pem_block(self):
        content = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc...\n-----END RSA PRIVATE KEY-----"
        sanitized, findings = scan_content(content)
        assert any(f["type"] == "pem-private-key" for f in findings)


class TestScanContentIPsAndSerials:
    def test_detects_ip_with_credential(self):
        _, findings = scan_content("Connecting to root@192.168.1.1 via SSH")
        assert any(f["type"] == "ip-with-credential" for f in findings)

    def test_detects_adb_serial(self):
        _, findings = scan_content("Device abcdef0123456789 connected via adb")
        serial_findings = [f for f in findings if f["type"] == "adb-serial-warning"]
        assert len(serial_findings) >= 1


class TestScanContentClean:
    def test_clean_content_no_findings(self):
        content = "All 47 tests passed in 234.5s. No issues found."
        sanitized, findings = scan_content(content)
        assert len(findings) == 0
        assert sanitized == content

    def test_sha256_not_flagged(self):
        content = "sha256: a3f2b8c1d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1"
        _, findings = scan_content(content)
        # Should NOT be flagged because it's in a safe context (sha256:)
        hex_findings = [f for f in findings if "bare-hex" in f.get("type", "")]
        assert len(hex_findings) == 0

    def test_commit_hash_not_flagged(self):
        content = "commit a3f2b8c1d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9"
        _, findings = scan_content(content)
        hex_findings = [f for f in findings if "bare-hex" in f.get("type", "")]
        assert len(hex_findings) == 0


# --------------------------------------------------------------------------- #
# scan_content — multiple secrets in one content
# --------------------------------------------------------------------------- #


class TestScanContentMultiple:
    def test_multiple_secrets_all_redacted(self):
        content = (
            f'nsec1{"x" * 58}\n'
            f'password = "secret123"\n'
            'root@10.0.0.1\n'
        )
        sanitized, findings = scan_content(content)
        assert len(findings) >= 3
        assert "nsec1" not in sanitized
        assert "secret123" not in sanitized

    def test_redacted_placeholder_format(self):
        nsec = "nsec1" + "x" * 58
        sanitized, _ = scan_content(f"key={nsec}")
        assert "[REDACTED:" in sanitized


# --------------------------------------------------------------------------- #
# scan_file (integration with file I/O)
# --------------------------------------------------------------------------- #


class TestScanFile:
    def test_blocked_file_returns_none(self, tmp_path):
        f = tmp_path / "config.json"
        f.write_text('{"password": "secret"}')
        sanitized, findings = scan_file(str(f))
        assert sanitized is None
        assert findings[0]["type"] == "blocked-file"

    def test_clean_file_returns_content(self, tmp_path):
        f = tmp_path / "clean.txt"
        f.write_text("All tests passed.")
        sanitized, findings = scan_file(str(f))
        assert sanitized is not None
        assert len(findings) == 0

    def test_dirty_file_redacted(self, tmp_path):
        f = tmp_path / "log.txt"
        f.write_text(f'NOSTR_SECRET_KEY={"a" * 64}')
        sanitized, findings = scan_file(str(f))
        assert sanitized is not None
        assert len(findings) > 0
        assert "a" * 64 not in sanitized

    def test_json_file_not_scanned(self, tmp_path):
        f = tmp_path / "results.json"
        f.write_text('{"nsec": "nsec1' + "x" * 58 + '"}')
        sanitized, findings = scan_file(str(f))
        # JSON files skip content scanning
        assert len(findings) == 0


# --------------------------------------------------------------------------- #
# verify_content_clean / verify_clean
# --------------------------------------------------------------------------- #


class TestVerifyClean:
    def test_clean_content(self):
        assert verify_content_clean("Everything passed, no secrets.") is True

    def test_dirty_content(self):
        assert verify_content_clean(f'nsec1{"x" * 58}') is False

    def test_clean_file(self, tmp_path):
        f = tmp_path / "clean.txt"
        f.write_text("test output log")
        assert verify_clean(str(f)) is True

    def test_blocked_file_not_clean(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("PASSWORD=secret")
        assert verify_clean(str(f)) is False
