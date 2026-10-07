"""Unit tests for lib/labgrid_vlab.py — the VM-as-labgrid-place venue.

No labgrid package, no coordinator, no network: script generation is
asserted on content, the client helpers on fake runners.
"""
from __future__ import annotations

import subprocess

import pytest

from lib.labgrid_vlab import (
    DEFAULT_LANE,
    VlabExportError,
    VlabExportSpec,
    exporter_config_yaml,
    export_start_script,
    export_stop_script,
    parse_resources,
    place_names,
    resolve_serial,
    serial_ports,
)


class TestLane:
    def test_default_places(self):
        assert place_names() == ("vlab-default-owrt", "vlab-default-client")

    def test_lane_slug(self):
        assert place_names("rc1") == ("vlab-rc1-owrt", "vlab-rc1-client")

    @pytest.mark.parametrize("bad", ["", "Upper", "sp ace", "un/der", "-x", "a" * 64])
    def test_bad_lane_rejected(self, bad):
        with pytest.raises(VlabExportError):
            place_names(bad)


class TestPorts:
    def test_base_ports(self):
        assert serial_ports(0) == (46000, 46001)

    def test_lane_offsets(self):
        assert serial_ports(7) == (46014, 46015)

    @pytest.mark.parametrize("bad", [-1, 1000, 10_000])
    def test_lane_num_bounds(self, bad):
        with pytest.raises(VlabExportError):
            serial_ports(bad)


class TestExporterConfig:
    def test_yaml_structure(self):
        spec = VlabExportSpec(lane="rc1", serial_port_owrt=46014,
                              serial_port_client=46015)
        text = exporter_config_yaml(spec)
        assert "vlab-rc1-owrt:" in text and "vlab-rc1-client:" in text
        assert "port: 46014" in text and "port: 46015" in text
        assert "username: root" in text and "username: debian" in text
        # raw serial: no speed field on the NetworkSerialPort
        nsp = text.split("NetworkSerialPort:")[1]
        assert "speed" not in nsp.split("NetworkService")[0]

    def test_yaml_parses_and_places_match(self):
        yaml = pytest.importorskip("yaml")
        spec = VlabExportSpec(lane="rc1")
        cfg = yaml.safe_load(exporter_config_yaml(spec))
        assert set(cfg) == set(spec.places)
        owrt = cfg[spec.places[0]]
        assert owrt["NetworkService"]["address"] == spec.owrt_address
        assert owrt["NetworkSerialPort"]["host"] == spec.serial_host


class TestStartScript:
    SPEC = VlabExportSpec(lane="rc1", coordinator="ai-legion:20408",
                          serial_port_owrt=46014, serial_port_client=46015)
    SCRIPT = export_start_script(SPEC, "/tmp/vlab", "/tmp/vlab/run/serial.sock",
                                 "/tmp/vlab/run/serial-client.sock")

    def test_workdir_expands_at_runtime(self):
        assert 'workdir=$(eval printf' in self.SCRIPT
        assert 'run="$workdir/run"' in self.SCRIPT

    def test_relays_reference_own_sockets_and_ports(self):
        # literals live at the ensure_relay call sites; the body binds $2/$3
        assert ('ensure_relay "$run/labgrid-socat-owrt.pid" 46014 '
                "/tmp/vlab/run/serial.sock") in self.SCRIPT
        assert ('ensure_relay "$run/labgrid-socat-client.pid" 46015 '
                "/tmp/vlab/run/serial-client.sock") in self.SCRIPT
        assert "TCP-LISTEN:$port,bind=127.0.0.1,reuseaddr,fork" in self.SCRIPT
        assert 'UNIX-CONNECT:"$sock"' in self.SCRIPT

    def test_exporter_spawned_with_lane_name_and_coordinator(self):
        assert "labgrid-exporter -c ai-legion:20408 -n vlab-rc1" in self.SCRIPT
        assert '"$run/labgrid-exporter.yaml"' in self.SCRIPT

    def test_idempotent_relay_reuse_via_pidfile_marker(self):
        assert 'grep -qa "rc1" /proc/$(cat "$pidfile")/cmdline' in self.SCRIPT

    def test_probe_ports_before_success(self):
        assert "for port in 46014 46015; do" in self.SCRIPT
        assert "/dev/tcp/127.0.0.1/$port" in self.SCRIPT

    def test_old_exporter_killed_before_respawn(self):
        # the exporter pidfile check must run BEFORE the new nohup spawn
        kill_pos = self.SCRIPT.find("labgrid-exporter.pid")
        spawn_pos = self.SCRIPT.find("nohup labgrid-exporter")
        assert 0 < kill_pos < spawn_pos

    def test_announces_place_names(self):
        assert "vlab-rc1-owrt" in self.SCRIPT and "vlab-rc1-client" in self.SCRIPT


class TestStopScript:
    def test_kills_all_three_by_pidfile_with_marker(self):
        s = export_stop_script(VlabExportSpec(lane="rc1"), "/tmp/vlab")
        for pidfile in ("labgrid-exporter.pid", "labgrid-socat-owrt.pid",
                        "labgrid-socat-client.pid"):
            assert pidfile in s
        assert s.count('grep -qa "rc1"') == 3
        # kill-never-by-pattern: no pkill/killall anywhere
        assert "pkill" not in s and "killall" not in s


class TestParseResources:
    def test_quoted_and_bare_values(self):
        out = ("NetworkSerialPort: host='127.0.0.1', port=46000\n"
               "NetworkService: address='10.99.99.1', port=22, username='root'\n")
        res = parse_resources(out)
        assert res["NetworkSerialPort"] == {"host": "127.0.0.1", "port": "46000"}
        assert res["NetworkService"]["username"] == "root"

    def test_ignores_noise(self):
        out = "Place 'x' is not acquired\n\nsome banner: no fields here\n"
        assert parse_resources(out) == {}


class _FakeRunner:
    def __init__(self, rc=0, stdout="", stderr=""):
        self._rc, self._out, self._err = rc, stdout, stderr
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=self._rc,
                                           stdout=self._out, stderr=self._err)


class TestResolveSerial:
    def test_ok(self):
        r = _FakeRunner(stdout="NetworkSerialPort: host='127.0.0.1', port=46014\n")
        assert resolve_serial("c:1", "p", runner=r) == ("127.0.0.1", 46014)
        assert r.calls[0][:2] == ["labgrid-client", "-x"]

    def test_client_failure_raises(self):
        r = _FakeRunner(rc=1, stderr="place not found")
        with pytest.raises(VlabExportError, match="place not found"):
            resolve_serial("c:1", "p", runner=r)

    def test_missing_serial_port_raises(self):
        r = _FakeRunner(stdout="NetworkService: address='10.0.0.1', port=22\n")
        with pytest.raises(VlabExportError, match="no NetworkSerialPort"):
            resolve_serial("c:1", "p", runner=r)
