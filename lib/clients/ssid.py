"""SSID auto-resolution and router awareness for ClientDevice adapters.

Borrows the SSID resolution pattern from lib/clients/wifi.py but works
without a full WiFi fixture — just needs the router's SSH address.
"""
from __future__ import annotations

import os
import re
import subprocess


def resolve_ssid(router_host: str, prefix: str = "TollGate") -> str:
    """Query the router via SSH for the actual SSID matching the prefix.

    Returns the full SSID (e.g. "TollGate-0805") or the prefix
    unchanged if resolution fails.
    """
    if not router_host:
        return prefix

    # Try iwinfo first
    try:
        out = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5",
             "-o", "StrictHostKeyChecking=no",
             f"root@{router_host}",
             "iwinfo 2>/dev/null | grep ESSID | grep -v private"],
            capture_output=True, text=True, timeout=10).stdout
        for line in out.strip().split("\n"):
            m = re.search(r'ESSID:\s*"([^"]+)"', line)
            if m and m.group(1).startswith(prefix + "-"):
                return m.group(1)
    except Exception:
        pass

    # Fallback: uci wireless config
    try:
        out = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5",
             "-o", "StrictHostKeyChecking=no",
             f"root@{router_host}",
             "uci show wireless 2>/dev/null | grep '\\.ssid=' | grep -v private"],
            capture_output=True, text=True, timeout=10).stdout
        for line in out.strip().split("\n"):
            _, _, val = line.partition("=")
            val = val.strip("'\"")
            if val.startswith(prefix + "-"):
                return val
    except Exception:
        pass

    return prefix


def get_router_host() -> str:
    """Get the router SSH host from environment."""
    return (
        os.environ.get("TOLLGATE_SSH_HOST")
        or os.environ.get("ROUTER_IP")
        or ""
    )
