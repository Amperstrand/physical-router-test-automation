"""E2E coverage for the tollgate-bash-client against a live TollGate router.

These tests exercise the NEW capabilities added in BC1
(github.com/sh1ftred/tollgate-bash-client, branch feature/openwrt-service-mode):

  * cross-platform OS detection + byte counters (lib/common.sh)
  * TollGate auto-discovery (lib/discovery.sh) — must recognise THIS router
  * structured JSON logging (lib/logging.sh)

They run the bash client's libs *on the test runner* (a macOS/Linux box with
bash+jq+curl), pointing discovery at the live router fixture. That validates the
exact code path a real client uses to find and pay a TollGate.

Requires a physical router: marked ``physical_only`` and skipped in the
virtual/cloud lab. The bash client checkout is resolved from, in order:

  1. ``$TOLLGATE_BASH_CLIENT_DIR``  — explicit path to a checkout
  2. ``$PWD/../tollgate-bash-client`` — sibling checkout (common in the lab)
  3. a shallow clone of the feature branch into ``tests/.bash-client``
"""

import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.story("G8"),pytest.mark.api, pytest.mark.physical_only]

BASH_CLIENT_REPO = "https://github.com/sh1ftred/tollgate-bash-client"
BASH_CLIENT_BRANCH = "feature/openwrt-service-mode"


def _skip_if_no_tools():
    for tool in ("bash", "jq", "curl"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH — cannot run bash-client libs")


def _resolve_client_dir():
    env_dir = os.environ.get("TOLLGATE_BASH_CLIENT_DIR", "").strip()
    if env_dir and os.path.isfile(os.path.join(env_dir, "auto-pay.sh")):
        return env_dir
    sibling = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "tollgate-bash-client")
    )
    if os.path.isfile(os.path.join(sibling, "auto-pay.sh")):
        return sibling
    # Shallow clone once per session into the tests dir.
    dest = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", ".bash-client")
    )
    if not os.path.isfile(os.path.join(dest, "auto-pay.sh")):
        subprocess.run(
            [
                "git", "clone", "--depth", "1",
                "-b", BASH_CLIENT_BRANCH, BASH_CLIENT_REPO, dest,
            ],
            check=True,
            timeout=120,
        )
    return dest


@pytest.fixture(scope="module")
def bash_client():
    """Return the path to a tollgate-bash-client checkout."""
    _skip_if_no_tools()
    return _resolve_client_dir()


def _run_bash(client_dir, script, extra_env=None):
    """Run a bash snippet that sources the client libs; return CompletedProcess."""
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    # Run from the client dir so relative `./lib/...` sources resolve.
    return subprocess.run(
        ["bash", "-c", script],
        cwd=client_dir,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )


def test_detect_os_is_supported(bash_client):
    """detect_os() must report a known platform, not 'unknown'."""
    res = _run_bash(bash_client, ". ./lib/common.sh; detect_os")
    assert res.returncode == 0, res.stderr
    os_name = res.stdout.strip()
    assert os_name in ("macos", "linux"), f"unexpected os: {os_name!r}"


def test_byte_counters_are_numeric(bash_client):
    """get_local_bytes() must return two non-negative integers for the default iface."""
    res = _run_bash(
        bash_client,
        ". ./lib/common.sh; read -r rx tx <<< \"$(get_local_bytes)\"; echo \"$rx $tx\"",
    )
    assert res.returncode == 0, res.stderr
    rx, tx = res.stdout.split()
    assert rx.isdigit() and tx.isdigit(), f"non-numeric bytes: {rx!r} {tx!r}"
    assert int(rx) >= 0 and int(tx) >= 0


def test_discovery_recognises_live_router(bash_client, router):
    """is_tollgate(<this router>) must be true — the router answers on :2121."""
    res = _run_bash(
        bash_client,
        f". ./lib/common.sh; . ./lib/discovery.sh; "
        f"if is_tollgate {router.host}; then echo TOLLGATE; else echo NO; fi",
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "TOLLGATE", (
        f"discovery failed to recognise router {router.host} as a TollGate\n"
        f"stdout={res.stdout!r} stderr={res.stderr!r}"
    )


def test_discovery_override_short_circuits(bash_client, router):
    """discover_tollgate() honours TOLLGATE_HOST without scanning the subnet."""
    res = _run_bash(
        bash_client,
        ". ./lib/common.sh; . ./lib/discovery.sh; discover_tollgate",
        extra_env={"TOLLGATE_HOST": router.host},
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == router.host


def test_structured_logging_emits_valid_json(bash_client, tmp_path):
    """log_event must write one JSON object per line with the required keys."""
    log_file = tmp_path / "tollgate.log"
    res = _run_bash(
        bash_client,
        f". ./lib/logging.sh; "
        f"log_info smoke 'a message' iface=test; "
        f"log_error pay 'payment failed' host=x; "
        f"echo '---LOG---'; cat '{log_file}'",
        extra_env={"TOLLGATE_LOG": str(log_file)},
    )
    assert res.returncode == 0, res.stderr
    body = res.stdout.split("---LOG---", 1)[1].strip()
    lines = [ln for ln in body.splitlines() if ln.strip()]
    assert len(lines) == 2, f"expected 2 log lines, got {len(lines)}: {body!r}"

    import json

    for ln in lines:
        ev = json.loads(ln)  # raises if not valid JSON
        for key in ("ts", "level", "event", "msg", "host", "pid"):
            assert key in ev, f"missing key {key!r} in {ln!r}"
    # The error event must carry the extra host= field.
    assert lines[1].rstrip().endswith('"host":"x"') or '"host":"x"' in lines[1]
