"""Unit tests for scripts/collect-results.py.

Imports parsing functions directly (no subprocess) for speed and coverage.
Run: python3 -m pytest tests/unit/test_collect_results.py -v
"""

import json
import importlib.util
import os
import subprocess
import sys

import pytest

SCRIPTS_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
_spec = importlib.util.spec_from_file_location(
    "collect_results",
    os.path.join(SCRIPTS_DIR, "collect-results.py"),
)
_cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cr)

determine_overall_status = _cr.determine_overall_status
merge_counts = _cr.merge_counts
parse_junit = _cr.parse_junit
parse_playwright = _cr.parse_playwright
parse_pytest_log = _cr.parse_pytest_log


JUNIT_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="api" tests="5" failures="1" errors="1" skipped="1" time="10.5">
    <testcase classname="tests.api.test_health" name="test_health_endpoint" time="0.5"/>
    <testcase classname="tests.api.test_info" name="test_info_endpoint" time="0.3">
      <failure message="assert 200 == 500">Expected 200 got 500</failure>
    </testcase>
    <testcase classname="tests.api.test_error" name="test_error_case" time="0.1">
      <error message="RuntimeError: connection timeout">Connection timed out after 30s</error>
    </testcase>
    <testcase classname="tests.api.test_skip" name="test_skipped_case" time="0.0">
      <skipped message="feature not available" type="pytest.skip">Skipped: feature not available</skipped>
    </testcase>
    <testcase classname="tests.api.test_pass" name="test_another_pass" time="0.2"/>
  </testsuite>
</testsuites>
"""

PLAYWRIGHT_JSON = {
    "stats": {
        "expected": 3,
        "unexpected": 1,
        "flaky": 1,
        "skipped": 1,
        "duration": 15000,
    },
    "suites": [
        {
            "suites": [],
            "specs": [
                {
                    "title": "dashboard loads",
                    "file": "tests/web/tollgate.spec.mjs",
                    "tests": [
                        {
                            "results": [{"status": "expected", "duration": 3000}],
                            "annotations": [],
                            "status": "expected",
                        }
                    ],
                },
                {
                    "title": "network tab works",
                    "file": "tests/web/tollgate.spec.mjs",
                    "tests": [
                        {
                            "results": [
                                {
                                    "status": "unexpected",
                                    "duration": 5000,
                                    "error": {"message": "Button not found"},
                                }
                            ],
                            "annotations": [],
                            "status": "unexpected",
                        }
                    ],
                },
                {
                    "title": "config saves",
                    "file": "tests/web/tollgate.spec.mjs",
                    "tests": [
                        {
                            "results": [{"status": "expected", "duration": 2000}],
                            "annotations": [],
                            "status": "expected",
                        }
                    ],
                },
                {
                    "title": "fund modal",
                    "file": "tests/web/tollgate.spec.mjs",
                    "tests": [
                        {
                            "results": [{"status": "flaky", "duration": 4000}],
                            "annotations": [],
                            "status": "flaky",
                        }
                    ],
                },
                {
                    "title": "drain modal",
                    "file": "tests/web/tollgate.spec.mjs",
                    "tests": [
                        {
                            "results": [{"status": "skipped", "duration": 0}],
                            "annotations": [],
                            "status": "skipped",
                        }
                    ],
                },
            ],
        }
    ],
}


pytestmark = [pytest.mark.story("P5")]

@pytest.fixture
def junit_file(tmp_path):
    p = tmp_path / "junit.xml"
    p.write_text(JUNIT_XML)
    return str(p)


@pytest.fixture
def playwright_file(tmp_path):
    p = tmp_path / "results.json"
    p.write_text(json.dumps(PLAYWRIGHT_JSON))
    return str(p)


def _run_collect(tmp_path, extra_args):
    script = os.path.join(SCRIPTS_DIR, "collect-results.py")
    cmd = [sys.executable, script, "--run-dir", str(tmp_path)]
    cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True)


def test_parse_junit_passes(junit_file):
    runner, _ = parse_junit(junit_file, "api")
    assert runner["counts"]["total"] == 5
    assert runner["counts"]["passed"] == 2
    assert runner["counts"]["failed"] == 1
    assert runner["counts"]["errors"] == 1
    assert runner["counts"]["skipped"] == 1
    assert runner["counts"]["flaky"] == 0


def test_parse_junit_per_test_outcomes(junit_file):
    _, tests = parse_junit(junit_file, "api")
    by_name = {t["name"]: t for t in tests}

    assert by_name["test_health_endpoint"]["outcome"] == "passed"
    assert by_name["test_health_endpoint"]["duration_ms"] == 500

    assert by_name["test_info_endpoint"]["outcome"] == "failed"
    assert by_name["test_info_endpoint"]["duration_ms"] == 300

    assert by_name["test_error_case"]["outcome"] == "error"
    assert by_name["test_error_case"]["duration_ms"] == 100

    assert by_name["test_skipped_case"]["outcome"] == "skipped"
    assert by_name["test_another_pass"]["outcome"] == "passed"
    assert by_name["test_another_pass"]["duration_ms"] == 200


def test_parse_junit_failure_messages(junit_file):
    _, tests = parse_junit(junit_file, "api")
    by_name = {t["name"]: t for t in tests}

    assert by_name["test_info_endpoint"]["failure_message"] is not None
    assert "200" in by_name["test_info_endpoint"]["failure_message"]

    assert by_name["test_error_case"]["failure_message"] is not None
    assert "timeout" in by_name["test_error_case"]["failure_message"].lower()

    assert by_name["test_health_endpoint"]["failure_message"] is None


def test_parse_playwright_counts(playwright_file):
    runner, _ = parse_playwright(playwright_file, "browser")
    assert runner["counts"]["total"] == 6
    assert runner["counts"]["passed"] == 3
    assert runner["counts"]["failed"] == 1
    assert runner["counts"]["flaky"] == 1
    assert runner["counts"]["skipped"] == 1
    assert runner["counts"]["errors"] == 0
    assert runner["duration_ms"] == 15000


def test_parse_playwright_per_test_outcomes(playwright_file):
    _, tests = parse_playwright(playwright_file, "browser")
    by_name = {t["name"]: t for t in tests}

    assert by_name["dashboard loads"]["outcome"] == "passed"
    assert by_name["dashboard loads"]["duration_ms"] == 3000

    assert by_name["network tab works"]["outcome"] == "failed"
    assert by_name["network tab works"]["duration_ms"] == 5000

    assert by_name["config saves"]["outcome"] == "passed"
    assert by_name["fund modal"]["outcome"] == "flaky"
    assert by_name["fund modal"]["duration_ms"] == 4000

    assert by_name["drain modal"]["outcome"] == "skipped"
    assert by_name["drain modal"]["duration_ms"] == 0


def test_parse_playwright_failure_message(playwright_file):
    _, tests = parse_playwright(playwright_file, "browser")
    failed = [t for t in tests if t["outcome"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["failure_message"] == "Button not found"


def test_multi_runner_merge(junit_file, playwright_file):
    runner_j, tests_j = parse_junit(junit_file, "api")
    runner_p, tests_p = parse_playwright(playwright_file, "browser")
    runners = [runner_j, runner_p]
    counts = merge_counts(runners)

    assert counts["total"] == 11
    assert counts["passed"] == 5
    assert counts["failed"] == 2
    assert counts["errors"] == 1
    assert counts["skipped"] == 2
    assert counts["flaky"] == 1


def test_status_passed():
    runner = {
        "status": "passed",
        "counts": {"failed": 0, "errors": 0},
    }
    assert determine_overall_status([runner]) == "passed"


def test_status_failed():
    runner = {
        "status": "failed",
        "counts": {"failed": 1, "errors": 0},
    }
    assert determine_overall_status([runner]) == "failed"


def test_status_errored_no_runners():
    assert determine_overall_status([]) == "errored"


def test_parse_pytest_log_basic():
    log = (
        "api/test_health.py::test_root_endpoint PASSED                            [ 27%]\n"
        "api/test_health.py::test_pay_endpoint FAILED                             [ 50%]\n"
        "api/test_info.py::test_info_endpoint PASSED                              [ 36%]\n"
        "api/test_edge.py::test_skipped SKIPPED (reason text)                     [ 40%]\n"
        "api/test_edge.py::test_error ERROR                                       [ 45%]\n"
    )
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        f.write(log)
        f.flush()
        runner, tests = parse_pytest_log(f.name, "api")
    os.unlink(f.name)

    assert runner["counts"]["total"] == 5
    assert runner["counts"]["passed"] == 2
    assert runner["counts"]["failed"] == 1
    assert runner["counts"]["errors"] == 1
    assert runner["counts"]["skipped"] == 1
    assert runner["status"] == "failed"  # failed takes priority over errored
    assert len(tests) == 5

    by_name = {t["name"]: t for t in tests}
    assert by_name["test_root_endpoint"]["outcome"] == "passed"
    assert by_name["test_pay_endpoint"]["outcome"] == "failed"
    assert by_name["test_skipped"]["outcome"] == "skipped"
    assert by_name["test_error"]["outcome"] == "error"


def test_missing_junit_file(tmp_path):
    missing = str(tmp_path / "nonexistent.xml")
    assert not os.path.isfile(missing)
    result = _run_collect(tmp_path, ["--pytest", f"api={missing}"])
    assert result.returncode == 2


def test_missing_junit_falls_back_to_output_log(tmp_path):
    log_content = (
        "api/test_health.py::test_root_endpoint PASSED                            [ 27%]\n"
        "api/test_health.py::test_pay_endpoint FAILED                             [ 50%]\n"
        "api/test_info.py::test_info SKIPPED (reason here)\n"
    )
    log_path = tmp_path / "output.log"
    log_path.write_text(log_content)

    missing = str(tmp_path / "nonexistent.xml")
    assert not os.path.isfile(missing)

    result = _run_collect(tmp_path, ["--pytest", "api=nonexistent.xml", "--allow-failures"])
    assert result.returncode == 0

    run_json = json.loads((tmp_path / "run.json").read_text())
    assert run_json["counts"]["total"] == 3
    assert run_json["counts"]["passed"] == 1
    assert run_json["counts"]["failed"] == 1
    assert run_json["counts"]["skipped"] == 1
    assert len(run_json["runners"]) == 1
    assert run_json["runners"][0]["name"] == "api"
    assert run_json["runners"][0]["counts"]["total"] == 3


def test_missing_playwright_file(tmp_path):
    missing = str(tmp_path / "nonexistent.json")
    assert not os.path.isfile(missing)
    result = _run_collect(tmp_path, ["--playwright", f"browser={missing}"])
    assert result.returncode == 2


def test_run_json_schema(junit_file, tmp_path):
    rel = "junit.xml"
    src = tmp_path / rel
    src.write_text(JUNIT_XML)

    result = _run_collect(tmp_path, [
        "--pytest", f"api={rel}",
        "--run-id", "test-run-001",
        "--started-at", "2026-05-16T17:26:00Z",
        "--finished-at", "2026-05-16T17:26:25Z",
        "--allow-failures",
    ])
    assert result.returncode == 0

    run_json = json.loads((tmp_path / "run.json").read_text())
    required_keys = [
        "schema_version", "run_id", "status", "started_at", "finished_at",
        "duration_ms", "test_plan", "sut", "test_suite", "lab", "counts", "runners",
    ]
    for key in required_keys:
        assert key in run_json, f"Missing key: {key}"

    assert run_json["schema_version"] == 1
    assert run_json["run_id"] == "test-run-001"
    assert isinstance(run_json["runners"], list)
    assert len(run_json["runners"]) == 1
    assert run_json["runners"][0]["framework"] == "pytest"
    assert run_json["runners"][0]["name"] == "api"

    sut_keys = ["repo", "commit", "commit_short", "branch", "pr", "backend", "installed_version"]
    for key in sut_keys:
        assert key in run_json["sut"], f"Missing sut key: {key}"

    lab_keys = ["router_id", "router_model", "router_arch", "router_ip", "client_type", "viewport", "virtual_lab"]
    for key in lab_keys:
        assert key in run_json["lab"], f"Missing lab key: {key}"


def test_summary_json_schema(junit_file, tmp_path):
    rel = "junit.xml"
    src = tmp_path / rel
    src.write_text(JUNIT_XML)

    result = _run_collect(tmp_path, [
        "--pytest", f"api={rel}",
        "--run-id", "test-run-002",
        "--allow-failures",
    ])
    assert result.returncode == 0

    summary = json.loads((tmp_path / "summary.json").read_text())
    required_keys = ["run_id", "status", "counts", "runners", "tests", "failed_tests", "skipped_tests"]
    for key in required_keys:
        assert key in summary, f"Missing key: {key}"

    assert isinstance(summary["tests"], list)
    assert len(summary["tests"]) == 5
    for t in summary["tests"]:
        for tk in ["runner", "framework", "name", "file", "outcome", "duration_ms", "failure_message", "markers"]:
            assert tk in t, f"Missing test key: {tk}"


def test_summary_failed_tests(junit_file, tmp_path):
    rel = "junit.xml"
    src = tmp_path / rel
    src.write_text(JUNIT_XML)

    result = _run_collect(tmp_path, ["--pytest", f"api={rel}", "--allow-failures"])
    assert result.returncode == 0

    summary = json.loads((tmp_path / "summary.json").read_text())
    assert len(summary["failed_tests"]) == 2
    names = [t["name"] for t in summary["failed_tests"]]
    assert "test_info_endpoint" in names
    assert "test_error_case" in names


def test_allow_failures_exit_code(junit_file, tmp_path):
    rel = "junit.xml"
    src = tmp_path / rel
    src.write_text(JUNIT_XML)

    result = _run_collect(tmp_path, ["--pytest", f"api={rel}", "--allow-failures"])
    assert result.returncode == 0


def test_exit_code_on_failure(junit_file, tmp_path):
    rel = "junit.xml"
    src = tmp_path / rel
    src.write_text(JUNIT_XML)

    result = _run_collect(tmp_path, ["--pytest", f"api={rel}"])
    assert result.returncode == 1


def test_router_ip_redacted_default(tmp_path):
    result = _run_collect(tmp_path, [])
    assert result.returncode == 2  # no runners → errored

    if (tmp_path / "run.json").exists():
        run_json = json.loads((tmp_path / "run.json").read_text())
        assert run_json["lab"]["router_ip"] == "<REDACTED>"

    result2 = _run_collect(tmp_path, ["--pytest", "api=nonexistent.xml"])
    assert result2.returncode == 2
