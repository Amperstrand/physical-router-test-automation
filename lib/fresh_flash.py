"""Fresh-flash prerequisite for the install-path scenarios.

The dual-install-path coverage needs a *clean* OpenWrt 25.12.x image: the whole
point is that a bare package install and the installer path both produce the
same policy on a freshly flashed router, with no state carried over.

Flashing is destructive, so this module is deliberately paranoid:

* the image must look like an OpenWrt **25.12.x mediatek/filogic
  glinet_gl-mt3000 sysupgrade** image, and its sha256 must match the upstream
  ``sha256sums``;
* ``sysupgrade -n`` (never keep config) is the only flash mode this harness
  issues;
* the router wallet must be **empty** — ``/etc/tollgate/ecash`` holds real
  ecash, and ``sysupgrade -n`` wipes it.  Draining is the *operator's* step
  (``tollgate wallet drain cashu --yes``); the harness refuses to flash a
  non-empty wallet unless ``--allow-nonempty-wallet`` is passed explicitly.

The module is pure logic + shell builders so it can be unit-tested without a
router; :mod:`scripts.fresh_flash` wires it to SSH under the bench lock.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

#: the image the bench was flashed with (OpenWrt 25.12.5 / mediatek-filogic)
BENCH_IMAGE_FILENAME = "openwrt-25.12.5-mediatek-filogic-glinet_gl-mt3000-squashfs-sysupgrade.bin"
BENCH_IMAGE_SHA256 = "1ffa6526ea099878e0fc520dc0473e95202c1a470b3eabcd2cd40e9c8eaab8c6"
BENCH_IMAGE_URL = (
    "https://downloads.openwrt.org/releases/25.12.5/targets/mediatek/filogic/"
    + BENCH_IMAGE_FILENAME
)
UPSTREAM_SUMS_URL = (
    "https://downloads.openwrt.org/releases/25.12.5/targets/mediatek/filogic/sha256sums"
)

#: a local copy of the image used by the bench phase (if present)
LOCAL_IMAGE_CANDIDATES = (
    os.path.expanduser("~/worktrees/mt3000-flash/" + BENCH_IMAGE_FILENAME),
    "/tmp/" + BENCH_IMAGE_FILENAME,
)

REQUIRED_IMAGE_TOKENS = (
    "openwrt-25.12",  # OpenWrt 25.12.x release
    "mediatek-filogic",  # target
    "gl-mt3000",  # device
    "sysupgrade",
)

ECASH_DIR = "/etc/tollgate/ecash"
TOLLGATE_DIR = "/etc/tollgate"
#: the uci-defaults setup marker: when it is present at the same version, the
#: setup script only verifies the APs and never re-converges the NDS allow list
SETUP_MARKER = "/etc/tollgate-setup-done"

ALLOW_NONEMPTY_FLAG = "--allow-nonempty-wallet"

#: Flashing is destructive and destroys real money, so it is doubly gated:
#: an explicit environment switch AND the wallet gate below.
FLASH_ENABLE_ENV = "TOLLGATE_ENABLE_SYSUPGRADE_FLASHING"


class FlashRefused(RuntimeError):
    """Raised instead of flashing when the prep conditions are not met."""


class ImageInvalid(FlashRefused):
    """The candidate image is not the OpenWrt 25.12.x image this harness expects."""


# ---------------------------------------------------------------------------
# Image validation
# ---------------------------------------------------------------------------


def validate_image_filename(filename: str) -> list[str]:
    """Return the list of problems with an image filename ([] means OK)."""
    base = os.path.basename(filename or "").lower()
    problems = [
        f"image filename {os.path.basename(filename or '')!r} is missing the required token {token!r}"
        for token in REQUIRED_IMAGE_TOKENS
        if token not in base
    ]
    if not base.endswith(".bin"):
        problems.append(f"image filename {os.path.basename(filename or '')!r} does not end in .bin")
    if not re.search(r"openwrt-25\.12(\.|$)", base):
        problems.append(
            f"image filename {os.path.basename(filename or '')!r} is not an OpenWrt 25.12.x image "
            "(the apk-based target this harness validates)"
        )
    return problems


def resolve_image_path(explicit: str | None = None) -> str:
    """First existing candidate: explicit path, then the bench worktree copy."""
    candidates = [c for c in [explicit, *LOCAL_IMAGE_CANDIDATES] if c]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    raise ImageInvalid(
        "no local sysupgrade image found; pass --image <path> or download "
        f"{BENCH_IMAGE_URL} (sha256 {BENCH_IMAGE_SHA256})"
    )


def expected_sha_from_upstream_sums(sums_text: str, filename: str) -> str | None:
    """Look the image hash up in an OpenWrt ``sha256sums`` file."""
    base = os.path.basename(filename)
    for raw in (sums_text or "").splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", raw.strip())
        if match and match.group(2).strip().endswith(base):
            return match.group(1).lower()
    return None


def verify_image(path: str, expected_sha256: str | None = None) -> list[str]:
    """Validate name + sha256 of a candidate image; [] means ready to flash."""
    from lib.install_paths import sha256_file

    problems = validate_image_filename(path)
    if problems:
        return problems
    expected = (expected_sha256 or BENCH_IMAGE_SHA256).lower()
    actual = sha256_file(path)
    if actual != expected:
        problems.append(
            f"image sha256 {actual} != expected {expected} for {os.path.basename(path)} — "
            "refusing to flash an unverified image"
        )
    return problems


# ---------------------------------------------------------------------------
# Wallet gate — real money
# ---------------------------------------------------------------------------

#: how the operator drains (documented, NOT executed by the harness on its own)
DRAIN_COMMAND = "tollgate wallet drain cashu --yes"
WALLET_BALANCE_COMMAND = "tollgate --json wallet balance 2>/dev/null || tollgate wallet balance 2>/dev/null"
ECASH_LISTING_COMMAND = f"find {ECASH_DIR} -type f -size +0c 2>/dev/null; ls -la {ECASH_DIR} 2>/dev/null"


@dataclass
class WalletState:
    """What the router's wallet looks like right before a flash."""

    total_sats: int = 0
    nonempty_files: list[str] = field(default_factory=list)
    raw_balance: str = ""
    raw_listing: str = ""
    probed: bool = True

    @property
    def empty(self) -> bool:
        return self.total_sats == 0 and not self.nonempty_files

    def summary(self) -> str:
        if not self.probed:
            return "wallet state unknown (not probed)"
        return (
            f"total_sats={self.total_sats} nonempty_ecash_files={len(self.nonempty_files)}"
            + (f" [{', '.join(self.nonempty_files[:5])}]" if self.nonempty_files else "")
        )


def parse_wallet_balance(text: str) -> int:
    """Best-effort total sat balance from ``tollgate wallet balance`` output."""
    if not text:
        return 0
    totals = [int(m) for m in re.findall(r'"(?:total|balance|amount)"\s*:\s*(\d+)', text)]
    if totals:
        return max(totals)
    numbers = [int(m) for m in re.findall(r"(\d+)\s*(?:sat|sats)\b", text, re.IGNORECASE)]
    return max(numbers) if numbers else 0


def parse_ecash_listing(text: str) -> list[str]:
    """Non-empty files under ``/etc/tollgate/ecash`` from the listing command.

    Handles both ``find -type f -size +0c`` (bare paths) and ``ls -la`` rows.
    """
    files: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("total "):
            continue
        if line.startswith(f"{ECASH_DIR}/") or line.startswith(f"{TOLLGATE_DIR}/"):
            files.append(line)
            continue
        parts = line.split()
        if len(parts) >= 9 and parts[0].startswith("-") and parts[4] != "0":
            name = parts[-1]
            if name not in (".", ".."):
                files.append(f"{ECASH_DIR}/{name}" if "/" not in name else name)
    return sorted(set(files))


def parse_wallet_state(balance_output: str, listing_output: str) -> WalletState:
    return WalletState(
        total_sats=parse_wallet_balance(balance_output),
        nonempty_files=parse_ecash_listing(listing_output),
        raw_balance=balance_output or "",
        raw_listing=listing_output or "",
    )


def flash_guard(state: WalletState, *, allow_nonempty: bool) -> None:
    """Refuse to flash a router that still holds money (unless told explicitly)."""
    if state.empty:
        return
    if allow_nonempty:
        return
    raise FlashRefused(
        "REFUSING TO FLASH: the router wallet is NOT empty "
        f"({state.summary()}). `sysupgrade -n` wipes {TOLLGATE_DIR} — including real ecash. "
        f"Drain first (operator step): `{DRAIN_COMMAND}` "
        f"(prints the Cashu tokens; exit 2 = cancelled and nothing moved), "
        f"then re-run. Pass {ALLOW_NONEMPTY_FLAG} only if you accept losing the funds."
    )


def flashing_enabled() -> bool:
    """Is the destructive flash switch on?  (``TOLLGATE_ENABLE_SYSUPGRADE_FLASHING=true``)"""
    return os.environ.get(FLASH_ENABLE_ENV, "").strip().lower() in ("1", "true", "yes")


def flash_enable_gate() -> None:
    """Refuse unless the operator switched flashing on explicitly.

    ``sysupgrade -n`` wipes the router; a stray cron, a mistyped target or an
    accidental re-run must not be able to erase the bench on its own.
    """
    if flashing_enabled():
        return
    raise FlashRefused(
        f"REFUSING TO FLASH: {FLASH_ENABLE_ENV} is not set to 'true'. Flashing erases "
        f"{TOLLGATE_DIR} (config, identities and real ecash) and is opt-in only. "
        f"Re-run with {FLASH_ENABLE_ENV}=true once you have drained the wallet "
        f"(`{DRAIN_COMMAND}`)."
    )


def flash_preconditions(state: WalletState, *, allow_nonempty: bool = False) -> list[str]:
    """Everything that must hold before ``sysupgrade`` — empty means go.

    Returns the refusals as strings so callers can report *all* of them at once
    (the pytest gate prints every blocker instead of the first one).
    """
    problems: list[str] = []
    if not flashing_enabled():
        problems.append(
            f"{FLASH_ENABLE_ENV} is not 'true' — the destructive flash switch is off"
        )
    if not state.empty and not allow_nonempty:
        problems.append(
            f"router wallet is NOT empty ({state.summary()}) — `sysupgrade -n` wipes "
            f"{TOLLGATE_DIR}, including real ecash; drain first with `{DRAIN_COMMAND}`"
        )
    return problems


# ---------------------------------------------------------------------------
# Flash command builders
# ---------------------------------------------------------------------------


def remote_image_path(filename: str = BENCH_IMAGE_FILENAME) -> str:
    return f"/tmp/{os.path.basename(filename)}"


def sysupgrade_command(remote_path: str, *, keep_config: bool = False) -> str:
    """``sysupgrade -n <image>`` — ``-n`` deliberately does NOT keep config."""
    flags = "" if keep_config else "-n "
    return f"sysupgrade {flags}{remote_path}"


def post_flash_readdress_command(interface: str, address: str = "192.168.1.200/24") -> str:
    """Re-address the host NIC after the flash: a fresh image defaults to 192.168.1.1."""
    return f"ip addr replace {address} dev {interface} && ip link set {interface} up"


def board_identity_command() -> str:
    return (
        "cat /tmp/sysinfo/board_name 2>/dev/null; "
        ". /etc/openwrt_release 2>/dev/null; echo \"$DISTRIB_DESCRIPTION|$DISTRIB_ARCH\""
    )


def fresh_image_ready_violations(board_output: str, *, expect_arch: str = "aarch64_cortex-a53") -> list[str]:
    """After the flash: the board must be the MT3000 on OpenWrt 25.12.x, arch-correct."""
    problems: list[str] = []
    text = board_output or ""
    if "gl-mt3000" not in text.lower():
        problems.append(f"post-flash board is not glinet,gl-mt3000: {text.strip()[:120]!r}")
    if "25.12" not in text:
        problems.append(f"post-flash OpenWrt release is not 25.12.x: {text.strip()[:120]!r}")
    if expect_arch and expect_arch not in text:
        problems.append(f"post-flash arch is not {expect_arch}: {text.strip()[:120]!r}")
    return problems


def no_tollgate_state_violations(
    installed_version_output: str, tollgate_dir_listing: str, *, setup_marker_present: bool = False
) -> list[str]:
    """A fresh image must NOT already carry tollgate-wrt or /etc/tollgate state."""
    problems: list[str] = []
    if "tollgate-wrt" in (installed_version_output or ""):
        problems.append(
            "tollgate-wrt is already installed right after the flash — this is not a "
            f"fresh image ({installed_version_output.strip()[:120]!r})"
        )
    if setup_marker_present:
        problems.append(
            f"{SETUP_MARKER} is present after the flash — the uci-defaults setup already ran, and "
            "a same-version reinstall would only verify the APs instead of re-converging the NDS "
            "allow list (the 'stale marker' trap; see docs/install-paths-e2e.md)"
        )
    listing = tollgate_dir_listing or ""
    for line in ("config.json", "identities.json", "install.json"):
        if line in listing:
            problems.append(
                f"{TOLLGATE_DIR}/{line} survived the flash — `sysupgrade -n` was not applied"
            )
    return problems


def setup_marker_command() -> str:
    """Print the marker path when it exists (empty output = absent)."""
    return f"[ -f {SETUP_MARKER} ] && echo {SETUP_MARKER} || true"
