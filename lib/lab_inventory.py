"""Gitignored physical-lab inventory loader.

Real device identifiers (MACs, serials, per-unit IPs, passwords, keyfile
paths) live ONLY in ``configs/labgrid/inventory.local.yaml`` — gitignored
by policy. The committed ``inventory.local.yaml.example`` carries
placeholders and the update procedure. Code, docs and labgrid environments
reference LOGICAL names only (``ap-lan2``).

Nothing in this module ever puts identifier VALUES into log output or
exception text: errors name the missing field and the file to create,
never the content of any field.

Protected ports: the union of switch-level ``protected_ports`` and any
router with ``protected: true`` flows into PoeControllerConfig, whose
_manage() refuses them before any ubus call is issued. Ports are protected
for physical reasons (one-way-trip TFTP units, uplink trunks) — the guard
exists so no test can bypass them by accident.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PATH = _REPO_ROOT / "configs" / "labgrid" / "inventory.local.yaml"
_EXAMPLE_PATH = _REPO_ROOT / "configs" / "labgrid" / "inventory.local.yaml.example"


class InventoryError(RuntimeError):
    """Inventory missing or malformed — points at the file to fix, not values."""


@dataclass(frozen=True)
class SwitchEntry:
    host: str
    username: str = "root"
    keyfile: str | None = None
    port: int = 22


@dataclass(frozen=True)
class RouterEntry:
    name: str
    poe_port: str
    model: str = ""
    vlan: int = 0
    address: str = ""
    password: str = ""
    mac: str = ""
    serial: str = ""
    serial_console: str = ""
    protected: bool = False
    note: str = ""
    place: str = ""
    keyfile: str = ""
    jump_host: str = ""


@dataclass(frozen=True)
class CoordinatorEntry:
    address: str = ""


@dataclass(frozen=True)
class LabInventory:
    switch: SwitchEntry
    routers: dict[str, RouterEntry]
    coordinator: CoordinatorEntry = field(default_factory=CoordinatorEntry)
    switch_protected_ports: frozenset[str] = frozenset()

    @property
    def protected_ports(self) -> frozenset[str]:
        """Switch-declared ports plus every router flagged protected."""
        from_routers = frozenset(
            r.poe_port for r in self.routers.values() if r.protected
        )
        return self.switch_protected_ports | from_routers

    def router(self, name: str) -> RouterEntry:
        try:
            return self.routers[name]
        except KeyError:
            known = sorted(self.routers) or ["(none)"]
            raise InventoryError(
                f"router {name!r} not in inventory (known: {known}) — add it to "
                f"{_DEFAULT_PATH} (schema: {_EXAMPLE_PATH})"
            ) from None


def inventory_path() -> Path:
    override = os.environ.get("TOLLGATE_LAB_INVENTORY")
    return Path(override).expanduser() if override else _DEFAULT_PATH


def inventory_exists() -> bool:
    return inventory_path().is_file()


def load_inventory(path: Path | None = None) -> LabInventory:
    """Parse and validate the gitignored inventory.

    Raises InventoryError with actionable, identifier-free messages.
    """
    src = (path or inventory_path()).expanduser()
    if not src.is_file():
        raise InventoryError(
            f"physical-lab inventory not found at {src} — copy the schema from "
            f"{_EXAMPLE_PATH} and fill in real values (gitignored, never commit)"
        )
    try:
        doc = yaml.safe_load(src.read_text())
    except yaml.YAMLError as e:
        raise InventoryError(f"inventory {src} is not valid YAML: {e}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("switch"), dict):
        raise InventoryError(
            f"inventory {src} must contain a 'switch:' mapping (see {_EXAMPLE_PATH})"
        )

    switch_doc = doc["switch"]
    host = str(switch_doc.get("host", "")).strip()
    if not host or host.startswith("<"):
        raise InventoryError(
            f"inventory {src} switch.host is unset or still a placeholder — "
            f"fill it in locally (schema: {_EXAMPLE_PATH})"
        )
    keyfile = str(switch_doc["keyfile"]) if switch_doc.get("keyfile") else None
    switch = SwitchEntry(
        host=host,
        username=str(switch_doc.get("username", "root")),
        keyfile=keyfile,
        port=int(switch_doc.get("port", 22)),
    )

    switch_protected = frozenset(
        str(p) for p in (doc.get("protected_ports") or [])
    )

    coord_doc = doc.get("coordinator") or {}
    coordinator = CoordinatorEntry(address=str(coord_doc.get("address", "") or ""))

    routers: dict[str, RouterEntry] = {}
    for name, entry in (doc.get("routers") or {}).items():
        if not isinstance(entry, dict) or not entry.get("poe_port"):
            raise InventoryError(
                f"inventory router {name!r} needs at least a poe_port — "
                f"see {_EXAMPLE_PATH}"
            )
        routers[str(name)] = RouterEntry(
            name=str(name),
            poe_port=str(entry["poe_port"]),
            model=str(entry.get("model", "") or ""),
            vlan=int(entry.get("vlan", 0) or 0),
            address=str(entry.get("address", "") or ""),
            password=str(entry.get("password", "") or ""),
            mac=str(entry.get("mac", "") or ""),
            serial=str(entry.get("serial", "") or ""),
            serial_console=str(entry.get("serial_console", "") or ""),
            protected=bool(entry.get("protected", False)),
            note=str(entry.get("note", "") or ""),
            place=str(entry.get("place", "") or ""),
            keyfile=str(entry.get("keyfile", "") or ""),
            jump_host=str(entry.get("jump_host", "") or ""),
        )

    return LabInventory(
        switch=switch,
        routers=routers,
        coordinator=coordinator,
        switch_protected_ports=switch_protected,
    )


def poe_controller_config(inv: LabInventory):
    """Build a PoeControllerConfig from inventory (keyfile expanded,
    protected ports enforced by the controller)."""
    from tollgate_lab.hardware.poe import PoeControllerConfig

    return PoeControllerConfig(
        host=inv.switch.host,
        username=inv.switch.username,
        keyfile=os.path.expanduser(inv.switch.keyfile) if inv.switch.keyfile else None,
        port=inv.switch.port,
        protected_ports=inv.protected_ports,
    )
