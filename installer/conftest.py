"""Fixtures for tollgate-installer E2E tests (self-provisioned local QEMU lab).

The suite lives outside ``tests/`` (like ``conwrt/``) so the shared-router
fixtures and the session-autouse ``deploy_session`` in ``tests/conftest.py``
never apply. Run explicitly: ``pytest installer/ -v``.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from installer_service import InstallerService  # noqa: E402
from lab_vm import BASE_IMAGE, InstallerLab, load_base_password  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def _require(cond: bool, msg: str) -> None:
    if not cond:
        pytest.skip(msg)


@pytest.fixture(scope="session")
def installer_lab():
    _require(sys.platform.startswith("linux"), "installer tests require Linux (KVM/QEMU)")
    _require(Path("/dev/kvm").exists(), "/dev/kvm missing — installer tests need KVM")
    _require(BASE_IMAGE.exists(), f"baked base image missing: {BASE_IMAGE}")
    for tool in ("qemu-system-x86_64", "qemu-img", "sshpass"):
        _require(shutil.which(tool), f"{tool} not installed")

    has_bin_override = bool(os.environ.get("TOLLGATE_INSTALLER_BIN"))
    _require(has_bin_override or shutil.which("go"), "neither TOLLGATE_INSTALLER_BIN nor go available")
    if not has_bin_override:
        src = Path(os.environ.get("TOLLGATE_INSTALLER_SRC",
                                  str(Path.home() / "src" / "tollgate-installer")))
        _require(src.exists(), f"installer source not found: {src}")

    creds = REPO_ROOT / "credentials" / "virtual-lab-credentials.json"
    _require(creds.exists(), f"{creds} missing — needed for the baked-base root password")

    lab = InstallerLab(load_base_password(REPO_ROOT))
    lab.cleanup_prior()
    lab.ensure_network()
    lab.boot_fresh_vm()

    # The installer (post #41) refuses SSH to untrusted hosts before auth.
    # The lab base has no host keys: each fresh VM generates new ones at
    # first boot, and the Go client negotiates dropbear's RSA key — so the
    # pin must be computed per boot, from this exact VM.
    import subprocess
    ks = subprocess.run(["ssh-keyscan", "-t", "rsa", "10.99.95.1"],
                        capture_output=True, text=True, timeout=30).stdout
    fp = subprocess.run(["ssh-keygen", "-lf", "-"], input=ks,
                        capture_output=True, text=True).stdout.split()[1]
    os.environ["TOLLGATE_TRUST_HOST_KEY"] = fp
    yield lab
    os.environ.pop("TOLLGATE_TRUST_HOST_KEY", None)
    lab.stop_vm()
    lab.teardown_network()


@pytest.fixture(scope="session")
def installer_service(installer_lab):
    svc = InstallerService()
    svc.build()
    svc.start()
    yield svc
    svc.stop()
