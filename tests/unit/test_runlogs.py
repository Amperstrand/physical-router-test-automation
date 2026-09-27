"""Unit tests for lib/runlogs.py (hardware-free).

Covers env-driven source selection, console-file snapshots with tail
capping, TCP console live streaming (against a local socket server), and
collector output naming. These sources exist so console history rides
with every story run — the evidence class that survives network-stack
failures (2026-09-27 br-lan collapse).
"""
import socket
import threading
import time
from pathlib import Path

from lib.runlogs import (
    ConsoleFileSource,
    ConsoleTcpSource,
    PhoneLogcatSource,
    RunLogCollector,
    RouterLogreadSource,
    default_sources,
)


def test_default_sources_env_driven():
    env = {
        "PHONE_SERIAL": "X",
        "TOLLGATE_SSH_HOST": "10.99.99.1",
        "TOLLGATE_CONSOLE_LOG": "/tmp/console.log",
        "TOLLGATE_CONSOLE_TCP": "127.0.0.1:4003",
        "TOLLGATE_LABGRID_CLIENT": "labgrid-client",
        "LG_COORDINATOR": "127.0.0.1:20408",
        "TOLLGATE_LABGRID_PLACES": "android-test, nr7101-router",
    }
    names = [s.name for s in default_sources(env)]
    assert "phone-logcat" in names
    assert "phone-logcat-key" in names
    assert "router-logread" in names
    assert any(n.startswith("console-") for n in names)
    assert names.count("labgrid-places") == 1
    assert "labgrid-resources-android-test" in names
    assert "labgrid-resources-nr7101-router" in names


def test_default_sources_minimal_env():
    assert default_sources({}) == []


def test_console_file_source_snapshot(tmp_path):
    src_file = tmp_path / "serial.log"
    src_file.write_text("boot line 1\nboot line 2\n")
    src = ConsoleFileSource(str(src_file), label="serial")
    assert src.available()
    out = tmp_path / "artifact.txt"
    src.collect(str(out))
    assert "boot line 2" in out.read_text()


def test_console_file_source_tail_cap(tmp_path):
    src_file = tmp_path / "big.log"
    filler = "x" * 1024
    src_file.write_text(filler * 1024)  # 1 MiB > 256 KiB cap
    out = tmp_path / "artifact.txt"
    ConsoleFileSource(str(src_file), label="big").collect(str(out))
    data = out.read_text()
    assert "tailed last" in data
    assert len(data) < 300 * 1024


def test_console_file_source_missing_file(tmp_path):
    src = ConsoleFileSource(str(tmp_path / "nope.log"), label="nope")
    assert not src.available()


def _tcp_server(server_path: Path, received: threading.Event):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)

    def accept_and_emit():
        conn, _ = srv.accept()
        time.sleep(0.2)
        conn.sendall(b"[    0.000000] kernel boot line\n")
        conn.sendall(b"netifd: interface 'lan' is now up\n")
        conn.close()
        received.set()

    threading.Thread(target=accept_and_emit, daemon=True).start()
    return srv, port


def test_console_tcp_source_live_stream(tmp_path):
    received = threading.Event()
    srv, port = _tcp_server(tmp_path, received)
    try:
        src = ConsoleTcpSource(f"127.0.0.1:{port}", label="probe")
        assert src.available()
        collector = RunLogCollector([src])
        collector.start_live(str(tmp_path / "live"))
        assert received.wait(5), "server never saw a connection"
        deadline = time.time() + 5
        while time.time() < deadline:
            live = tmp_path / "live" / "console-probe.live"
            if live.exists() and b"netifd" in live.read_bytes():
                break
            time.sleep(0.1)
        collector.stop_live()
        out = tmp_path / "artifact.txt"
        src.collect(str(out))
        assert "netifd: interface 'lan' is now up" in out.read_text()
    finally:
        srv.close()


def test_console_tcp_source_never_attached(tmp_path):
    src = ConsoleTcpSource("127.0.0.1:1", label="dead")
    out = tmp_path / "artifact.txt"
    src.collect(str(out))
    assert "never attached" in out.read_text()


def test_collector_collect_into_naming(tmp_path, monkeypatch):
    def fake_run(cmd, capture_output=True, timeout=30):
        class R:
            stdout = b"captured-by-fake\n"
            returncode = 0
        return R()

    monkeypatch.setattr("lib.runlogs.subprocess.run", fake_run)
    logread = tmp_path / "logread.log"
    logread.write_text("router log\n")
    collector = RunLogCollector([
        RouterLogreadSource("10.99.99.1"),
        PhoneLogcatSource("FAKESERIAL", key_slice=False),
        PhoneLogcatSource("FAKESERIAL", key_slice=True),
        ConsoleFileSource(str(logread), label="serial"),
    ])
    written = collector.collect_into(str(tmp_path / "out"), ts="101010")
    names = [Path(w).name for w in written]
    assert "router-logread-101010.txt" in names
    assert "phone-logcat-101010.txt" in names
    assert "phone-logcat-key-101010.txt" in names
    assert "console-serial-101010.txt" in names
    assert "router log" in (tmp_path / "out" / "console-serial-101010.txt").read_text()


def test_collector_skips_unavailable(tmp_path):
    missing = ConsoleFileSource(str(tmp_path / "absent.log"), label="absent")
    collector = RunLogCollector([missing])
    assert collector.collect_into(str(tmp_path / "out")) == []
