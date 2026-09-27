"""Cudy WR3000 **v1** two-stage OpenWrt flash lane — pure, offline-testable logic.

The operator's requirement: *"the physical router testing kit should have the
ability to flash OpenWrt and TollGate on these Cudy routers."*  This module is
the decision half of that lane; :mod:`scripts.cudy_flash` wires it to HTTP/SSH
and does the destructive parts.

Why two stages (OpenWrt TOH, "OEM easy installation", for Cudy WR3000 v1):

* the **stock** CudyOS web UI refuses a mainline OpenWrt image — Cudy signs its
  own images and ships no signature-disable build, so an unsigned sysupgrade is
  rejected;
* it *does* accept **Cudy's own signed OpenWrt** transitional image, and once
  that is running a real OpenWrt, the ordinary mainline ``sysupgrade`` path is
  available.  No case opening, no UART.

    stage 1  vendor UI upload of the vendored Cudy-signed transitional image
             (``openwrt-mediatek-filogic-cudy_wr3000-v1-sysupgrade.bin``) —
             192.168.10.1 -> reboot -> 192.168.1.1, Wi-Fi interfaces DISABLED
    stage 2  mainline ``sysupgrade -n`` of OpenWrt 25.12.x for the same
             target/board (staged over ssh stdin — these builds have no
             sftp-server, so ``scp``/``sftp`` are not a transport here)
    stage 3  TollGate install — NOT reimplemented here: the kit already owns it
             (see :func:`tollgate_install_handoff`)

Hard guards kept from the rest of the kit (import, never re-invent):

* :mod:`lib.fresh_flash` owns the destructive-switch gate
  (``TOLLGATE_ENABLE_SYSUPGRADE_FLASHING``), ``FlashRefused``/``ImageInvalid``,
  the wallet gate (a probe that did not answer is *unknown*, never "empty"), and
  the sysupgrade/board-identity command builders;
* :mod:`lib.install_paths` owns the TollGate artifact + install-path machinery.

HONESTY / EVIDENCE-FIRST

Stage 1's upload endpoint and form shape are **pinned from a real hardware run on
2026-09-27** (Cudy WR3000 v1, this bench): the Firmware *modal* on
``/cgi-bin/luci/admin/panel`` loads the JS; attaching the image to the file input
``cbid.upgrade.1.firmware`` (``accept=.bin``) makes the page's own onchange POST it to
``/cgi-bin/luci/admin/system/upgrade`` (HTTP 200), a ``Proceed`` button then appears and
clicking it POSTs again (HTTP 302, redirect to ``…/reboot?upgrade=`` →
``/reboot/apply?upgrade=true``).  A bare GET of ``…/system/upgrade`` returns a ~2.6 KB
fragment in which ``window.upload_file`` is undefined — the modal is what loads it.

Stage 2 (mainline ``sysupgrade`` over an ssh stdin redirect) was also exercised end to end
on that run.  ``docs/cudy-wr3000-flashing.md`` records exactly which parts of this lane are
hardware-verified, which are unit-tested only, and which remain unverified (notably the
*volatile / RAM* install, which is implemented but has NOT been run on hardware).

The flash-capacity wall found on that run (a 16 MB device cannot hold the 21 MB
uncompressed TollGate payload in its 4.6 MB free overlay) is modelled here as a hard
preflight (``check_install_capacity``) plus a documented volatile (tmpfs) install
(``volatile_install_plan``), rather than being allowed to die mid-extract with ENOSPC.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from lib import fresh_flash as ff

# ---------------------------------------------------------------------------
# Device / target facts (all verified — see docs/cudy-wr3000-flashing.md)
# ---------------------------------------------------------------------------

TARGET = "mediatek/filogic"
#: target as it appears in the OpenWrt image filename (hyphen, not slash)
TARGET_TOKEN = "mediatek-filogic"
#: OpenWrt board token (the kernel/board_name spelling uses a comma)
BOARD = "cudy_wr3000-v1"
BOARD_NAME_TOKEN = "cudy,wr3000-v1"
#: ``supported_devices`` for the transitional build
SUPPORTED_DEVICES = (BOARD_NAME_TOKEN, "R31")
ARCH = "aarch64_cortex-a53"

#: the mainline release the lane targets (same target/board as the transitional)
MAINLINE_RELEASE = "25.12.5"
UPSTREAM_SUMS_URL = (
    f"https://downloads.openwrt.org/releases/{MAINLINE_RELEASE}/targets/{TARGET}/sha256sums"
)

# ---------------------------------------------------------------------------
# Stage 1 — the vendored, Cudy-signed transitional image
# ---------------------------------------------------------------------------

TRANSITIONAL_IMAGE_FILENAME = "openwrt-mediatek-filogic-cudy_wr3000-v1-sysupgrade.bin"
TRANSITIONAL_IMAGE_SHA256 = "8ee579d1b970488ee06f47964ac27ef88bc627b563cea00b88f1cb3a2917ec64"
TRANSITIONAL_IMAGE_SIZE = 9964331
#: build identity, pinned from the image itself (``openwrt_release`` / board.json)
TRANSITIONAL_BUILD = "OpenWrt SNAPSHOT r22906-c9cb6411c1"
TRANSITIONAL_TARGET_BOARD = f"{TARGET} / board {BOARD}"
#: the date the two flash stages were exercised END TO END on real hardware
HARDWARE_VERIFIED_DATE = "2026-09-27"
#: VERIFIED 2026-09-27: stage 1 leaves root reachable by ssh with an EMPTY password
TRANSITIONAL_ROOT_PASSWORD_NOTE = (
    "after stage 1 the box answers on 192.168.1.1 and `ssh root@192.168.1.1` succeeds with an "
    "EMPTY password (these builds ship no sftp-server, and the Wi-Fi interfaces are DISABLED). "
    "Set the lab root password before handing over to the install path."
)

#: provenance of the vendored blob (do NOT re-host it; fetch it from Cudy)
TRANSITIONAL_SOURCE_PAGE = "https://www.cudy.com/blogs/faq/openwrt-software-download"
TRANSITIONAL_SOURCE_DRIVE_FOLDER = "1BKVarlwlNxf7uJUtRhuMGUqeCa5KpMnj"
TRANSITIONAL_SOURCE_DRIVE_FILE_ID = "1AWpLk9bElfBverPHdpXOJrP30WcFf9OC"
#: the exact (double-space) Drive download name; the inner zip holds ONE .bin
TRANSITIONAL_SOURCE_ZIP = "WR3000+V1  without recovery TFTP.zip"
TRANSITIONAL_INNER_ZIP = "WR3000+V1  without recovery TFTP/openwrt-mediatek-filogic-cudy_wr3000-v1-sysupgrade.zip"
#: the "recovery TFTP" variant of the same download is a DIFFERENT archive: it is
#: for the serial/U-Boot route and is not the image this lane uploads.
TRANSITIONAL_VARIANT_NOTE = (
    "the Cudy Drive folder offers a 'WR3000+V1  without recovery TFTP.zip' AND a "
    "recovery-TFTP variant. This lane needs the WITHOUT-recovery-TFTP one — that is "
    "the image whose signature CudyOS accepts from its own web UI."
)

#: a local copy the operator already extracted (sha256-checked before use)
LOCAL_TRANSITIONAL_CANDIDATES = (
    os.path.expanduser("~/cudy-flash/cudy-openwrt/img/" + TRANSITIONAL_IMAGE_FILENAME),
    "/tmp/" + TRANSITIONAL_IMAGE_FILENAME,
)

# ---------------------------------------------------------------------------
# Stage 2 — the mainline image, DERIVED from target/board (never hardcoded)
# ---------------------------------------------------------------------------


def mainline_image_filename(
    release: str = MAINLINE_RELEASE, target: str = TARGET_TOKEN, board: str = BOARD
) -> str:
    """``openwrt-25.12.5-mediatek-filogic-cudy_wr3000-v1-squashfs-sysupgrade.bin``.

    Derived from the target/board token rather than hardcoded, so the lane cannot
    silently flash (or *claim* to flash) the MT3000 filename ``lib/fresh_flash.py``
    pins for the other bench box.
    """
    return f"openwrt-{release}-{target}-{board}-squashfs-sysupgrade.bin"


#: the operator's verified mainline image (matches downloads.openwrt.org
#: ``sha256sums`` for 25.12.5/mediatek/filogic — verified 2026-09-27)
MAINLINE_IMAGE_FILENAME = mainline_image_filename()
MAINLINE_IMAGE_SHA256 = "be876cf5335ab757874cd19f806b01f2271d8c20a1a0e68f1680019346c4408a"
MAINLINE_IMAGE_SIZE = 9699606
MAINLINE_IMAGE_URL = (
    f"https://downloads.openwrt.org/releases/{MAINLINE_RELEASE}/targets/{TARGET}/"
    + MAINLINE_IMAGE_FILENAME
)

LOCAL_MAINLINE_CANDIDATES = (
    os.path.expanduser("~/cudy-flash/" + MAINLINE_IMAGE_FILENAME),
    os.path.expanduser("~/worktrees/mt3000-flash/" + MAINLINE_IMAGE_FILENAME),
    "/tmp/" + MAINLINE_IMAGE_FILENAME,
)

# ---------------------------------------------------------------------------
# Addresses, doors, credentials
# ---------------------------------------------------------------------------

#: factory CudyOS LAN address and the only doors it opens (53/80/443; no SSH)
OEM_LAN_ADDRESS = "192.168.10.1"
#: after the transitional flash the box answers on OpenWrt's default LAN address
OPENWRT_LAN_ADDRESS = "192.168.1.1"
#: host-side /24 to re-address onto the fresh box (reuse ff.post_flash_readdress_command)
POST_FLASH_READDRESS = "192.168.1.200/24"

#: TOH's documented door: panel (Advanced Settings) -> the Firmware *modal*.
#: VERIFIED 2026-09-27: the modal on this page is what loads the upgrade JS.
OEM_PANEL_PATH = "/cgi-bin/luci/admin/panel"
#: the door the kit's own bench-prep suite found the Firmware section behind
OEM_FIRMWARE_LINK_SELECTOR = 'a[href*="active=autoupgrade"]'
#: pages the lane fetches (the modal lives on the panel).  VERIFIED 2026-09-27.
OEM_FIRMWARE_PATHS = (OEM_PANEL_PATH,)

# --- stage-1 upload shape, PINNED FROM HARDWARE 2026-09-27 ------------------------
#: the multipart POST target.  NB: a bare GET of this path returns a ~2.6 KB fragment in
#: which ``window.upload_file`` is undefined — it is the POST endpoint, not a page.
OEM_UPLOAD_ENDPOINT = "/cgi-bin/luci/admin/system/upgrade"
#: the file input's ``name``, as served inside the Firmware modal (``accept=".bin"``)
OEM_UPLOAD_FILE_FIELD = "cbid.upgrade.1.firmware"
OEM_UPLOAD_FILE_ACCEPT = ".bin"
#: the label of the button that appears AFTER the upload POST (clicking it POSTs again)
OEM_UPLOAD_PROCEED_LABEL = "Proceed"
#: the two reboot applies the 302 from ``Proceed`` drives, in order (VERIFIED)
OEM_REBOOT_UPGRADE_PATHS = (
    "/cgi-bin/luci/admin/system/reboot?upgrade=",
    "/reboot/apply?upgrade=true",
)
#: what a live run must reproduce for stage 1 (upload 200 -> Proceed 302 -> reboot apply)
OEM_UPLOAD_FLOW = (
    "attach the image to the modal's file input {field} (accept={accept}) -> the page's own "
    "onchange POSTs it to {endpoint} (HTTP 200) -> a `{proceed}` button appears -> clicking it "
    "POSTs again (HTTP 302) and drives {reboot}"
).format(
    field=OEM_UPLOAD_FILE_FIELD,
    accept=OEM_UPLOAD_FILE_ACCEPT,
    endpoint=OEM_UPLOAD_ENDPOINT,
    proceed=OEM_UPLOAD_PROCEED_LABEL,
    reboot=" -> ".join(OEM_REBOOT_UPGRADE_PATHS),
)
OEM_UPLOAD_FRAGMENT_NOTE = (
    f"a bare GET of {OEM_UPLOAD_ENDPOINT} returns a ~2.6 KB fragment where "
    "`window.upload_file` is undefined; the Firmware modal opened from "
    f"{OEM_PANEL_PATH} is what loads the upgrade JS.  Do not treat the fragment as the page."
)
#: login form shape on CudyOS (hidden ``luci_username=admin`` + one password field)
OEM_LOGIN_PATH = "/cgi-bin/luci/"
OEM_LOGIN_USERNAME = "admin"

URL_ENV = "CUDY_URL"
#: the vendor password is supplied ONLY via env; there is deliberately no literal
#: default here.  The repo's lab convention lives in ``tests/browser/admin_spa.spec.mjs``
#: (see :data:`PASSWORD_CONVENTION_REF`) — read it there, never copy it into code.
PASSWORD_ENVS = ("CUDY_PASSWORD", "TOLLGATE_LUCI_PASSWORD")
PASSWORD_CONVENTION_REF = "tests/browser/admin_spa.spec.mjs"
#: the laptop feed for the vendored blob (Drive file id), for the docs/CLI help
TRANSITIONAL_FETCH_URL = (
    f"https://drive.google.com/uc?export=download&id={TRANSITIONAL_SOURCE_DRIVE_FILE_ID}"
)


def oem_url(path: str = "", *, host: str | None = None) -> str:
    """Build a URL against the vendor UI (env ``CUDY_URL`` wins over the factory IP)."""
    base = (host or os.environ.get(URL_ENV) or f"http://{OEM_LAN_ADDRESS}").rstrip("/")
    if not path:
        return base
    return base + "/" + path.lstrip("/")


def vendor_password() -> str:
    """The vendor password from env only (``CUDY_PASSWORD``, then ``TOLLGATE_LUCI_PASSWORD``).

    Returns ``""`` when neither is set — callers must refuse, naming
    :data:`PASSWORD_CONVENTION_REF`, rather than fall back to a literal.
    """
    for name in PASSWORD_ENVS:
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


# ---------------------------------------------------------------------------
# Model / hardware-revision guard (WR3000 v1 ONLY)
# ---------------------------------------------------------------------------

#: labels that positively identify a supported **v1.0** box
MODEL_V1_MARKERS = ("WR3000 V1.0", "WR3000 V1", "R31")
#: labels that positively identify the UNSUPPORTED v2.0 box (different SoC)
MODEL_V2_MARKERS = ("WR3000 V2.0", "WR3000 2.0", "WR3000V2.0", "WR3000V2", "WR3000 2", "V2.0")

MODEL_V2_REASON = (
    "this is a Cudy WR3000 **2.0** (a different, NOT OpenWrt-supported SoC). The "
    "OpenWrt Table of Hardware entry for the WR3000 says the 2.0 CPU is not "
    "supported, and a Cudy-signed transitional image for the v1 board will not "
    "boot there. This lane REFUSES to flash a 2.0 box — there is no lane for it."
)


@dataclass(frozen=True)
class ModelVerdict:
    """What the box's label/model string says the hardware is."""

    raw: str = ""
    variant: str = "unknown"  # "1.0" | "2.0" | "unknown"
    evidence: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return self.variant == "1.0"

    def describe(self) -> str:
        return (
            f"variant={self.variant} supported={self.supported} "
            f"evidence={list(self.evidence) or 'none'}"
        )


class ModelRefused(ff.FlashRefused):
    """The box is not a WR3000 v1.0 (or cannot be positively identified as one)."""


def classify_model(label_text: str) -> ModelVerdict:
    """Classify a label/model string as WR3000 v1.0, v2.0, or unknown.

    FAIL CLOSED: only a positive v1 marker makes a box flashable. A 2.0 marker
    refuses loudly; anything else is ``unknown`` and refuses too (the operator can
    override an unknown verdict explicitly, never a 2.0 one).
    """
    text = re.sub(r"\s+", " ", (label_text or "")).strip().upper()
    if not text:
        return ModelVerdict(raw=label_text or "", variant="unknown")
    v2 = tuple(marker for marker in MODEL_V2_MARKERS if marker.upper() in text)
    if v2:
        return ModelVerdict(raw=label_text, variant="2.0", evidence=v2)
    v1 = tuple(marker for marker in MODEL_V1_MARKERS if marker.upper() in text)
    if v1:
        return ModelVerdict(raw=label_text, variant="1.0", evidence=v1)
    return ModelVerdict(raw=label_text, variant="unknown")


def require_model_supported(verdict: ModelVerdict, *, allow_unknown: bool = False) -> ModelVerdict:
    """Raise :class:`ModelRefused` unless the box is a positively-identified v1.0.

    ``allow_unknown=True`` (CLI ``--assume-wr3000-v1``) is the ONLY way past an
    unidentifiable label, and it can never override a 2.0 verdict.
    """
    if verdict.supported:
        return verdict
    if verdict.variant == "2.0":
        raise ModelRefused(
            f"REFUSING TO FLASH: {MODEL_V2_REASON} [label seen: {verdict.raw.strip()[:120]!r}; "
            f"matched {list(verdict.evidence)}]"
        )
    if allow_unknown:
        return verdict
    raise ModelRefused(
        "REFUSING TO FLASH: the box could not be positively identified as a Cudy WR3000 "
        f"**v1.0**. Expected one of {list(MODEL_V1_MARKERS)} on the label (or via "
        "--model); a v1/v2 mix-up bricks the unit. Re-run with --assume-wr3000-v1 only "
        f"when you have physically read the label. [label seen: {verdict.raw.strip()[:120]!r}]"
    )


# ---------------------------------------------------------------------------
# Image identity: filename + size + sha256 (+ optional <image>.sha256 sidecar)
# ---------------------------------------------------------------------------


def validate_transitional_filename(filename: str) -> list[str]:
    """Problems with a stage-1 filename (``[]`` = OK)."""
    base = os.path.basename(filename or "")
    problems: list[str] = []
    if base != TRANSITIONAL_IMAGE_FILENAME:
        problems.append(
            f"stage-1 image filename {base!r} is not {TRANSITIONAL_IMAGE_FILENAME!r} — the "
            "Cudy-signed transitional build is a single, exactly-named file "
            f"({TRANSITIONAL_VARIANT_NOTE})"
        )
    return problems


def validate_mainline_filename(
    filename: str, *, release: str = MAINLINE_RELEASE, board: str = BOARD
) -> list[str]:
    """Problems with a stage-2 filename (``[]`` = OK).

    Both the release and the board token are required, and the board token is the
    Cudy one — so the MT3000 image ``lib/fresh_flash.py`` pins can never be staged
    here by accident.
    """
    base = os.path.basename(filename or "").lower()
    problems: list[str] = []
    for token in (f"openwrt-{release}", TARGET_TOKEN, board.lower(), "squashfs-sysupgrade"):
        if token not in base:
            problems.append(f"stage-2 image filename {os.path.basename(filename or '')!r} is missing {token!r}")
    if not base.endswith(".bin"):
        problems.append(f"stage-2 image filename {os.path.basename(filename or '')!r} does not end in .bin")
    if "gl-mt3000" in base:
        problems.append(
            f"stage-2 image filename {os.path.basename(filename or '')!r} is the MT3000 bench image — "
            "this lane flashes the Cudy; use the board-derived filename "
            f"{mainline_image_filename(release, TARGET_TOKEN, board)!r}"
        )
    return problems


def read_sha256_sidecar(path: str) -> str | None:
    """Return the sha256 recorded in ``<path>.sha256``, or ``None`` when absent.

    Accepts both the bare-hex and ``sha256sum``-style (``<hex>  <name>``) forms —
    either the image's own sidecar or an OpenWrt ``sha256sums``-style single row.
    """
    sidecar = str(path) + ".sha256"
    if not os.path.isfile(sidecar):
        return None
    try:
        with open(sidecar, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:  # pragma: no cover - defensive
        return None
    match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    return match.group(1).lower() if match else None


def verify_cudy_image(
    path: str,
    *,
    expected_sha256: str,
    expected_size: int | None = None,
    filename_validator=None,
    use_sidecar: bool = True,
) -> list[str]:
    """Validate name + size + sha256 of a Cudy image; ``[]`` means ready to use.

    A ``<path>.sha256`` sidecar is honoured when present.  When BOTH the sidecar
    and a caller-supplied hash exist and disagree, that is a refusal (never a
    silent pick).
    """
    from lib.install_paths import sha256_file

    if filename_validator is not None:
        problems = list(filename_validator(path))
        if problems:
            return problems
    problems = []

    if not os.path.isfile(path):
        return [f"image {path!r} does not exist"]

    expected = expected_sha256.lower()
    if use_sidecar:
        sidecar = read_sha256_sidecar(path)
        if sidecar and sidecar != expected:
            return [
                f"{os.path.basename(path)}.sha256 says {sidecar} but the lane expects {expected} — "
                "refusing to flash an image whose own recorded hash disagrees with the pinned one"
            ]

    if expected_size is not None:
        actual_size = os.path.getsize(path)
        if actual_size != expected_size:
            problems.append(
                f"image size {actual_size} != expected {expected_size} for {os.path.basename(path)} — "
                "refusing to flash a truncated or substituted image"
            )

    actual = sha256_file(path)
    if actual != expected:
        problems.append(
            f"image sha256 {actual} != expected {expected} for {os.path.basename(path)} — "
            "refusing to flash an unverified image"
        )
    return problems


def verify_transitional_image(path: str, *, expected_sha256: str | None = None) -> list[str]:
    """Stage-1 identity gate (name + exact size + pinned sha256 + sidecar if present)."""
    return verify_cudy_image(
        path,
        expected_sha256=expected_sha256 or TRANSITIONAL_IMAGE_SHA256,
        expected_size=TRANSITIONAL_IMAGE_SIZE,
        filename_validator=validate_transitional_filename,
    )


def verify_mainline_image(path: str, *, expected_sha256: str | None = None) -> list[str]:
    """Stage-2 identity gate (board-derived name + exact size + pinned sha256)."""
    return verify_cudy_image(
        path,
        expected_sha256=expected_sha256 or MAINLINE_IMAGE_SHA256,
        expected_size=MAINLINE_IMAGE_SIZE,
        filename_validator=validate_mainline_filename,
    )


def resolve_transitional_image(explicit: str | None = None) -> str:
    """First existing candidate: explicit ``--image``, then the extracted local copy."""
    return _resolve(explicit, LOCAL_TRANSITIONAL_CANDIDATES, TRANSITIONAL_IMAGE_FILENAME, TRANSITIONAL_FETCH_URL)


def resolve_mainline_image(explicit: str | None = None) -> str:
    """First existing candidate: explicit ``--image``, then the local copies."""
    return _resolve(explicit, LOCAL_MAINLINE_CANDIDATES, MAINLINE_IMAGE_FILENAME, MAINLINE_IMAGE_URL)


def _resolve(explicit: str | None, local: tuple[str, ...], filename: str, url: str) -> str:
    candidates = [c for c in [explicit, *local] if c]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    raise ff.ImageInvalid(
        f"no local {filename} found; pass --image <path> or download {url}"
    )


def expected_sha_from_upstream_sums(sums_text: str, filename: str) -> str | None:
    """Look a filename up in an OpenWrt ``sha256sums`` file (reused from fresh_flash)."""
    return ff.expected_sha_from_upstream_sums(sums_text, filename)


# ---------------------------------------------------------------------------
# Page classification — evidence-first, fail closed
# ---------------------------------------------------------------------------

#: markers that mean "this is U-Boot / recovery / failsafe", never a flash door
BOOTLOADER_MARKERS = (
    "u-boot",
    "uboot",
    "tftpboot",
    "bootm",
    "failsafe",
    "system recovery",
    "recovery mode",
    "tftp",
)

#: markers unique to the CudyOS vendor skin (so CudyOS is not mistaken for an
#: OpenWrt-with-LuCI box, which it superficially resembles)
VENDOR_UI_MARKERS = (
    "cudyos",
    "quick setup",
    "diagnostic tools",
    "general settings",
    "advanced settings",
    "luci_username=admin",
    "admin/panel",
    "admin/wizard",
)

#: markers that identify a RUNNING mainline OpenWrt (the stage-1 result).  A bare
#: "luci" is deliberately NOT enough — CudyOS is LuCI-derived.
OPENWRT_MARKERS = (
    "openwrt 25",
    "openwrt 24",
    "openwrt 23",
    "openwrt snap",
    "/etc/openwrt_release",
    "attitude adjustment",
    "powered by openwrt",
    "openwrt_release",
)

PAGE_KINDS = ("vendor-ui", "bootloader", "openwrt", "unknown")


@dataclass(frozen=True)
class PageVerdict:
    """What a fetched page is (and why we think so)."""

    kind: str = "unknown"  # one of PAGE_KINDS
    evidence: tuple[str, ...] = ()
    url: str = ""

    @property
    def is_vendor_ui(self) -> bool:
        return self.kind == "vendor-ui"

    @property
    def is_bootloader(self) -> bool:
        return self.kind == "bootloader"

    @property
    def is_running_openwrt(self) -> bool:
        return self.kind == "openwrt"

    @property
    def upload_permitted(self) -> bool:
        """Only a positively-classified vendor UI may be uploaded to."""
        return self.kind == "vendor-ui"

    def describe(self) -> str:
        return f"{self.kind} (evidence={list(self.evidence) or 'none'})"


class PageUnclassified(ff.FlashRefused):
    """The page could not be positively classified — the upload must not proceed."""


def classify_page(html: str, *, url: str = "") -> PageVerdict:
    """Classify a fetched page: vendor UI / bootloader / running OpenWrt / unknown.

    Priority is deliberate: a bootloader page that is not also the vendor skin
    wins first (uploading a sysupgrade to U-Boot's web recovery is a different,
    riskier lane), then the CudyOS-only markers, then mainline-OpenWrt markers.
    Anything else is ``unknown`` and refuses.
    """
    text = (html or "").lower()
    boot = tuple(m for m in BOOTLOADER_MARKERS if m in text)
    vendor = tuple(m for m in VENDOR_UI_MARKERS if m in text)
    openwrt = tuple(m for m in OPENWRT_MARKERS if m in text)
    if boot and not vendor:
        return PageVerdict("bootloader", boot, url)
    if vendor:
        return PageVerdict("vendor-ui", vendor, url)
    if openwrt:
        return PageVerdict("openwrt", openwrt, url)
    return PageVerdict("unknown", (), url)


def require_uploadable_page(verdict: PageVerdict, *, allow_unknown: bool = False) -> PageVerdict:
    """Refuse the stage-1 upload unless the page is positively the vendor UI."""
    if verdict.upload_permitted:
        return verdict
    if verdict.is_bootloader:
        raise PageUnclassified(
            f"REFUSING THE OEM UPLOAD: {verdict.url or 'the page'} looks like a BOOTLOADER / "
            f"recovery page (evidence={list(verdict.evidence)}), not the CudyOS vendor UI. "
            "Flashing U-Boot's web recovery is a different lane and needs its own evidence. "
            "Nothing was uploaded."
        )
    if verdict.is_running_openwrt:
        raise PageUnclassified(
            f"REFUSING THE OEM UPLOAD: {verdict.url or 'the page'} is already running OpenWrt "
            f"(evidence={list(verdict.evidence)}). Stage 1 is already done — run `sysupgrade` "
            "(stage 2) instead. Nothing was uploaded."
        )
    if allow_unknown:
        return verdict
    raise PageUnclassified(
        "REFUSING THE OEM UPLOAD: the page could not be positively classified as the CudyOS "
        "vendor UI, so the lane will not guess an endpoint or a file input. Run "
        "`oem-upload --dump-page` first and pin the endpoint/selectors from the real HTML, "
        "then re-run. Nothing was uploaded."
    )


# ---------------------------------------------------------------------------
# Parsing the vendor UI's upload response, and its forms/selectors
# ---------------------------------------------------------------------------

#: a page's ``<form>`` with a file input is the ONLY thing this lane will upload
#: through. ``--dump-page`` records these verbatim so the endpoint and field name
#: get pinned from real evidence on the first live run instead of guessed.
FORM_RE = re.compile(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>", re.IGNORECASE | re.DOTALL)
ATTR_RE = re.compile(r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
FILE_INPUT_RE = re.compile(r"<input\b[^>]*type\s*=\s*[\"']?file[\"']?[^>]*>", re.IGNORECASE)
SUBMIT_RE = re.compile(r"<(?:input|button)\b[^>]*type\s*=\s*[\"']?submit[\"']?[^>]*>", re.IGNORECASE)


@dataclass(frozen=True)
class UploadForm:
    """A discovered vendor-UI form (possibly the firmware-upload one)."""

    action: str = ""
    method: str = "post"
    enctype: str = ""
    file_field: str = ""
    file_inputs: int = 0
    submit_fields: tuple[str, ...] = ()
    raw_file_input: str = ""

    @property
    def looks_like_upload(self) -> bool:
        """True when the form can carry a file (a file input exists)."""
        return self.file_inputs > 0

    def describe(self) -> str:
        return (
            f"action={self.action or '(same page)'} method={self.method} "
            f"enctype={self.enctype or '(default)'} file_field={self.file_field or '?'} "
            f"file_inputs={self.file_inputs} submits={list(self.submit_fields)}"
        )


def _attrs(fragment: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in ATTR_RE.finditer(fragment or ""):
        key = match.group(1).lower()
        found[key] = match.group(2) if match.group(2) is not None else match.group(3) or ""
    return found


def upload_file_field(html: str) -> str:
    """The ``name=`` of the file input on a page (``""`` when there is none)."""
    match = FILE_INPUT_RE.search(html or "")
    if not match:
        return ""
    return _attrs(match.group(0)).get("name", "")


def parse_upload_forms(html: str) -> list[UploadForm]:
    """Every ``<form>`` on a page, with its file inputs and submit fields.

    Pure text parsing so ``--dump-page`` can record exactly what the box serves,
    and so the uploader refuses when no form with a file input is found (rather
    than POSTing to a guessed endpoint).
    """
    forms: list[UploadForm] = []
    for match in FORM_RE.finditer(html or ""):
        attrs = _attrs(match.group("attrs"))
        body = match.group("body") or ""
        file_inputs = FILE_INPUT_RE.findall(body)
        first_file = file_inputs[0] if file_inputs else ""
        submits: list[str] = []
        for submit in [*SUBMIT_RE.findall(body)]:
            submit_attrs = _attrs(submit)
            submits.append(submit_attrs.get("name") or submit_attrs.get("value") or submit_attrs.get("id") or "<unnamed>")
        forms.append(
            UploadForm(
                action=attrs.get("action", ""),
                method=(attrs.get("method") or "post").lower(),
                enctype=(attrs.get("enctype") or "").lower(),
                file_field=_attrs(first_file).get("name", "") if first_file else "",
                file_inputs=len(file_inputs),
                submit_fields=tuple(submits),
                raw_file_input=first_file,
            )
        )
    return forms


def find_upload_form(html: str) -> UploadForm | None:
    """The first form that can carry a file upload (``None`` when there is none)."""
    for form in parse_upload_forms(html):
        if form.looks_like_upload:
            return form
    return None


def evidence_summary(html: str, *, url: str = "") -> str:
    """A human-readable --dump-page summary: classification + forms + selectors.

    Records the raw facts a live run must pin: the page kind and why, every form
    with an action/method/enctype, the file input's ``name``, and the submit
    selectors.  No upload is attempted from this function.
    """
    verdict = classify_page(html, url=url)
    forms = parse_upload_forms(html)
    upload_form = find_upload_form(html)
    lines = [
        f"url            : {url or '(unknown)'}",
        f"classification : {verdict.describe()}",
        f"file-field     : {upload_file_field(html) or '(none found)'}",
        f"upload-form    : {upload_form.describe() if upload_form else '(none found)'}",
        f"forms          : {len(forms)}",
    ]
    for index, form in enumerate(forms):
        lines.append(f"  form[{index}]     : {form.describe()}")
    return "\n".join(lines)


#: wordings that mean the vendor firmware page ACCEPTED the image
UPLOAD_ACCEPTED_MARKERS = (
    "upgrading",
    "upgrade in progress",
    "firmware update in progress",
    "do not power off",
    "do not turn off",
    "rebooting",
    "restarting",
    "please wait",
    "sysupgrade",
)

#: wordings that mean it REFUSED it (Cudy signature protection is the expected one)
UPLOAD_REFUSED_MARKERS = (
    "invalid",
    "not a valid",
    "signature",
    "verify failed",
    "verification failed",
    "failed",
    "error",
    "unsupported",
    "incorrect",
    "wrong",
    "too large",
    "check the image",
    "refused",
)

UPLOAD_OUTCOMES = ("accepted", "refused", "unknown")


@dataclass(frozen=True)
class UploadOutcome:
    """What the vendor UI answered after an upload attempt."""

    kind: str = "unknown"  # one of UPLOAD_OUTCOMES
    evidence: tuple[str, ...] = ()
    message: str = ""

    @property
    def accepted(self) -> bool:
        return self.kind == "accepted"

    def describe(self) -> str:
        return f"{self.kind} (evidence={list(self.evidence) or 'none'}): {self.message.strip()[:160]}"


def classify_upload_response(text: str) -> UploadOutcome:
    """Parse the vendor UI's answer to an upload.

    A refusal wins over an acceptance: a page that says both ("invalid ... try
    again") is a refusal.  Neither marker present is ``unknown`` — which must
    never be reported as a successful flash.
    """
    body = (text or "").lower()
    accepted = tuple(m for m in UPLOAD_ACCEPTED_MARKERS if m in body)
    refused = tuple(m for m in UPLOAD_REFUSED_MARKERS if m in body)
    if refused:
        return UploadOutcome("refused", refused, text or "")
    if accepted:
        return UploadOutcome("accepted", accepted, text or "")
    return UploadOutcome("unknown", (), text or "")


def upload_response_violations(outcome: UploadOutcome) -> list[str]:
    """Report a stage-1 upload that did NOT positively succeed."""
    if outcome.accepted:
        return []
    if outcome.kind == "refused":
        return [
            "the vendor UI REFUSED the transitional image "
            f"({outcome.describe()}). The expected signature-refusal means this was a "
            "*stock* OpenWrt image rather than Cudy's signed transitional build — check "
            f"the file is exactly {TRANSITIONAL_IMAGE_FILENAME} with sha256 "
            f"{TRANSITIONAL_IMAGE_SHA256}"
        ]
    return [
        "the vendor UI's answer could not be classified (unknown) — this is NOT a pass. "
        f"{outcome.describe()}; capture the page and pin the wording before claiming a flash"
    ]


# --- the two-step stage-1 exchange (PINNED FROM HARDWARE 2026-09-27) --------------
#
# The hardware run showed the modal does NOT do a single POST: the file input's onchange
# POSTs the image (HTTP 200), a `Proceed` button then appears, and clicking it POSTs again
# (HTTP 302) which drives the reboot applies.  The lane reproduces that sequence and treats
# it as the pass — the body wording alone is not enough.

STAGE1_SEQUENCE_KINDS = ("accepted", "refused", "unknown")


@dataclass(frozen=True)
class Stage1Outcome:
    """What the real two-step stage-1 exchange answered."""

    file_status: int = 0
    proceed_status: int = 0
    reboot_status: int | None = None
    kind: str = "unknown"  # one of STAGE1_SEQUENCE_KINDS
    message: str = ""

    @property
    def accepted(self) -> bool:
        return self.kind == "accepted"

    def describe(self) -> str:
        return (
            f"{self.kind}: upload HTTP {self.file_status} -> Proceed HTTP {self.proceed_status}"
            + (f" -> reboot HTTP {self.reboot_status}" if self.reboot_status is not None else "")
            + (f": {self.message.strip()[:160]}" if self.message else "")
        )


def classify_stage1_sequence(
    file_status: int,
    proceed_status: int,
    *,
    reboot_status: int | None = None,
    message: str = "",
) -> Stage1Outcome:
    """Classify the verified two-step stage-1 exchange.

    Accepted == the file POST answered HTTP 200 AND the ``Proceed`` POST answered a 3xx
    redirect.  A ``>=400`` on either is a refusal; anything else is ``unknown``, which must
    never be reported as a pass.
    """
    if file_status >= 400 or proceed_status >= 400:
        return Stage1Outcome(file_status, proceed_status, reboot_status, "refused", message)
    if file_status == 200 and 300 <= proceed_status < 400:
        return Stage1Outcome(file_status, proceed_status, reboot_status, "accepted", message)
    return Stage1Outcome(file_status, proceed_status, reboot_status, "unknown", message)


def stage1_sequence_violations(outcome: Stage1Outcome) -> list[str]:
    """Report a stage-1 exchange that did not positively complete."""
    if outcome.accepted:
        return []
    if outcome.kind == "refused":
        return [
            f"the vendor UI REFUSED the transitional image ({outcome.describe()}). If this is "
            "Cudy's signature refusal, the file is not Cudy's signed transitional build — "
            f"check it is exactly {TRANSITIONAL_IMAGE_FILENAME} with sha256 {TRANSITIONAL_IMAGE_SHA256}"
        ]
    return [
        "the vendor UI's two-step answer was NOT the verified shape "
        f"({OEM_UPLOAD_FLOW}) — this is NOT a pass: {outcome.describe()}. Capture the modal with "
        "`--dump-page` and re-check before claiming a flash"
    ]


# ---------------------------------------------------------------------------
# Stage-2 transport (NO sftp-server on these builds) + flash commands
# ---------------------------------------------------------------------------

#: These OpenWrt builds ship no ``sftp-server``, so ``scp``/``sftp`` fail.  The
#: working transport is an ssh stdin redirect; the kit's own notes say the same
#: ("Staging the image (no sftp-server on these builds)").
SFTP_UNSUPPORTED_NOTE = (
    "OpenWrt on these targets has no sftp-server: `scp` and `sftp` FAIL. Stage the "
    "image with an ssh stdin redirect (see stage_image_command) and re-verify the "
    "sha256 ON THE DEVICE before sysupgrade."
)


def stage_image_command(
    host: str,
    remote_path: str,
    local_path: str,
    *,
    user: str = "root",
    password_env: str = "TOLLGATE_SSH_PASSWORD",
) -> str:
    """The no-sftp staging transport: ``ssh <host> 'cat > <remote>' < <local>``.

    Returns a shell string (run under ``sshpass -e`` so no secret reaches argv).
    Contains no ``scp``/``sftp`` by construction.
    """
    remote = ff.remote_image_path(remote_path)
    return (
        f"sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-o LogLevel=ERROR {user}@{host} 'cat > {remote}' < {local_path}"
    )


def sha256_on_device_command(remote_path: str) -> str:
    """Command that prints the ON-DEVICE sha256 of the staged image."""
    remote = ff.remote_image_path(remote_path)
    return f"sha256sum {remote} | cut -d' ' -f1"


def remote_image_path(filename: str) -> str:
    """``/tmp/<basename>`` — reused from :mod:`lib.fresh_flash` (same convention)."""
    return ff.remote_image_path(filename)


def sysupgrade_command(remote_path: str, *, keep_config: bool = False) -> str:
    """``sysupgrade -n <image>`` — reused from :mod:`lib.fresh_flash` (wipes config)."""
    return ff.sysupgrade_command(remote_path, keep_config=keep_config)


def readdress_command(interface: str, address: str = POST_FLASH_READDRESS) -> str:
    """Re-address the host NIC after the transitional flash (default 192.168.1.x)."""
    return ff.post_flash_readdress_command(interface, address)


def board_identity_command() -> str:
    """``board_name`` + ``openwrt_release`` — reused from :mod:`lib.fresh_flash`."""
    return ff.board_identity_command()


# ---------------------------------------------------------------------------
# Post-flash identity checks
# ---------------------------------------------------------------------------


def post_transitional_identity_violations(board_output: str) -> list[str]:
    """After stage 1: the box must be running ON THE CUDY BOARD but still SNAPSHOT.

    The transitional build is a snapshot (``r22906-c9cb6411c1``); a 25.12.x
    release string here means it is not the transitional image, and the board
    token must be the Cudy one (never the MT3000).
    """
    text = (board_output or "").strip()
    lower = text.lower()
    problems: list[str] = []
    if BOARD_NAME_TOKEN not in lower and BOARD not in lower:
        problems.append(f"post-stage-1 board is not {BOARD_NAME_TOKEN}: {text[:120]!r}")
    if "snapshot" not in lower:
        problems.append(
            f"post-stage-1 image is not the Cudy SNAPSHOT transitional build ({TRANSITIONAL_BUILD}): {text[:120]!r}"
        )
    if ARCH not in text:
        problems.append(f"post-stage-1 arch is not {ARCH}: {text[:120]!r}")
    return problems


def post_mainline_identity_violations(
    board_output: str, *, expect_release: str = MAINLINE_RELEASE
) -> list[str]:
    """After stage 2: the box must be the Cudy board on the mainline release."""
    text = (board_output or "").strip()
    lower = text.lower()
    problems: list[str] = []
    if BOARD_NAME_TOKEN not in lower and BOARD not in lower:
        problems.append(f"post-flash board is not {BOARD_NAME_TOKEN}: {text[:120]!r}")
    if expect_release not in text:
        problems.append(f"post-flash OpenWrt release is not {expect_release}: {text[:120]!r}")
    if "snapshot" in lower:
        problems.append(
            f"post-flash image is still the SNAPSHOT transitional build — stage 2 did not land: {text[:120]!r}"
        )
    if ARCH not in text:
        problems.append(f"post-flash arch is not {ARCH}: {text[:120]!r}")
    return problems


def no_tollgate_state_violations(
    installed_version_output: str, tollgate_dir_listing: str, *, setup_marker_present: bool = False
) -> list[str]:
    """A freshly-flashed mainline image must carry no TollGate state.

    Thin pass-through to :func:`lib.fresh_flash.no_tollgate_state_violations`
    (same board-agnostic rules: no ``tollgate-wrt`` package, no
    ``/etc/tollgate/{config,identities,install}.json``, no stale setup marker).
    """
    return ff.no_tollgate_state_violations(
        installed_version_output, tollgate_dir_listing, setup_marker_present=setup_marker_present
    )


# ---------------------------------------------------------------------------
# The Wi-Fi trap: a fresh OpenWrt has ZERO wireless interfaces until enabled
# ---------------------------------------------------------------------------

#: stock OpenWrt ships BOTH AP ``wifi-iface`` sections ``option disabled 1``; the
#: radios are up but present no interfaces, so every scan strategy fails and a
#: portal test on such a box is not a TollGate result.
WIFI_IFACE_ENABLE_COMMANDS = (
    "uci set wireless.default_radio0.disabled=0",
    "uci set wireless.default_radio1.disabled=0",
    "uci commit wireless",
    "wifi up",
)
#: verification: count of ``"ifname"`` in ``ubus call network.wireless status``
WIFI_VERIFY_COMMAND = "ubus call network.wireless status"
WIFI_SCAN_VERIFY_COMMAND = "iwinfo phy0-ap0 scan | head"
WIFI_IFACE_DISABLED_NOTE = (
    "a fresh OpenWrt on this board comes up with Wi-Fi DISABLED (both AP wifi-iface "
    "sections `option disabled 1`, radios up, zero interfaces). Enable the IFACES, not "
    "just the radios, before any portal evidence."
)


def count_wifi_ifaces(ubus_output: str) -> int:
    """Number of ``"ifname"`` occurrences in ``ubus call network.wireless status``."""
    return len(re.findall(r'"ifname"', ubus_output or ""))


def wifi_iface_violations(ubus_output: str) -> list[str]:
    """Report a box with zero wireless interfaces (a known false-pass trap)."""
    if count_wifi_ifaces(ubus_output) > 0:
        return []
    return [
        "the box reports ZERO wireless interfaces (no \"ifname\" in "
        f"`{WIFI_VERIFY_COMMAND}`) — {WIFI_IFACE_DISABLED_NOTE} Run: "
        + " && ".join(WIFI_IFACE_ENABLE_COMMANDS)
    ]


# ---------------------------------------------------------------------------
# Stage-3: the TollGate install handoff (reuse, never reimplement)
# ---------------------------------------------------------------------------

#: a fresh OpenWrt has an EMPTY root password, so the lab password must be SET as
#: part of the handover; ``chpasswd`` does not exist on OpenWrt.
ROOT_PASSWORD_ENV = "TOLLGATE_ROUTER_PASSWORD"
SET_ROOT_PASSWORD_COMMAND = "passwd root"
SET_ROOT_PASSWORD_NOTE = (
    "a freshly flashed OpenWrt has an EMPTY root password. Set the lab password as part "
    "of the handover: printf '%s\\n%s\\n' \"$PASSWORD\" \"$PASSWORD\" | ssh root@<router> "
    "passwd root   # chpasswd does NOT exist on OpenWrt"
)

#: the no-brick install ORDER the kit's own Cudy notes pin (do not reorder)
TOLLGATE_INSTALL_ORDER = (
    "dependencies",
    "keepalive (trustedmac + SSH-22 pre-auth, seeded BEFORE enforcement)",
    "nodogsplash",
    "tollgate-wrt LAST",
)


@dataclass(frozen=True)
class InstallHandoff:
    """Where the TollGate install actually lives — the kit already owns it."""

    order: tuple[str, ...] = TOLLGATE_INSTALL_ORDER
    make_target: str = "install-path-e2e"
    script: str = "scripts/install-path-e2e.py --flash-and-run"
    library: str = "lib/install_paths.py"
    package_lane_script: str = "scripts/fresh-flash.py"
    prerequisites: tuple[str, ...] = field(
        default_factory=lambda: (
            "set the root password (fresh image: EMPTY) — " + ROOT_PASSWORD_ENV,
            "enable the AP wifi-iface sections (they boot `disabled 1`)",
            "confirm the box answers on 192.168.1.1 with the expected board token",
        )
    )

    def commands(self) -> tuple[str, ...]:
        return (
            f"make {self.make_target}",
            self.script,
        )

    def describe(self) -> str:
        return (
            "TollGate install is NOT reimplemented in this lane: it is "
            f"{self.script} via `make {self.make_target}` ({self.library}: artifact "
            f"selection, artifact-identity gate, policy gate). Install order: "
            + " -> ".join(self.order)
        )


def tollgate_install_handoff() -> InstallHandoff:
    """The stage-3 handoff description (this lane only *enters* the existing path)."""
    return InstallHandoff()


# ---------------------------------------------------------------------------
# Module identity / allotment checks (the verification ladder's first rungs)
# ---------------------------------------------------------------------------

#: the module API's identity kind (``curl http://<box>:2121/``)
MODULE_API_KIND = 10021
#: missing ``price_per_step`` tags == degraded mode, never evidence
MODULE_API_URL = "http://{host}:2121/"


def parse_kind(text: str) -> int | None:
    """The ``kind`` integer out of a module/API JSON payload, if present."""
    match = re.search(r'"kind"\s*:\s*(\d+)', text or "")
    return int(match.group(1)) if match else None


def module_identity_violations(api_text: str, *, expect_kind: int = MODULE_API_KIND) -> list[str]:
    """Post-install identity check: the box answers ``kind:10021`` in FULL mode.

    Missing ``price_per_step`` tags means degraded mode — it must be fixed before
    any evidence is recorded (the kit's own Cudy notes say so).
    """
    problems: list[str] = []
    kind = parse_kind(api_text)
    if kind is None:
        problems.append(
            f"the module API answered no `kind` at all (nothing installed yet?): {api_text.strip()[:120]!r}"
        )
    elif kind != expect_kind:
        problems.append(f"the module API answered kind:{kind}, expected kind:{expect_kind}")
    if "price_per_step" not in (api_text or ""):
        problems.append(
            "the module API payload has no `price_per_step` tags — the module is in DEGRADED "
            "mode; fix it before recording any evidence"
        )
    return problems


# ---------------------------------------------------------------------------
# The flash-capacity wall (VERIFIED 2026-09-27) + the volatile (tmpfs) install
# ---------------------------------------------------------------------------
#
# A Cudy WR3000 v1 has 16 MB SPI-NOR.  Measured on the bench box:
#   mtd5  firmware   15.1 MB (0xf10000) = kernel (mtd6, 4.2 MB) + rootfs (mtd7, 10.8 MB)
#   mtd8  rootfs_data    5.9 MB, of which only ~4.6 MB was FREE (the jffs2 overlay)
#   the tollgate-wrt payload, UNCOMPRESSED = 21 MB:
#       usr/bin/tollgate-wrt  12,361,280 B
#       usr/bin/tollgate       7,373,632 B
#       etc/ 924 K, www/ 216 K, lib/ ~1.2 MB
#   the .apk/.ipk is 8.5 MB COMPRESSED — it cannot be unpacked into 4.6 MB free.
# So `apk` dies mid-extract: `failed to extract usr/bin/tollgate-wrt: No space left on
# device`.  A custom ImageBuilder image fails the same arithmetic (base squashfs ~6.5 MB +
# kernel 3.2 MB + ~8.5 MB compressed payload > 15.1 MB firmware area).
#
# The operator's chosen answer is a VOLATILE (tmpfs) install: ~117 MB of tmpfs is free, so
# the two big binaries live in /tmp and are symlinked from /usr/bin, while the small parts
# (~1.2 MB) go on flash.  It is NOT persistent, and this module says so loudly.

FLASH_TOTAL_BYTES = 0xF10000              # mtd5 firmware area, 15.1 MB
FLASH_KERNEL_BYTES = 4_200_000            # mtd6 (as measured)
FLASH_ROOTFS_BYTES = 10_800_000           # mtd7 (as measured)
OVERLAY_TOTAL_BYTES = 5_900_000           # mtd8 rootfs_data (as measured)
OVERLAY_FREE_BYTES_MEASURED = 4_600_000   # free at install time on the bench box
TMPFS_FREE_BYTES_MEASURED = 117 * 1024 * 1024

TOLLGATE_BINARY_TOLLGATE_WRT_BYTES = 12_361_280
TOLLGATE_BINARY_TOLLGATE_BYTES = 7_373_632
TOLLGATE_SMALL_PARTS_BYTES = 924_000 + 216_000 + 1_200_000
TOLLGATE_UNCOMPRESSED_BYTES = 21 * 1024 * 1024   # whole payload, measured
TOLLGATE_PACKAGE_COMPRESSED_BYTES = 8_500_000    # .apk/.ipk, measured

#: on-device probes: free KB on the jffs2 overlay and on tmpfs (/tmp)
OVERLAY_FREE_COMMAND = "df -k /overlay 2>/dev/null | awk 'NR==2 {print $4}'"
TMPFS_FREE_COMMAND = "df -k /tmp 2>/dev/null | awk 'NR==2 {print $4}'"
#: list a (gzipped) package's members with sizes, for the uncompressed-payload sum
PACKAGE_LIST_COMMAND = "tar -tvzf {package}"

MODE_FLASH = "flash"
MODE_VOLATILE = "volatile"
INSTALL_MODES = (MODE_FLASH, MODE_VOLATILE)


def parse_df_kb(text: str) -> int | None:
    """First integer in a ``df -k`` output (a KB count), as bytes; ``None`` if absent."""
    match = re.search(r"(\d+)", text or "")
    return int(match.group(1)) * 1024 if match else None


def tar_uncompressed_bytes(listing: str) -> int:
    """Sum the size column of a ``tar -tvzf`` listing (the payload's UNCOMPRESSED bytes).

    GNU/BusyBox ``tar -tv`` print ``<perms> <owner/group> <size> <date> <time> <name>``;
    the size is the third whitespace-separated field.  Directories and symlinks show 0 and
    contribute nothing.  Returns 0 for an empty/garbled listing (callers treat 0 as
    "unknown", never as small).
    """
    total = 0
    for line in (listing or "").splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[2].isdigit():
            total += int(fields[2])
    return total


@dataclass(frozen=True)
class CapacityVerdict:
    """Whether a payload fits the free space for a given install mode."""

    payload_bytes: int = 0
    available_bytes: int = 0
    mode: str = MODE_FLASH
    fits: bool = False
    reason: str = ""

    def describe(self) -> str:
        return (
            f"mode={self.mode} payload={self.payload_bytes} B "
            f"available={self.available_bytes} B fits={self.fits}"
            + (f" — {self.reason}" if self.reason else "")
        )


def check_install_capacity(
    payload_bytes: int, *, available_bytes: int, mode: str = MODE_FLASH
) -> CapacityVerdict:
    """Refuse a flash install that would die mid-extract instead of with an ENOSPC.

    ``mode`` is ``"flash"`` (the jffs2 overlay) or ``"volatile"`` (tmpfs ``/tmp``).  The
    reason names the payload size against the free space, exactly as the hardware run
    measured it, so the operator sees the arithmetic rather than `apk`'s ENOSPC.
    """
    if mode not in INSTALL_MODES:
        raise ValueError(f"unknown install mode {mode!r}; expected one of {INSTALL_MODES}")
    payload = int(payload_bytes)
    available = int(available_bytes)
    fits = payload <= available
    reason = ""
    if not fits:
        where = "overlay" if mode == MODE_FLASH else "tmpfs (/tmp)"
        reason = (
            f"payload size {payload} B vs free {where} {available} B — the payload is larger "
            f"than the free space.  A {mode} install would die mid-extract with "
            "`failed to extract usr/bin/tollgate-wrt: No space left on device` (ENOSPC). "
            "Use the VOLATILE install (`install-tollgate --volatile`), which puts the two big "
            "binaries in tmpfs and IS LOST ON REBOOT, or ship a smaller payload."
        )
    return CapacityVerdict(
        payload_bytes=payload, available_bytes=available, mode=mode, fits=fits, reason=reason
    )


def capacity_problems(verdict: CapacityVerdict) -> list[str]:
    """A refusal string when the payload does not fit (``[]`` when it does)."""
    if verdict.fits:
        return []
    return [f"REFUSING TO INSTALL: {verdict.reason}"]


# --- the volatile (tmpfs) install -------------------------------------------------

VOLATILE_TMP_DIR = "/tmp/tollgate-wrt-volatile"
#: the two large binaries that move to tmpfs (symlinked back into /usr/bin)
VOLATILE_BIG_BINARIES = ("usr/bin/tollgate-wrt", "usr/bin/tollgate")
#: the small parts that stay on flash
VOLATILE_FLASH_PREFIXES = ("etc/", "www/", "lib/")
VOLATILE_INSTALL_NOTE = (
    "VOLATILE (tmpfs) install: the two big binaries live in " + VOLATILE_TMP_DIR + " (RAM) and "
    "are symlinked from /usr/bin; the small parts (etc/, www/, lib/ ~1.2 MB) go on flash. "
    "THE INSTALL IS LOST ON REBOOT — /tmp is tmpfs, it does not survive a power cycle or a "
    "`sysupgrade`. This lane will NOT report a volatile install as persistent."
)


@dataclass(frozen=True)
class VolatileInstallPlan:
    """The deterministic, idempotent step list for a tmpfs install of the big binaries."""

    payload_bytes: int = TOLLGATE_UNCOMPRESSED_BYTES
    tmp_dir: str = VOLATILE_TMP_DIR
    big_binaries: tuple[str, ...] = VOLATILE_BIG_BINARIES
    flash_prefixes: tuple[str, ...] = VOLATILE_FLASH_PREFIXES
    warnings: tuple[str, ...] = (VOLATILE_INSTALL_NOTE,)

    @property
    def persistent(self) -> bool:
        """Always ``False`` — this plan is by definition not persistent."""
        return False

    def commands(self, remote_package: str = "/tmp/<package>.apk") -> tuple[str, ...]:
        """The router-side commands (idempotent: ``mkdir -p`` / ``ln -sf`` / ``cp -a``)."""
        steps = [f"mkdir -p {self.tmp_dir}"]
        steps.append(f"tar -xzf {remote_package} -C {self.tmp_dir}")
        for relative in self.big_binaries:
            steps.append(f"ln -sf {self.tmp_dir}/{relative} /{relative}")
        for prefix in self.flash_prefixes:
            steps.append(f"cp -a {self.tmp_dir}/{prefix}. /{prefix} 2>/dev/null || true")
        return tuple(steps)

    def describe(self) -> str:
        return (
            f"volatile install plan: payload={self.payload_bytes} B -> {self.tmp_dir}; "
            f"symlink {list(self.big_binaries)} into /; flash {list(self.flash_prefixes)}; "
            f"persistent={self.persistent}.  {VOLATILE_INSTALL_NOTE}"
        )


def volatile_install_plan(
    payload_bytes: int = TOLLGATE_UNCOMPRESSED_BYTES, *, tmp_free_bytes: int = 0
) -> VolatileInstallPlan:
    """Build the volatile install plan (never claims persistence)."""
    return VolatileInstallPlan(payload_bytes=int(payload_bytes))


def volatile_persistence_violations(claim_persistent: bool) -> list[str]:
    """A refusal when anything claims a volatile install survives a reboot."""
    if claim_persistent:
        return [
            "REFUSING TO CALL THIS PERSISTENT: a volatile install lives in tmpfs (/tmp) and "
            "is LOST on reboot. " + VOLATILE_INSTALL_NOTE
        ]
    return []


# ---------------------------------------------------------------------------
# Lock policy — explicit and documented, never silent
# ---------------------------------------------------------------------------

#: The kit's ``~/.hermes/state/bench-mt3000.lock`` is one file for the MT3000.  The
#: Cudy is a physically SEPARATE box on a different L2/segment, so it is NOT the
#: same single-owner resource; taking that flock for the Cudy would serialise two
#: unrelated boxes.  The lane therefore uses its OWN lock name, and only when the
#: operator asks for it.
LOCK_CHOICE_NOTE = (
    "lock policy (explicit): the Cudy WR3000 is a separate physical box from the MT3000 "
    "bench, so this lane does NOT take the shared bench flock "
    "(~/.hermes/state/bench-mt3000.lock) by default. When the Cudy shares the bench host's "
    "wire/segment with another owner, serialise it explicitly with "
    "TOLLGATE_CUDY_TAKE_BENCH_LOCK=true, which takes its own lock name ("
    "~/.hermes/state/bench-cudy-wr3000.lock by default, overridable with TOLLGATE_CUDY_LOCK)."
)
LOCK_ENV = "TOLLGATE_CUDY_TAKE_BENCH_LOCK"
LOCK_PATH_ENV = "TOLLGATE_CUDY_LOCK"
DEFAULT_LOCK_PATH = os.path.expanduser("~/.hermes/state/bench-cudy-wr3000.lock")


def cudy_lock_path() -> str:
    return os.path.expanduser(os.environ.get(LOCK_PATH_ENV) or DEFAULT_LOCK_PATH)


def take_cudy_lock() -> bool:
    """Should this run serialise on the Cudy's own lock file?"""
    return os.environ.get(LOCK_ENV, "").strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Gate aggregation
# ---------------------------------------------------------------------------

CONFIRM_FLAG = "--yes-i-mean-it"


def require_confirm(confirmed: bool, *, action: str) -> None:
    """Refuse a destructive action that was not explicitly confirmed.

    The destructive switch (``TOLLGATE_ENABLE_SYSUPGRADE_FLASHING``) says the
    operator *may* flash; ``--yes-i-mean-it`` says they *mean this one right now*.
    Both are required for every mutating stage.
    """
    if confirmed:
        return
    raise ff.FlashRefused(
        f"REFUSING {action} without {CONFIRM_FLAG}. Both the destructive switch "
        f"({ff.FLASH_ENABLE_ENV}=true) and an explicit {CONFIRM_FLAG} are required, so a "
        "stray cron or a mistyped re-run cannot replace this box's firmware on its own."
    )


def preconditions(
    *,
    stage: str,
    enable: bool,
    model: ModelVerdict | None = None,
    image_problems: tuple[str, ...] | list[str] = (),
    wallet_problems: tuple[str, ...] | list[str] = (),
    page_problems: tuple[str, ...] | list[str] = (),
    install_problems: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """Every blocker for a stage, as strings (``[]`` = go) — reported all at once.

    ``stage`` is ``"oem-upload"`` or ``"sysupgrade"``.  Both are destructive, so
    both need the kit's destructive switch; the wallet gate applies to the
    ``sysupgrade`` stage only (an OEM/CudyOS box has no ``/etc/tollgate``).
    ``install_problems`` carries the TollGate-install refusals (e.g. the
    flash-capacity preflight) so the ``install-tollgate`` stage can report them
    alongside the switch.
    """
    problems: list[str] = []
    if not enable:
        problems.append(
            f"{ff.FLASH_ENABLE_ENV} is not 'true' — the destructive flash switch is off "
            "(flashing wipes the box's config; opt in explicitly)"
        )
    if model is not None and not model.supported:
        problems.append(
            f"hardware is not a positively-identified WR3000 v1.0 ({model.describe()}) — "
            "refusing to flash"
        )
    problems.extend(image_problems)
    problems.extend(wallet_problems)
    problems.extend(page_problems)
    problems.extend(install_problems)
    return problems
