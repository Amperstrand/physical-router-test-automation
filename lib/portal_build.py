"""
Portal build — builds either the TollGate or net4sats captive portal SPA from source.

Both are React/Vite apps that talk to the same router backend (:2121).
The only differences are branding (logo, colors, locale strings) and
error-code prefixes (TG vs NS).

Usage:
    from lib.portal_build import PortalBuild
    build = PortalBuild("net4sats")       # or "tollgate"
    path = build.build()                   # → Path to built SPA
    build.deploy(router_host="10.99.99.1") # → SCP to router
"""

import logging
import os
import pathlib
import shutil
import subprocess
from typing import Optional

log = logging.getLogger("tollgate.portal_build")

# Portal registry: repo → branding metadata
PORTALS = {
    "tollgate": {
        "repo": "https://github.com/OpenTollGate/tollgate-captive-portal-site.git",
        "display_name": "TollGate",
        "tagline": "Pay-as-you-go internet access",
        "color_primary": "#f97316",     # orange
        "color_bg": "#0a0e1a",
        "error_prefix": "TG",
    },
    "net4sats": {
        "repo": "https://github.com/net4sats/net4sats-captive-portal-site.git",
        "display_name": "net4sats",
        "tagline": "Prepaid internet — network access for sats",
        "color_primary": "#0891b2",     # cyan
        "color_bg": "#04111f",
        "error_prefix": "NS",
    },
}

# Cache directory for portal repos (avoids re-cloning)
_CACHE_DIR = pathlib.Path(os.environ.get("TOLLGATE_PORTAL_CACHE",
                                          pathlib.Path.home() / ".cache" / "tollgate-portals"))


class PortalBuild:
    """Builds and deploys a branded captive portal SPA."""

    def __init__(self, skin: str = "tollgate"):
        if skin not in PORTALS:
            raise ValueError(f"Unknown portal skin: {skin!r}. "
                             f"Must be one of: {', '.join(PORTALS)}")
        self.skin = skin
        self.meta = PORTALS[skin]
        self.repo_dir = _CACHE_DIR / skin

    @property
    def display_name(self) -> str:
        return self.meta["display_name"]

    @property
    def build_dir(self) -> pathlib.Path:
        return self.repo_dir / "build"

    def _ensure_repo(self):
        """Clone or update the portal repo."""
        if self.repo_dir.exists() and (self.repo_dir / ".git").exists():
            log.debug("updating %s repo", self.skin)
            subprocess.run(["git", "fetch", "-q", "origin"],
                          cwd=self.repo_dir, capture_output=True)
            subprocess.run(["git", "reset", "-q", "--hard", "origin/main"],
                          cwd=self.repo_dir, capture_output=True)
        else:
            log.info("cloning %s portal repo", self.skin)
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "-q", "--depth", "1",
                 self.meta["repo"], str(self.repo_dir)],
                capture_output=True, timeout=120,
            )

    def build(self, clean: bool = True) -> pathlib.Path:
        """Build the SPA. Returns path to the build output directory."""
        self._ensure_repo()

        if clean and self.build_dir.exists():
            shutil.rmtree(self.build_dir)

        log.info("building %s portal SPA", self.skin)
        # Install deps if node_modules missing
        if not (self.repo_dir / "node_modules").exists():
            log.info("  installing npm dependencies")
            subprocess.run(["npm", "install", "--silent"],
                          cwd=self.repo_dir, capture_output=True, timeout=300)
        result = subprocess.run(
            ["npx", "vite", "build"],
            cwd=self.repo_dir,
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            # Stale lockfile can pull incompatible native modules — retry clean
            log.warning("build failed, retrying with clean install")
            import shutil as _shutil
            _shutil.rmtree(self.repo_dir / "node_modules", ignore_errors=True)
            (self.repo_dir / "package-lock.json").unlink(missing_ok=True)
            subprocess.run(["npm", "install", "--silent"],
                          cwd=self.repo_dir, capture_output=True, timeout=300)
            result = subprocess.run(
                ["npx", "vite", "build"],
                cwd=self.repo_dir,
                capture_output=True, text=True, timeout=300,
            )
        if result.returncode != 0:
            raise RuntimeError(f"vite build failed: {result.stderr[-300:]}")

        if not self.build_dir.exists():
            raise RuntimeError(f"build output missing at {self.build_dir}")

        log.info("built %s portal → %s (%d files)",
                 self.skin, self.build_dir,
                 len(list(self.build_dir.rglob("*"))))
        return self.build_dir

    def deploy(self, router_host: str = "10.99.99.1",
               ssh_password: Optional[str] = None,
               portal_path: str = "/etc/tollgate/tollgate-captive-portal-site",
               method: str = "ssh") -> bool:
        """Deploy the built SPA to the router.

        Args:
            router_host: router IP or hostname
            ssh_password: SSH password (for virtual lab)
            portal_path: where to place the SPA on the router
            method: "ssh" (tar over SSH) or "scp"
        """
        if not self.build_dir.exists():
            self.build()

        # Create tarball (excluding macOS metadata)
        tar_path = _CACHE_DIR / f"{self.skin}-portal.tgz"
        subprocess.run(
            ["tar", "czf", str(tar_path),
             "-C", str(self.build_dir),
             "--exclude", "._*", "--exclude", ".DS_Store",
             "."],
            capture_output=True,
        )

        if method == "ssh":
            if ssh_password:
                ssh_cmd = ["sshpass", "-p", ssh_password, "ssh",
                           "-o", "StrictHostKeyChecking=no",
                           f"root@{router_host}"]
            else:
                ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=no",
                           f"root@{router_host}"]

            # Send tarball and extract on router
            with open(tar_path, "rb") as f:
                result = subprocess.run(
                    ssh_cmd + [f"mkdir -p {portal_path} && tar xzf - -C {portal_path} && ls {portal_path}/ | head -3"],
                    stdin=f,
                    capture_output=True, text=True, timeout=60,
                )
            success = result.returncode == 0
        else:
            # SCP method
            if ssh_password:
                scp_cmd = ["sshpass", "-p", ssh_password, "scp", "-O",
                           "-o", "StrictHostKeyChecking=no"]
            else:
                scp_cmd = ["scp", "-o", "StrictHostKeyChecking=no"]

            # Tar → scp → extract
            subprocess.run(scp_cmd + [str(tar_path),
                                      f"root@{router_host}:/tmp/portal.tgz"],
                          capture_output=True, timeout=60)
            result = subprocess.run(
                ssh_cmd + [f"mkdir -p {portal_path} && tar xzf /tmp/portal.tgz -C {portal_path}"],
                capture_output=True, text=True, timeout=30,
            )
            success = result.returncode == 0

        if success:
            log.info("deployed %s portal to %s:%s",
                     self.skin, router_host, portal_path)
        else:
            log.error("deploy failed: %s", result.stderr[-200:] if result.stderr else "unknown")
        return success

    def narration_substitutions(self) -> dict[str, str]:
        """Return TTS-friendly substitutions for narration templates."""
        name = self.display_name.lower()
        if self.skin == "net4sats":
            spoken = "net-for-satts"
        else:
            spoken = "toll-gate"
        return {
            "{brand}": self.display_name,
            "{brand_spoken}": spoken,
            "{tagline}": self.meta["tagline"],
        }
