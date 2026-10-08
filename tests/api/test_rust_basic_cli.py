import json
import socket

import pytest

pytestmark = [pytest.mark.story("R1"),pytest.mark.rust_basic_only, pytest.mark.api, pytest.mark.smoke]


def _send_cli_command(socket_path, command, timeout=5):
    """Send a line-delimited command to the Unix socket, return response text."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(socket_path)
        s.sendall((command + "\n").encode())
        chunks = []
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
        return b"".join(chunks).decode(errors="replace")


def test_cli_version(rust_basic_server):
    """S5: 'version' command returns multi-line text with version info."""
    resp = _send_cli_command(rust_basic_server["socket_path"], "version")
    assert "version:" in resp.lower(), f"Missing 'version:' in response: {resp!r}"
    assert "0.1.0" in resp, f"Missing version 0.1.0 in response: {resp!r}"


def test_cli_status(rust_basic_server):
    """status command returns JSON {success: true, message: 'running'}."""
    resp = _send_cli_command(rust_basic_server["socket_path"], "status")
    data = json.loads(resp.strip())
    assert data.get("success") is True, f"Expected success=true: {data!r}"
    assert data.get("message") == "running", f"Expected message='running': {data!r}"


def test_cli_unknown_command(rust_basic_server):
    """S8: Unknown command returns {success: false, error: 'unknown command: ...'}."""
    resp = _send_cli_command(rust_basic_server["socket_path"], "frobnicate")
    data = json.loads(resp.strip())
    assert data.get("success") is False, f"Expected success=false: {data!r}"
    err = data.get("error", "")
    assert "unknown command" in err.lower(), f"Expected 'unknown command' in error: {data!r}"
    assert "frobnicate" in err, f"Expected 'frobnicate' in error: {data!r}"
