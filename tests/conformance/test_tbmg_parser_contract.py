"""O4 rail: the config-loader contract is pinned, not aspirational.

Mirrors tbmg's parser table (src/config_manager/min_steps_test.go,
TestMintConfigMinStepsDefault) and the bad-JSON loader row
(EnsureDefaultConfig: invalid JSON -> backup + rewrite defaults + serve
defaults — NOT a hard fail) at PRTA level.

- Always runs: the vendored table (tbmg/min_steps_table.json) is valid,
  self-consistent, and every row's expectation satisfies the documented
  invariant (min_steps >= 1 after parse — cashud rejects 0).
- With TBMG_CHECKOUT set: the table rows are diffed against the live
  fork test file (drift fails loudly), and the fork's own table test is
  executed via `go test` — the real parser, not a reimplementation.
- The fail-loud ASPIRATION (O4's end state) stays in the gap register:
  the loader currently degrades bad JSON to defaults-with-backup; when
  tbmg hardens it, this rail's `loader_bad_json` row and the go-test
  gate are the tripwire that must be updated in the same change.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.story("O4"), pytest.mark.api, pytest.mark.extended]

HERE = Path(__file__).parent
TABLE = json.loads((HERE / "tbmg" / "min_steps_table.json").read_text())
TBMG = Path(__file__).parent.parent.parent / "conformance-tbmg-checkout"
GO_TEST = ["go", "test", "-run", "TestMintConfigMinStepsDefault"]

ROW_RE = re.compile(
    r'\{"([^"]+)", `(\{[^`]+\})`, (\d+)\}'
)


def test_table_rows_parse_and_provenance_present():
    rows = TABLE["rows"]
    assert len(rows) >= 6
    assert TABLE["provenance"]["test"] == "TestMintConfigMinStepsDefault"
    names = [r["name"] for r in rows]
    assert len(names) == len(set(names))
    for row in rows:
        json.loads(row["json"])  # each fixture is valid JSON itself


def test_table_never_produces_min_steps_below_one():
    # The client-side contract: cashud/wally reject advertisements with
    # min_steps=0, so every parse outcome in the table must be >= 1.
    assert all(row["want"] >= 1 for row in TABLE["rows"])
    assert {"absent key defaults to 1", "purchase_min_steps=0 defaults to 1"} <= {
        row["name"] for row in TABLE["rows"]
    }


def test_loader_bad_json_row_documents_current_contract():
    contract = TABLE["loader_bad_json"]["contract"]
    assert "backup" in contract and "defaults" in contract, (
        "the current loader contract (backup + defaults on bad JSON) must stay "
        "documented here; if tbmg made the loader fail loud, update this row "
        "and the gap register together"
    )


def _checkout() -> Path | None:
    import os

    env = os.environ.get("TBMG_CHECKOUT", "").strip()
    if env and Path(env).is_dir():
        return Path(env)
    default = Path.home() / "src/tollgate-module-basic-go"
    marker = default / "src/config_manager/min_steps_test.go"
    return default if marker.exists() else None


@pytest.mark.skipif(_checkout() is None, reason="no tbmg checkout (TBMG_CHECKOUT or ~/src default)")
def test_table_matches_fork_test_file():
    proc = subprocess.run(
        ["git", "-C", str(_checkout()), "show", "fork/main:src/config_manager/min_steps_test.go"],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        pytest.skip("checkout has no fork/main ref — drift check needs the fork remote fetched")
    fork_rows = [
        {"name": m.group(1), "json": m.group(2), "want": int(m.group(3))}
        for m in ROW_RE.finditer(proc.stdout)
    ]
    assert fork_rows, "could not parse table rows out of the fork test file (format changed?)"
    assert fork_rows == TABLE["rows"], (
        "tbmg parser table drifted — refresh tbmg/min_steps_table.json"
    )


@pytest.mark.skipif(_checkout() is None, reason="no tbmg checkout (TBMG_CHECKOUT or ~/src default)")
@pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain absent")
def test_fork_parser_table_green_via_go_test():
    proc = subprocess.run(
        GO_TEST, cwd=_checkout() / "src/config_manager", capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, f"go test failed:\n{proc.stdout}\n{proc.stderr}"
