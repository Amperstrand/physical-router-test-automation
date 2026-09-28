"""SSID resolution against the router's decided one-device model.

The product (tollgate-module-basic-go, decided model
docs/architecture/one-device-code.md) derives from ONE 4-char [A-Z0-9]
device code stored in ``uci tollgate.device.code``:

  captive SSID   ``<brand>-<code>``   brand prefix default ``TollGate-``
                                       (Net4sats-brand routers use ``Net4sats-``)
  private SSID   ``<nym>-<code>``     nym default ``c08r4d0r``
  portal banner  ``<captive SSID> Portal``

This module resolves those values from the ROUTER at runtime (via an
injected ``run_ssh(cmd) -> stdout`` callable so tests can mock SSH) with
config/env fallbacks. Prefixes are LISTS because the captive brand prefix
varies per router brand.

Prefix convention: prefixes are stored WITHOUT the trailing dash
(``"TollGate"``, ``"Net4sats"``); matching is ``ssid == p`` or
``ssid.startswith(p + "-")``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Mapping

DEFAULT_CAPTIVE_PREFIX = "TollGate"
DEFAULT_PRIVATE_PREFIX = "c08r4d0r"  # the nym

DEVICE_CODE_RE = re.compile(r"^[A-Z0-9]{4}$")

# Legacy literal kept as the last-resort fallback for benches whose firmware
# predates uci tollgate.device.code (test_multihop used to hardcode this).
LEGACY_RESELLER_SSID = "c08r4d0r-C830"

RunSsh = Callable[[str], str]


def normalize_prefix(prefix: str) -> str:
    """Trim a configured prefix: strip whitespace and any trailing dash."""
    return prefix.strip().rstrip("-").strip()


def _split_list(raw: str) -> list[str]:
    out = []
    for part in raw.split(","):
        p = normalize_prefix(part)
        if p:
            out.append(p)
    return out


def captive_prefixes_from_env(env: Mapping[str, str] | None = None) -> list[str]:
    """Captive brand prefixes from env: TOLLGATE_CAPTIVE_SSID_PREFIXES
    (comma-separated) → TOLLGATE_SSID_PREFIX → [TollGate]."""
    env = env if env is not None else os.environ
    raw = env.get("TOLLGATE_CAPTIVE_SSID_PREFIXES", "")
    if raw.strip():
        prefixes = _split_list(raw)
        if prefixes:
            return prefixes
    legacy = env.get("TOLLGATE_SSID_PREFIX", "")
    if legacy.strip():
        return [normalize_prefix(legacy)]
    return [DEFAULT_CAPTIVE_PREFIX]


def private_prefixes_from_env(env: Mapping[str, str] | None = None) -> list[str]:
    """Private SSID prefixes (nyms) from env: TOLLGATE_PRIVATE_SSID_PREFIX
    → [c08r4d0r]."""
    env = env if env is not None else os.environ
    raw = env.get("TOLLGATE_PRIVATE_SSID_PREFIX", "")
    if raw.strip():
        return _split_list(raw)
    return [DEFAULT_PRIVATE_PREFIX]


def prefixes_from_inventory(
    entry: Mapping[str, object] | None,
    env: Mapping[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Merge per-router inventory fields with env defaults.

    Inventory schema (all optional, backward compatible):
      ``captiveSsidPrefixes`` — string or list of captive brand prefixes
      ``tollgateSsidPrefix``  — legacy single captive prefix (still honored)
      ``privateSsidPrefix``   — private SSID prefix (nym override)

    Returns ``(captive_prefixes, private_prefixes)``.
    """
    entry = entry or {}
    captive: list[str] = []
    raw_captive = entry.get("captiveSsidPrefixes")
    if isinstance(raw_captive, str):
        captive = _split_list(raw_captive)
    elif isinstance(raw_captive, (list, tuple)):
        captive = [normalize_prefix(str(p)) for p in raw_captive if normalize_prefix(str(p))]
    if not captive:
        legacy = entry.get("tollgateSsidPrefix")
        if isinstance(legacy, str) and legacy.strip():
            captive = [normalize_prefix(legacy)]
    if not captive:
        captive = captive_prefixes_from_env(env)

    private: list[str] = []
    raw_private = entry.get("privateSsidPrefix")
    if isinstance(raw_private, str) and raw_private.strip():
        private = _split_list(raw_private)
    if not private:
        private = private_prefixes_from_env(env)
    return captive, private


def matches_prefix(ssid: str, prefix: str) -> bool:
    """True when ``ssid`` is the bare prefix or ``<prefix>-something``."""
    return ssid == prefix or ssid.startswith(prefix + "-")


def classify_ssid(
    ssid: str,
    captive_prefixes: list[str] | None = None,
    private_prefixes: list[str] | None = None,
) -> str:
    """Classify an SSID as 'captive', 'private', or 'other'.

    Private prefixes are checked first: the nym (c08r4d0r) is more specific
    than any brand prefix.
    """
    captive_prefixes = captive_prefixes if captive_prefixes is not None else captive_prefixes_from_env()
    private_prefixes = private_prefixes if private_prefixes is not None else private_prefixes_from_env()
    for p in private_prefixes:
        if matches_prefix(ssid, p):
            return "private"
    for p in captive_prefixes:
        if matches_prefix(ssid, p):
            return "captive"
    return "other"


def extract_ssids(*outputs: str) -> list[str]:
    """Ordered, de-duplicated SSIDs from any mix of ``iwinfo`` and/or
    ``uci show wireless`` output (each parser only matches its own format).
    Empty/hidden SSIDs are skipped."""
    ssids: list[str] = []
    for out in outputs:
        for line in out.splitlines():
            m = re.search(r'ESSID:\s*"([^"]+)"', line)
            if m and m.group(1).strip():
                ssid = m.group(1).strip()
                if ssid not in ssids:
                    ssids.append(ssid)
                continue
            if ".ssid=" in line:
                _, _, val = line.partition("=")
                val = val.strip().strip("'\"")
                if val and val not in ssids:
                    ssids.append(val)
    return ssids


@dataclass
class RouterIdentity:
    """What the router says about its own SSIDs."""

    device_code: str = ""
    ssids: list[str] = field(default_factory=list)
    captive_ssid: str = ""
    private_ssid: str = ""

    @property
    def code_valid(self) -> bool:
        return bool(DEVICE_CODE_RE.match(self.device_code or ""))


def probe_router_identity(
    run_ssh: RunSsh,
    captive_prefixes: list[str] | None = None,
    private_prefixes: list[str] | None = None,
) -> RouterIdentity:
    """Resolve SSIDs/device code from the live router.

    ``run_ssh(cmd) -> stdout`` executes on the router (Router.ssh or a mock);
    empty output means "not present" (missing uci option, absent tooling).
    No exception handling: SSH-level failures propagate to the caller.
    """
    captive_prefixes = captive_prefixes if captive_prefixes is not None else captive_prefixes_from_env()
    private_prefixes = private_prefixes if private_prefixes is not None else private_prefixes_from_env()

    device_code = run_ssh("uci -q get tollgate.device.code").strip()
    iwinfo = run_ssh("iwinfo 2>/dev/null | grep ESSID")
    uci_wireless = run_ssh("uci show wireless 2>/dev/null | grep '\\.ssid='")
    ssids = extract_ssids(iwinfo, uci_wireless)

    ident = RouterIdentity(device_code=device_code, ssids=ssids)

    # Prefer the code-derived names when the code is present and the router
    # actually broadcasts them.
    if device_code:
        for p in captive_prefixes:
            candidate = f"{p}-{device_code}"
            if candidate in ssids:
                ident.captive_ssid = candidate
                break
        for p in private_prefixes:
            candidate = f"{p}-{device_code}"
            if candidate in ssids:
                ident.private_ssid = candidate
                break

    # Fall back to prefix classification of whatever is broadcast.
    if not ident.captive_ssid:
        for ssid in ssids:
            if classify_ssid(ssid, captive_prefixes, private_prefixes) == "captive":
                ident.captive_ssid = ssid
                break
    if not ident.private_ssid:
        for ssid in ssids:
            if classify_ssid(ssid, captive_prefixes, private_prefixes) == "private":
                ident.private_ssid = ssid
                break
    return ident


def expected_portal_banner(captive_ssid: str) -> str:
    """The banner shape the decided writer produces: '<captive SSID> Portal'."""
    return f"{captive_ssid} Portal"


def resolve_reseller_ssid(
    run_ssh: RunSsh,
    env: Mapping[str, str] | None = None,
    fallback: str = LEGACY_RESELLER_SSID,
    private_prefixes: list[str] | None = None,
) -> str:
    """The SSID a reseller's client AP broadcasts (the private SSID).

    Order: RESELLER_SSID env override → router-resolved private SSID →
    legacy hardcoded fallback (benches without uci tollgate.device.code).
    """
    env = env if env is not None else os.environ
    override = env.get("RESELLER_SSID", "").strip()
    if override:
        return override
    try:
        ident = probe_router_identity(run_ssh, private_prefixes=private_prefixes)
        if ident.private_ssid:
            return ident.private_ssid
    except Exception:
        pass
    return fallback
