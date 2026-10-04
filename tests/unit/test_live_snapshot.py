"""Unit tests for scripts/live-snapshot.py (F4) — pure logic, no SSH.

Run: python3 -m pytest tests/unit/test_live_snapshot.py -v
"""

import importlib.util
import json
import os

import pytest

SCRIPTS_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
_spec = importlib.util.spec_from_file_location(
    "live_snapshot", os.path.join(SCRIPTS_DIR, "live-snapshot.py"))
_ls = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ls)


def write_snap(path, config='{"accepted_mints": []}', uci="network.lan.ipaddr='192.168.1.1'\n",
               mints_md5=None):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "config.json"), "w") as f:
        f.write(config)
    with open(os.path.join(path, "uci-show.txt"), "w") as f:
        f.write(uci)
    meta = {
        "host": "192.0.2.1",
        "captured_at": "t0",
        "file_md5": {
            "/etc/tollgate/config.json": mints_md5 or "aaa",
            "/etc/tollgate/wallet.db": "bbb",
        },
    }
    with open(os.path.join(path, "meta.json"), "w") as f:
        json.dump(meta, f)
    return path


def test_identical_snapshots_are_clean(tmp_path):
    base = write_snap(str(tmp_path / "base"))
    cur = write_snap(str(tmp_path / "cur"))
    assert _ls.compare_snapshots(base, cur) == ([], [])


def test_config_change_is_flagged_with_diff(tmp_path):
    base = write_snap(str(tmp_path / "base"))
    cur = write_snap(str(tmp_path / "cur"),
                     config='{"accepted_mints": [{"url": "https://x"}]}')
    strict, volatile = _ls.compare_snapshots(base, cur)
    assert [d[1] for d in strict] == ["config.json"]
    assert "https://x" in strict[0][2]
    assert volatile == []


def test_wallet_db_hash_change_flagged_via_meta(tmp_path):
    base = write_snap(str(tmp_path / "base"))
    cur = write_snap(str(tmp_path / "cur"), mints_md5="changed")
    strict, _ = _ls.compare_snapshots(base, cur)
    assert strict[0][0] == "changed"
    assert strict[0][1] == "meta.json"
    assert "/etc/tollgate/config.json: aaa -> changed" in strict[0][2]


def test_volatile_captures_do_not_count_as_drift(tmp_path):
    base = write_snap(str(tmp_path / "base"))
    cur = write_snap(str(tmp_path / "cur"),
                     uci="network.lan.ipaddr='192.168.1.1'\n")
    with open(os.path.join(cur, "ndsctl-status.txt"), "w") as f:
        f.write("State: Authenticated")
    with open(os.path.join(cur, "nft-ruleset.txt"), "w") as f:
        f.write("counter packets 999")
    strict, volatile = _ls.compare_snapshots(base, cur)
    assert strict == []
    assert {d[1] for d in volatile} == {"ndsctl-status.txt", "nft-ruleset.txt"}
    reverse_strict, _ = _ls.compare_snapshots(cur, base)
    assert reverse_strict == []


def test_unexpected_file_is_strict(tmp_path):
    base = write_snap(str(tmp_path / "base"))
    cur = write_snap(str(tmp_path / "cur"))
    with open(os.path.join(cur, "etc-tollgate-config.json.new"), "w") as f:
        f.write("{}")
    strict, _ = _ls.compare_snapshots(base, cur)
    assert strict[0][:2] == ("added", "etc-tollgate-config.json.new")


def test_meta_timestamp_only_change_is_clean(tmp_path):
    # captured_at differences alone must NOT count as drift (always differ)
    base = write_snap(str(tmp_path / "base"))
    cur = write_snap(str(tmp_path / "cur"))
    with open(os.path.join(cur, "meta.json")) as f:
        meta = json.load(f)
    meta["captured_at"] = "2099-01-01T00:00:00"
    with open(os.path.join(cur, "meta.json"), "w") as f:
        json.dump(meta, f)
    assert _ls.compare_snapshots(base, cur) == ([], [])


def test_format_diffs(tmp_path):
    base = write_snap(str(tmp_path / "base"))
    cur = write_snap(str(tmp_path / "cur"), uci="network.lan.ipaddr='10.0.0.1'\n")
    out = _ls.format_diffs(*_ls.compare_snapshots(base, cur))
    assert "DRIFT" in out and "uci-show.txt" in out
    assert _ls.format_diffs([], []) == "CLEAN — no drift detected"


def test_capture_writes_all_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(_ls, "ssh_run", lambda host, cmd, key=None, timeout=30:
                        "canned-output\n" if "md5sum" not in cmd else "d41d8cd98f00b204e9800998ecf8427e  file")
    out = _ls.capture("192.0.2.1", None, str(tmp_path / "snap"))
    names = set(_ls.snapshot_paths(out))
    assert {"config.json", "uci-show.txt", "ndsctl-status.txt", "nft-ruleset.txt",
            "processes.txt", "version.txt", "meta.json"} == names
    with open(os.path.join(out, "config.json")) as f:
        assert f.read() == "canned-output\n"
    with open(os.path.join(out, "meta.json")) as f:
        assert json.load(f)["file_md5"]["/etc/tollgate/wallet.db"] == "d41d8cd98f00b204e9800998ecf8427e"
