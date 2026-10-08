"""Unit tests for lib/preflight.py — environment autodetection (mocked SSH/subprocess)."""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from lib.preflight import (
    RouterProbe,
    SshError,
    apply_env_defaults,
    check_adb,
    check_docker,
    check_virtual_lab,
    compute_lanes,
    format_checklist,
    load_snapshot,
    make_ssh_runner,
    probe_router,
    run_preflight,
)


pytestmark = [pytest.mark.story("P11")]

def scripted_ssh(responses: dict, default: str = ""):
    def run_ssh(cmd: str) -> str:
        return responses.get(cmd, default)
    return run_ssh


HEALTHY_RESPONSES = {
    "true": "",
    "cat /etc/tollgate/brand 2>/dev/null": "net4sats\n",
    "uci -q get tollgate.device.code": "A1B2\n",
    "iwinfo 2>/dev/null | grep ESSID": 'ESSID: "TollGate-A1B2"\nESSID: "c08r4d0r-A1B2"\n',
    "uci show wireless 2>/dev/null | grep '\\.ssid='": "",
    ". /etc/openwrt_release 2>/dev/null && echo $DISTRIB_DESCRIPTION": "OpenWrt 24.10.1\n",
    "opkg list-installed tollgate-wrt 2>/dev/null | awk '{print $3}'": "0.6.0-alpha4\n",
    "pidof tollgate-wrt": "1234\n",
    "pidof nodogsplash": "1235\n",
}

NET4SATS_RESPONSES = dict(
    HEALTHY_RESPONSES,
    **{"iwinfo 2>/dev/null | grep ESSID": 'ESSID: "Net4sats-C830"\n'},
)


class TestProbeRouter:
    def test_healthy_router(self):
        probe = probe_router("alpha", {"sshHost": "10.0.0.1"}, run_ssh=scripted_ssh(HEALTHY_RESPONSES))
        assert probe.ssh_ok
        assert probe.brand == "net4sats"
        assert probe.device_code == "A1B2"
        assert probe.captive_ssid == "TollGate-A1B2"
        assert probe.private_ssid == "c08r4d0r-A1B2"
        assert probe.firmware == "OpenWrt 24.10.1"
        assert probe.tollgate_version == "0.6.0-alpha4"
        assert probe.tollgate_running
        assert probe.nodogsplash_running

    def test_unreachable_router(self):
        def down(cmd):
            raise SshError("exit 255: connect timeout")
        probe = probe_router("beta", {"sshHost": "10.0.0.2"}, run_ssh=down)
        assert not probe.ssh_ok
        assert "connect timeout" in probe.ssh_error
        assert not probe.tollgate_running

    def test_services_down(self):
        responses = dict(HEALTHY_RESPONSES, **{
            "pidof tollgate-wrt": "",
            "pidof nodogsplash": "",
        })
        probe = probe_router("alpha", {"sshHost": "10.0.0.1"}, run_ssh=scripted_ssh(responses))
        assert probe.ssh_ok
        assert not probe.tollgate_running
        assert not probe.nodogsplash_running

    def test_inventory_prefixes_used_for_classification(self):
        probe = probe_router(
            "alpha",
            {"sshHost": "10.0.0.1", "captiveSsidPrefixes": ["TollGate-", "Net4sats-"]},
            run_ssh=scripted_ssh(NET4SATS_RESPONSES),
        )
        assert probe.captive_ssid == "Net4sats-C830"

    def test_missing_entry_host(self):
        probe = probe_router("x", {}, run_ssh=None)
        assert not probe.ssh_ok
        assert "sshHost" in probe.ssh_error


class TestMakeSshRunner:
    @staticmethod
    def _runner_with_fake_subprocess(monkeypatch, entry, env, rc=0, stdout="ok\n"):
        import types
        argv_seen = {}

        def fake_run(argv, **kwargs):
            argv_seen["argv"] = argv
            return types.SimpleNamespace(returncode=rc, stderr="", stdout=stdout)

        monkeypatch.setattr("lib.preflight.subprocess.run", fake_run)
        runner = make_ssh_runner(entry, env=env)
        return runner, argv_seen

    def test_password_auth_uses_sshpass(self, monkeypatch):
        runner, seen = self._runner_with_fake_subprocess(
            monkeypatch, {"sshHost": "10.0.0.1"}, {"TOLLGATE_SSH_PASSWORD": "pw"})
        assert runner("true") == "ok\n"
        argv = seen["argv"]
        assert argv[:3] == ["sshpass", "-p", "pw"]
        assert argv[-1] == "true"
        assert argv[-2] == "root@10.0.0.1"

    def test_key_auth_flags(self, monkeypatch):
        runner, seen = self._runner_with_fake_subprocess(
            monkeypatch,
            {"sshHost": "10.0.0.9", "jumpHost": "j@1.2.3.4", "sshPort": 2222},
            {"TOLLGATE_SSH_KEY": "/k"},
        )
        runner("true")
        argv = seen["argv"]
        assert argv[0] == "ssh"
        assert "-i" in argv and "/k" in argv
        assert "-J" in argv and "j@1.2.3.4" in argv
        assert "-p" in argv and "2222" in argv

    def test_nonzero_exit_raises(self, monkeypatch):
        runner, _ = self._runner_with_fake_subprocess(
            monkeypatch, {"sshHost": "10.0.0.1"}, {}, rc=255)
        with pytest.raises(SshError):
            runner("true")

    def test_missing_host_raises(self):
        with pytest.raises(SshError):
            make_ssh_runner({"model": "x"}, env={})


class TestCheckAdb:
    def test_parses_devices(self):
        out = (
            "List of devices attached\n"
            "R5CR508MD9R          device usb:1-1 product:racer model:moto_g\n"
            "ZY326DPC7R           unauthorized usb:1-2\n"
        )
        status = check_adb(lambda argv: (0, out))
        assert status["available"]
        assert [d["serial"] for d in status["devices"]] == ["R5CR508MD9R", "ZY326DPC7R"]
        assert status["devices"][0]["state"] == "device"
        assert status["devices"][1]["state"] == "unauthorized"

    def test_adb_missing(self):
        assert check_adb(lambda argv: (127, ""))["available"] is False

    def test_no_devices(self):
        status = check_adb(lambda argv: (0, "List of devices attached\n\n"))
        assert status["available"]
        assert status["devices"] == []


class TestCheckDocker:
    def test_daemon_and_lab_images(self):
        images = (
            "cashubtc/nutshell:0.21.0\n"
            "cashubtc/mintd:0.18.0\n"
            "ubuntu:24.04\n"
            "cashubtc/mintd:0.17.6\n"
        )

        def run_cmd(argv):
            if argv[0] == "docker" and argv[1] == "info":
                return 0, "27.3.1\n"
            return 0, images

        status = check_docker(run_cmd)
        assert status["daemon"]
        assert status["server_version"] == "27.3.1"
        assert status["lab_images"] == ["cashubtc/mintd:0.17.6", "cashubtc/mintd:0.18.0", "cashubtc/nutshell:0.21.0"]

    def test_daemon_down(self):
        status = check_docker(lambda argv: (1, ""))
        assert status["cli"]
        assert not status["daemon"]
        assert status["lab_images"] == []

    def test_cli_missing(self):
        status = check_docker(lambda argv: (127, ""))
        assert not status["cli"]
        assert not status["daemon"]


class TestCheckVirtualLab:
    def _dead_pid(self):
        p = subprocess.Popen(["sleep", "60"])
        p.kill()
        p.wait()
        return p.pid

    def _write(self, run_dir, name, pid):
        with open(os.path.join(run_dir, name), "w") as f:
            f.write(str(pid))

    def test_alive_and_dead_pids(self, tmp_path):
        self._write(tmp_path, "tollgate.pid", os.getpid())
        self._write(tmp_path, "client.pid", self._dead_pid())
        status = check_virtual_lab(
            run_dir=str(tmp_path),
            run_cmd=lambda argv: (1, ""),
        )
        assert status["alive_pids"] == {"tollgate.pid": os.getpid()}
        assert status["bridge"] == ""
        assert status["alive"]  # one live pid suffices

    def test_bridge_up_without_pids(self, tmp_path):
        status = check_virtual_lab(
            run_dir=str(tmp_path),
            run_cmd=lambda argv: (0, "tg-poc-br"),
        )
        assert status["bridge"] == "tg-poc-br"
        assert status["alive"]

    def test_nothing_running(self, tmp_path):
        status = check_virtual_lab(
            run_dir=str(tmp_path),
            run_cmd=lambda argv: (1, ""),
        )
        assert not status["alive"]

    def test_garbage_pid_file(self, tmp_path):
        with open(os.path.join(tmp_path, "bad.pid"), "w") as f:
            f.write("not-a-pid")
        status = check_virtual_lab(run_dir=str(tmp_path), run_cmd=lambda argv: (1, ""))
        assert status["pid_files"]["bad.pid"] is None
        assert not status["alive"]


def _snapshot(routers=None, adb=None, docker=None, virtual_lab=None):
    return {
        "routers": routers or {},
        "adb": adb or {"available": True, "devices": []},
        "docker": docker or {"cli": True, "daemon": False},
        "virtual_lab": virtual_lab or {"alive": False},
    }


class TestComputeLanes:
    def _router(self, **kw):
        base = {"ssh_ok": True, "tollgate_running": True}
        base.update(kw)
        return base

    def test_all_down(self):
        lanes = compute_lanes(_snapshot())
        assert not any(l["runnable"] for l in lanes.values())

    def test_api_and_scenarios_need_running_tollgate(self):
        lanes = compute_lanes(_snapshot(routers={"a": self._router(tollgate_running=False)}))
        assert not lanes["api"]["runnable"]
        lanes = compute_lanes(_snapshot(routers={"a": self._router()}))
        assert lanes["api"]["runnable"]
        assert lanes["scenarios"]["runnable"]

    def test_phone_needs_adb_device_state(self):
        routers = {"a": self._router()}
        offline = _snapshot(routers=routers, adb={"available": True, "devices": [
            {"serial": "X", "state": "unauthorized", "description": ""}]})
        assert not compute_lanes(offline)["phone"]["runnable"]
        online = _snapshot(routers=routers, adb={"available": True, "devices": [
            {"serial": "X", "state": "device", "description": ""}]})
        assert compute_lanes(online)["phone"]["runnable"]

    def test_virtual_lab_and_cloud_lab(self):
        assert compute_lanes(_snapshot(virtual_lab={"alive": True}))["virtual-lab"]["runnable"]
        assert compute_lanes(_snapshot(docker={"cli": True, "daemon": True}))["cloud-lab"]["runnable"]

    def test_web_needs_ssh_only(self):
        lanes = compute_lanes(_snapshot(routers={"a": self._router(tollgate_running=False)}))
        assert lanes["web"]["runnable"]


class TestApplyEnvDefaults:
    def _probe(self, host, ok=True):
        return {"host": host, "ssh_ok": ok}

    def test_no_router_configured_picks_default_then_first(self):
        snap = {"default_router": "beta", "routers": {
            "alpha": self._probe("10.0.0.1"), "beta": self._probe("10.0.0.2")}}
        env = {}
        chosen, _ = apply_env_defaults(snap, env)
        assert chosen == "beta"
        assert env["TOLLGATE_SSH_HOST"] == "10.0.0.2"
        assert env["TOLLGATE_ROUTER_ID"] == "beta"

    def test_unreachable_configured_host_falls_back(self):
        snap = {"routers": {"alpha": self._probe("10.0.0.1"),
                            "beta": self._probe("10.0.0.2", ok=False)}}
        env = {"TOLLGATE_SSH_HOST": "10.0.0.2"}
        chosen, _ = apply_env_defaults(snap, env)
        assert chosen == "alpha"
        assert env["TOLLGATE_SSH_HOST"] == "10.0.0.1"

    def test_reachable_configured_host_untouched(self):
        snap = {"routers": {"alpha": self._probe("10.0.0.1")}}
        env = {"TOLLGATE_SSH_HOST": "10.0.0.1"}
        assert apply_env_defaults(snap, env)[0] is None
        assert env["TOLLGATE_SSH_HOST"] == "10.0.0.1"

    def test_host_absent_from_inventory_untouched(self):
        # e.g. the virtual-lab QEMU host — never judged, never overridden
        snap = {"routers": {"alpha": self._probe("10.0.0.1")}}
        env = {"TOLLGATE_SSH_HOST": "10.99.99.1"}
        assert apply_env_defaults(snap, env)[0] is None

    def test_no_reachable_routers_no_override(self):
        snap = {"routers": {"alpha": self._probe("10.0.0.1", ok=False)}}
        env = {}
        assert apply_env_defaults(snap, env)[0] is None
        assert "TOLLGATE_SSH_HOST" not in env

    def test_phone_serial_unset_picks_first_ready(self):
        snap = {"routers": {}, "adb": {"devices": [
            {"serial": "S1", "state": "device"}, {"serial": "S2", "state": "device"}]}}
        env = {}
        _, phone = apply_env_defaults(snap, env)
        assert phone == "S1"
        assert env["PHONE_SERIAL"] == "S1"

    def test_attached_serial_never_overridden(self):
        snap = {"routers": {}, "adb": {"devices": [
            {"serial": "S1", "state": "unauthorized"}, {"serial": "S2", "state": "device"}]}}
        env = {"PHONE_SERIAL": "S1"}
        assert apply_env_defaults(snap, env)[1] is None
        assert env["PHONE_SERIAL"] == "S1"

    def test_detached_serial_replaced(self):
        snap = {"routers": {}, "adb": {"devices": [{"serial": "S2", "state": "device"}]}}
        env = {"PHONE_SERIAL": "GONE"}
        assert apply_env_defaults(snap, env)[1] == "S2"
        assert env["PHONE_SERIAL"] == "S2"


class TestRunPreflight:
    def _inventory(self, tmp_path):
        inv = tmp_path / "routers.json"
        inv.write_text(json.dumps({
            "default": "alpha",
            "routers": {
                "alpha": {"sshHost": "10.0.0.1"},
                "beta": {"sshHost": "10.0.0.2"},
            },
        }))
        return str(inv)

    def test_end_to_end_snapshot_written(self, tmp_path):
        def factory(entry):
            if entry["sshHost"] == "10.0.0.2":
                def down(cmd):
                    raise SshError("exit 255: no route")
                return down
            return scripted_ssh(HEALTHY_RESPONSES)

        out = tmp_path / "preflight.json"
        snap = run_preflight(
            inventory_path=self._inventory(tmp_path),
            out_path=str(out),
            ssh_runner_factory=factory,
            run_cmd=lambda argv: (1, ""),
            virtual_lab_run_dir=str(tmp_path / "no-run-dir"),
        )
        assert snap["routers"]["alpha"]["ssh_ok"] is True
        assert snap["routers"]["beta"]["ssh_ok"] is False
        assert snap["default_router"] == "alpha"
        assert set(snap["lanes"]) == {"api", "phone", "web", "scenarios", "virtual-lab", "cloud-lab"}
        assert snap["lanes"]["api"]["runnable"]  # alpha healthy

        written = json.loads(out.read_text())
        assert written["routers"]["alpha"]["captive_ssid"] == "TollGate-A1B2"
        assert written == snap  # what ran is exactly what was persisted

    def test_missing_inventory_yields_empty_routers(self, tmp_path):
        snap = run_preflight(
            inventory_path=str(tmp_path / "absent.json"),
            out_path=str(tmp_path / "out.json"),
            run_cmd=lambda argv: (1, ""),
        )
        assert snap["routers"] == {}
        assert snap["inventory_path"] == ""
        assert not snap["lanes"]["api"]["runnable"]


class TestLoadSnapshot:
    def _write(self, path, epoch):
        path.write_text(json.dumps({
            "schema_version": 1,
            "generated_at": "x",
            "generated_at_epoch": epoch,
            "routers": {},
        }))

    def test_fresh(self, tmp_path):
        p = tmp_path / "s.json"
        self._write(p, time.time())
        assert load_snapshot(str(p)) is not None

    def test_stale(self, tmp_path):
        p = tmp_path / "s.json"
        self._write(p, time.time() - 3600)
        assert load_snapshot(str(p), max_age_seconds=300) is None

    def test_missing(self, tmp_path):
        assert load_snapshot(str(tmp_path / "nope.json")) is None

    def test_corrupt(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text("{not json")
        assert load_snapshot(str(p)) is None

    def test_no_epoch_is_stale(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"routers": {}}))
        assert load_snapshot(str(p)) is None


class TestFormatChecklist:
    def test_contains_rows_for_everything(self):
        snap = {
            "generated_at": "2026-09-28 10:00:00",
            "inventory_path": "config/routers.json",
            "routers": {
                "alpha": {"ssh_ok": True, "host": "10.0.0.1", "brand": "net4sats",
                          "device_code": "A1B2", "captive_ssid": "Net4sats-A1B2",
                          "private_ssid": "c08r4d0r-A1B2", "firmware": "OpenWrt 24.10.1",
                          "tollgate_version": "0.6.0", "tollgate_running": True,
                          "nodogsplash_running": True, "ssids": ["Net4sats-A1B2"]},
                "beta": {"ssh_ok": False, "host": "10.0.0.2", "ssh_error": "timeout"},
            },
            "adb": {"available": True, "devices": [{"serial": "S1", "state": "device",
                                                    "description": "model:x"}]},
            "docker": {"cli": True, "daemon": False},
            "virtual_lab": {"alive": False, "run_dir": "/nope"},
            "lanes": compute_lanes(_snapshot(
                routers={"r": {"ssh_ok": True, "tollgate_running": True}})),
        }
        text = format_checklist(snap)
        assert "alpha @ 10.0.0.1" in text
        assert "Net4sats-A1B2" in text
        assert "[FAIL] beta @ 10.0.0.2" in text
        assert "S1 device" in text
        for lane in ("api", "phone", "web", "scenarios", "virtual-lab", "cloud-lab"):
            assert lane in text
        assert "YES " in text and "NO  " in text
