import os
import shutil
import socket as _socket
import subprocess
import tempfile as _tempfile
import time

import json as _json

import pytest


@pytest.fixture(autouse=True)
def _reset_nds_and_trigger(router):
    provider = os.environ.get("TOLLGATE_VM_PROVIDER", "")
    if provider != "local" and not os.environ.get("TOLLGATE_VIRTUAL_LAB"):
        yield
        return

    client_ip = os.environ.get("TOLLGATE_CLIENT_IP", "10.99.99.100")
    client_mac = os.environ.get("TOLLGATE_CLIENT_MAC", "")

    router_arp = ""
    try:
        router_arp = router.ssh(f"cat /proc/net/arp 2>/dev/null | grep '{client_ip} ' | awk '{{print $4}}' || true", timeout=5).strip()
    except Exception:
        pass
    if router_arp:
        client_mac = router_arp

    if client_mac:
        try:
            router.ssh(f"ndsctl deauth {client_mac} 2>/dev/null || true", timeout=5)
            time.sleep(2)
        except Exception:
            pass

    try:
        subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
             f"root@{client_ip}",
             "curl -s -o /dev/null --max-time 5 http://10.99.99.1:2050/ && "
             "curl -s -o /dev/null --max-time 5 http://connectivitycheck.gstatic.com/generate_204"],
            capture_output=True, timeout=15,
        )
        time.sleep(2)
    except Exception:
        pass

    yield

    if client_mac:
        try:
            router.ssh(f"ndsctl deauth {client_mac} 2>/dev/null || true", timeout=5)
        except Exception:
            pass


DEFAULT_RUST_BASIC_BINARY = "/home/ubuntu/src/tollgate-module-basic-rust/target/release/tollgate-module-basic-rust"
DEFAULT_RUST_BASIC_HTTP_PORT = 2121


@pytest.fixture(scope="module")
def rust_basic_server():
    if os.environ.get("TOLLGATE_BACKEND") not in ("rust-basic", "rust-embedded"):
        pytest.skip("rust_basic_server requires TOLLGATE_BACKEND=rust-basic or rust-embedded")

    binary_path = os.environ.get("TOLLGATE_BINARY_PATH", DEFAULT_RUST_BASIC_BINARY)
    if not os.path.exists(binary_path):
        pytest.skip(
            f"Binary not found at {binary_path}. Build it first: "
            f"cd /home/ubuntu/src/tollgate-module-basic-rust && cargo build --release"
        )

    http_port = int(os.environ.get("TOLLGATE_HTTP_PORT", DEFAULT_RUST_BASIC_HTTP_PORT))

    try:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", http_port))
    except OSError as exc:
        pytest.fail(
            f"Port {http_port} already in use — another tollgate binary running? "
            f"Run `ss -tlnp | grep {http_port}` and kill the squatter. ({exc})"
        )

    owns_config_dir = False
    config_dir = os.environ.get("TOLLGATE_TEST_CONFIG_DIR")
    if not config_dir:
        config_dir = _tempfile.mkdtemp(prefix="tollgate-rust-basic-")
        owns_config_dir = True
    os.makedirs(config_dir, exist_ok=True)

    config = {
        "config_version": "v0.0.7",
        "log_level": "info",
        "metric": "milliseconds",
        "step_size": 5000,
        "margin": 0.1,
        "accepted_mints": [
            {
                "url": "https://testnut.cashu.exchange",
                "min_balance": 0,
                "balance_tolerance_percent": 0,
                "payout_interval_seconds": 60,
                "min_payout_amount": 0,
                "price_per_step": 1,
                "price_unit": "sat",
                "purchase_min_steps": 1,  # 0 is not representable post-parse: tbmg #104 normalizes 0/absent -> 1
            }
        ],
        "profit_share": [{"factor": 1.0, "identity": "owner"}],
    }
    with open(os.path.join(config_dir, "config.json"), "w") as f:
        _json.dump(config, f)

    env = {**os.environ, "TOLLGATE_TEST_CONFIG_DIR": config_dir}
    proc = subprocess.Popen(
        [binary_path],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    socket_path = os.path.join(config_dir, "tollgate.sock")

    try:
        for _ in range(50):
            if proc.poll() is not None:
                output = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
                pytest.fail(
                    f"Binary exited early with code {proc.returncode}.\nOutput:\n{output}"
                )
            try:
                with _socket.create_connection(("127.0.0.1", http_port), timeout=0.1):
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)
        else:
            output = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
            pytest.fail(
                f"Binary did not bind 127.0.0.1:{http_port} within 5s.\nOutput:\n{output}"
            )

        for _ in range(30):
            if os.path.exists(socket_path):
                break
            time.sleep(0.1)

        yield {
            "proc": proc,
            "config_dir": config_dir,
            "binary_path": binary_path,
            "http_url": f"http://127.0.0.1:{http_port}",
            "socket_path": socket_path,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if os.path.exists(socket_path):
            try:
                os.unlink(socket_path)
            except OSError:
                pass
        if owns_config_dir:
            shutil.rmtree(config_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _sweep_leaked_mint_blocks(router):
    """Per-TEST guard against mint-block REJECT leaks.

    pytest-timeout can kill a degraded test's teardown entirely; the
    runner only sweeps at file boundaries, so a leak poisoned later
    tests IN THE SAME file (token_formats/config_invariants/concurrent
    class, 2026-10-06). Flag-gated: costs one local check when no
    block was installed, one ssh sweep when one might have leaked.
    """
    import lib.helpers as _h
    if _h._mint_blocks_installed:
        try:
            _h.sweep_mint_blocks(router)
        except Exception:
            pass
        _h._mint_blocks_installed = False
    yield
    if _h._mint_blocks_installed:
        try:
            _h.sweep_mint_blocks(router)
        except Exception:
            pass
        _h._mint_blocks_installed = False
