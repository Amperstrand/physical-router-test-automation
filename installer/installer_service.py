"""tollgate-installer service lifecycle: clean build, run, deploy API client."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

DEFAULT_SRC = Path.home() / "src" / "tollgate-installer"
DEFAULT_PORT = 8199


class InstallerServiceError(RuntimeError):
    pass


class InstallerService:
    """Builds and runs the wizard binary; talks to its REST API."""

    def __init__(self) -> None:
        self._tmp: tempfile.TemporaryDirectory | None = None
        self.bin_path: Path | None = None
        self.port: int | None = None
        self.proc: subprocess.Popen | None = None
        self.log_path: Path | None = None

    def build(self) -> None:
        env_bin = os.environ.get("TOLLGATE_INSTALLER_BIN")
        if env_bin and Path(env_bin).exists():
            self.bin_path = Path(env_bin)
            return
        src = Path(os.environ.get("TOLLGATE_INSTALLER_SRC", str(DEFAULT_SRC)))
        if not src.exists():
            raise InstallerServiceError(f"installer source not found: {src}")
        ref = os.environ.get("TOLLGATE_INSTALLER_REF", "main")
        remote = subprocess.run(["git", "-C", str(src), "remote", "get-url", "origin"],
                                capture_output=True, text=True)
        upstream = remote.stdout.strip() if remote.returncode == 0 else ""
        self._tmp = tempfile.TemporaryDirectory(prefix="prta-installer-")
        clone = Path(self._tmp.name) / "src"
        if upstream:
            subprocess.run(["git", "clone", "-q", "--branch", ref, upstream, str(clone)],
                           check=True, timeout=180)
        else:
            subprocess.run(["git", "clone", "-q", str(src), str(clone)], check=True, timeout=120)
        # A clone contains committed state only: uncommitted WIP in the
        # source checkout is deliberately never the test target. The
        # default ref is upstream main — a stale local checkout must not
        # decide what gets tested.
        r = subprocess.run(["go", "build", "-o", "tollgate-installer", "."], cwd=clone,
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise InstallerServiceError(f"go build failed: {r.stderr[:400]}")
        self.bin_path = clone / "tollgate-installer"

    def start(self) -> None:
        if self.bin_path is None:
            raise InstallerServiceError("call build() first")
        self.port = self._pick_port()
        self.log_path = Path(tempfile.gettempdir()) / f"prta-installer-{self.port}.log"
        logf = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            [str(self.bin_path), "-port", str(self.port)],
            stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, start_new_session=True,
        )
        self._wait_ui(30)

    def _pick_port(self) -> int:
        port = DEFAULT_PORT
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
                port += 1
            except Exception:
                return port

    def _wait_ui(self, timeout: int) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(self.url("/"), timeout=2)
                return
            except Exception:
                time.sleep(0.5)
        tail = self.log_path.read_text()[-400:] if self.log_path else ""
        raise InstallerServiceError(f"installer UI not up on :{self.port}; log tail: {tail}")

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get_json(self, path: str, timeout: int = 120) -> dict:
        with urllib.request.urlopen(self.url(path), timeout=timeout) as r:
            return json.load(r)

    def post_json(self, path: str, payload: dict, timeout: int = 60) -> dict:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.url(path), data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def deploy(self, ip: str, password: str, lnurl: str, mode: str = "wan",
               timeout: int = 480) -> dict:
        """POST /api/deploy and poll /api/status/<job_id> to a terminal state."""
        resp = self.post_json("/api/deploy",
                              {"ip": ip, "password": password, "mode": mode, "lnurl": lnurl})
        jid = resp.get("job_id") or resp.get("id")
        if not jid:
            raise InstallerServiceError(f"deploy response missing job_id: {resp}")
        deadline = time.time() + timeout
        status: dict = {}
        while time.time() < deadline:
            status = self.get_json(f"/api/status/{jid}", timeout=30)
            if str(status.get("status", "")).lower() in ("done", "failed", "error"):
                return status
            time.sleep(5)
        raise InstallerServiceError(
            f"deploy job {jid} not terminal within {timeout}s: {json.dumps(status)[:300]}")

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        if self._tmp:
            self._tmp.cleanup()
            self._tmp = None


def go_available() -> bool:
    return shutil.which("go") is not None
