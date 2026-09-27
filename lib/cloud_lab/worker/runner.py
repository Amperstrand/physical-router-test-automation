"""Cloud lab worker — declarative pytest runner."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from lib.cloud_lab.constants import TEST_DIR
from lib.cloud_lab.worker.config import WorkerConfig
from lib.cloud_lab.worker.shell import _run, log

VL_SCENARIO_PATHS = (
    "tests/scenarios/test_captive_portal_browser.py",
    "tests/scenarios/test_mint_health.py",
    "tests/scenarios/test_boot_hygiene.py",
    "tests/scenarios/test_upstream_wifi.py",
    "tests/scenarios/test_upgrade.py",
)


@dataclass(frozen=True)
class RunnerSpec:
    name: str
    paths: tuple[str, ...]
    timeout: int = 300
    markers: str | None = None
    ignore_paths: tuple[str, ...] = ()
    pytest_node: str | None = None
    enabled: Callable[[WorkerConfig], bool] = field(default=lambda _c: True)

    def junit_rel(self) -> str:
        return f"raw/{self.name}/junit.xml"

    def raw_dir(self, results_dir: str) -> str:
        return f"{results_dir}/raw/{self.name}"


def _always(_config: WorkerConfig) -> bool:
    return True


def _hwsim_enabled(config: WorkerConfig) -> bool:
    return config.hwsim_enabled


def _reseller_enabled(config: WorkerConfig) -> bool:
    return config.reseller_scenarios


def _two_router_enabled(config: WorkerConfig) -> bool:
    return config.two_router


def _virtual_wifi_enabled(config: WorkerConfig) -> bool:
    return config.wifi_plane == "hwsim-netns"


def build_runners(config: WorkerConfig) -> list[RunnerSpec]:
    """Return ordered pytest runners for the given worker mode."""
    if config.quick:
        return [
            RunnerSpec(
                name="visual",
                paths=("tests/api/test_visual_happy_path.py",),
                timeout=300,
                pytest_node="tests/api/test_visual_happy_path.py::test_visual_happy_path",
            ),
        ]

    if config.smoke:
        runners = [
            RunnerSpec(
                name="visual",
                paths=("tests/api/test_visual_happy_path.py",),
                timeout=300,
            ),
            RunnerSpec(
                name="smoke-api",
                paths=("tests/api/",),
                timeout=120,
                markers="smoke",
                ignore_paths=("tests/api/test_visual_happy_path.py",),
            ),
            RunnerSpec(
                name="hwsim",
                paths=("tests/api/test_mac80211_hwsim.py",),
                timeout=120,
                enabled=_hwsim_enabled,
            ),
            RunnerSpec(
                name="virtual-wifi",
                paths=("tests/api/test_virtual_wifi_hwsim_netns.py",),
                timeout=180,
                enabled=_virtual_wifi_enabled,
            ),
        ]
        return [r for r in runners if r.enabled(config)]

    runners = [
        RunnerSpec(
            name="visual",
            paths=("tests/api/test_visual_happy_path.py",),
            timeout=300,
            markers="not complete" if not config.complete else None,
        ),
        RunnerSpec(
            name="api",
            paths=("tests/api/",),
            timeout=300,
            markers="not complete" if not config.complete else None,
            ignore_paths=("tests/api/test_visual_happy_path.py",),
        ),
        RunnerSpec(name="vl-scenarios", paths=VL_SCENARIO_PATHS, timeout=600),
        RunnerSpec(
            name="nut18",
            paths=("tests/api/test_nut18_payment.py",),
            timeout=120,
        ),
        RunnerSpec(
            name="scenarios",
            paths=("tests/scenarios/test_reseller_mode.py",),
            timeout=300,
            enabled=_reseller_enabled,
        ),
        RunnerSpec(
            name="two-router",
            paths=("tests/scenarios/test_two_router_cloud.py",),
            timeout=300,
            enabled=_two_router_enabled,
        ),
        RunnerSpec(
            name="virtual-wifi",
            paths=("tests/api/test_virtual_wifi_hwsim_netns.py",),
            timeout=180,
            enabled=_virtual_wifi_enabled,
        ),
    ]
    return [r for r in runners if r.enabled(config)]


def runner_scope(config: WorkerConfig) -> str:
    if config.quick:
        return "quick"
    if config.smoke:
        return "smoke"
    if config.complete:
        return "complete"
    return "full"


def pytest_collect_args(config: WorkerConfig) -> str:
    """Build --pytest name=path args for collect-results.py."""
    parts = [f"--pytest {spec.name}={spec.junit_rel()}" for spec in build_runners(config)]
    return " ".join(parts) + (" " if parts else "")


def _expected_pr_arg(config: WorkerConfig) -> str:
    return f"--expected-pr={config.sut_pr} " if config.sut_pr else ""


def _build_pytest_cmd(config: WorkerConfig, spec: RunnerSpec, results_dir: str) -> str:
    raw = spec.raw_dir(results_dir)
    expected_pr = _expected_pr_arg(config)
    target = spec.pytest_node or " ".join(spec.paths)
    marker = f"-m '{spec.markers}' " if spec.markers else ""
    ignore = " ".join(f"--ignore={p}" for p in spec.ignore_paths)
    ignore_sp = f"{ignore} " if ignore else ""
    pytest_cmd = (
        # -u: unbuffered stdout/stderr so `tail -f output.log` during a cloud
        # run is truly live (Python block-buffers when stdout is redirected to
        # a file, which would make real-time monitoring lag by many seconds).
        f"python3 -u -m pytest {target} "
        f"-v --tb=short --timeout={spec.timeout} {marker}"
        f"--backend={config.backend} "
        f"{expected_pr}--client=container --results {results_dir} "
        f"{ignore_sp}"
        f"--junitxml={raw}/junit.xml "
        f"--html={raw}/report.html --self-contained-html "
        f">{raw}/output.log 2>&1"
    )
    return (
        f"cd {TEST_DIR} && source /opt/tollgate-venv/bin/activate && set -a && source .env && set +a && "
        f"{pytest_cmd}"
    )


def _aggregate_exit(codes: dict[str, int]) -> int:
    worst = 0
    for code in codes.values():
        if code != 0 and code > worst:
            worst = code
    return worst


def run_tests(config: WorkerConfig, results_dir: str) -> int:
    """Execute all enabled runners and return worst exit code."""
    runners = build_runners(config)
    if not runners:
        log.warning("No test runners enabled for config")
        return 0

    raw_dirs = " ".join(f"{results_dir}/raw/{spec.name}" for spec in runners)
    prep = (
        f"cd {TEST_DIR} && source /opt/tollgate-venv/bin/activate && set -a && source .env && set +a && "
        f"mkdir -p {raw_dirs} {results_dir}/report"
    )
    r = _run(prep, timeout=30, check=False)
    if r.returncode != 0:
        log.error("Runner prep failed (rc=%d): %s", r.returncode, r.stderr[:200] if r.stderr else "")
        return 1

    mode = runner_scope(config)
    log.info("%s MODE: running %d runner(s): %s", mode.upper(), len(runners), ", ".join(r.name for r in runners))

    exit_codes: dict[str, int] = {}
    # Full mode measured ~100 min for the api runner alone on snapshot v19/v20
    # (49% at the old 3000s cap): the cap killed pytest mid-suite, so junit.xml
    # was never written and the published evidence truncated. 7200s matches the
    # worker's MAX_WALL; submit with --lease >= 150 for full runs.
    total_timeout = 600 if config.quick else (1200 if config.smoke else 7200)

    # vl-scenarios is destructive (blocks mints via /etc/hosts, restarts services).
    # Must run AFTER parallel api to avoid poisoning concurrent sessions.
    destructive_names = {"vl-scenarios"}

    visual_runners = [r for r in runners if r.name == "visual"]
    parallel_runners = [r for r in runners if r.name != "visual" and r.name not in destructive_names]
    sequential_runners = [r for r in runners if r.name in destructive_names]

    # Phase 1: visual gate
    for spec in visual_runners:
        cmd = _build_pytest_cmd(config, spec, results_dir)
        log.info("Runner [%s] starting (sequential gate, timeout=%ds)", spec.name, spec.timeout)
        r = _run(cmd, timeout=total_timeout, check=False)
        exit_codes[spec.name] = r.returncode
        if r.returncode != 0:
            log.warning("Runner [%s] exit=%d (gate failed, continuing)", spec.name, r.returncode)
        else:
            log.info("Runner [%s] passed (gate)", spec.name)

    # Phase 2: parallel non-destructive runners
    if parallel_runners:
        log.info("Starting %d parallel runner(s): %s", len(parallel_runners), ", ".join(r.name for r in parallel_runners))

        def _execute_runner(spec: RunnerSpec) -> tuple[str, int]:
            cmd = _build_pytest_cmd(config, spec, results_dir)
            log.info("Runner [%s] starting (parallel, timeout=%ds)", spec.name, spec.timeout)
            r = _run(cmd, timeout=total_timeout, check=False)
            return spec.name, r.returncode

        with ThreadPoolExecutor(max_workers=len(parallel_runners)) as pool:
            futures = {pool.submit(_execute_runner, spec): spec.name for spec in parallel_runners}
            pending = set(futures)
            try:
                for future in as_completed(futures, timeout=total_timeout):
                    pending.discard(future)
                    try:
                        name, code = future.result(timeout=30)
                    except Exception as exc:
                        name = futures[future]
                        log.error("Runner [%s] crashed: %s", name, exc)
                        code = 1
                    exit_codes[name] = code
                    if code != 0:
                        log.warning("Runner [%s] exit=%d", name, code)
                    else:
                        log.info("Runner [%s] passed", name)
            except TimeoutError:
                # as_completed timed out: futures still in `pending` were lost by
                # the old code (their results discarded silently). Mark them failed
                # and let pool shutdown join the processes _run already killed.
                for future in pending:
                    name = futures[future]
                    log.error("Runner [%s] exceeded the %ds pool budget — marked failed", name, total_timeout)
                    exit_codes[name] = 1

    # Phase 3: destructive runners (sequential, after parallel phase completes)
    if sequential_runners:
        log.info("Starting %d sequential destructive runner(s): %s", len(sequential_runners), ", ".join(r.name for r in sequential_runners))
        for spec in sequential_runners:
            cmd = _build_pytest_cmd(config, spec, results_dir)
            log.info("Runner [%s] starting (sequential destructive, timeout=%ds)", spec.name, spec.timeout)
            r = _run(cmd, timeout=total_timeout, check=False)
            exit_codes[spec.name] = r.returncode
            if r.returncode != 0:
                log.warning("Runner [%s] exit=%d", spec.name, r.returncode)
            else:
                log.info("Runner [%s] passed", spec.name)

    worst = _aggregate_exit(exit_codes)
    log.info(
        "All runners finished: worst_exit=%d (%s)",
        worst,
        ", ".join(f"{k}={v}" for k, v in exit_codes.items()),
    )
    return worst


def skip_summary_from_junit(junit_path: str) -> dict[str, Any]:
    """Parse skip reasons from a junit.xml file (best-effort)."""
    import xml.etree.ElementTree as ET
    from pathlib import Path

    path = Path(junit_path)
    if not path.exists():
        return {"total_skipped": 0, "reasons": {}}

    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return {"total_skipped": 0, "reasons": {}}

    reasons: dict[str, int] = {}
    skipped = 0
    for case in root.iter("testcase"):
        skip_el = case.find("skipped")
        if skip_el is None:
            continue
        skipped += 1
        msg = (skip_el.get("message") or skip_el.text or "unknown").strip()[:120]
        reasons[msg] = reasons.get(msg, 0) + 1
    return {"total_skipped": skipped, "reasons": reasons}
