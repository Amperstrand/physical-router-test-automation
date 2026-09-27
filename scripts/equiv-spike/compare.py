#!/usr/bin/env python3
"""Compare oracle vs SDK-free artifacts from an equiv-check run directory.

Logical equality (adbdump canonical view) is the release-gate criterion;
cross-lane byte equality additionally requires identical payload tree
insertion order — apk mkpkg sorts logically but not physically until the
upstream sorted-insertion fix ships in an SDK. --strict-bytes demands it.

Usage: compare.py <run_dir> [--strict-bytes]
Exit: 0 equivalent, 1 regression.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

STATIC_APK = Path.home() / "tg-equiv/laneB2/apk.static"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path: Path) -> dict:
    out = subprocess.run(
        [str(STATIC_APK), "adbdump", "--format", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def info_of(d: dict) -> dict:
    fields = {}
    for x in d.get("info", []):
        if isinstance(x, str) and ":" in x:
            k, v = x.split(":", 1)
            fields[k] = v
    return fields


def main() -> int:
    run = Path(sys.argv[1])
    strict = "--strict-bytes" in sys.argv[2:]
    b1 = json.loads((run / "laneB1.dump.json").read_text())
    b2 = dump(run / "laneB2.apk")

    checks = {
        "info": info_of(b1) == info_of(b2),
        "scripts": (b1.get("scripts") or {}) == (b2.get("scripts") or {}),
        "dirs": sorted(str(p.get("name")) for p in b1["paths"])
        == sorted(str(p.get("name")) for p in b2["paths"]),
    }
    print(f"logical: {checks}")
    h1 = sha(run / "laneB1.apk")
    h2 = sha(run / "laneB2.apk")
    print(f"bytes:   b1={h1[:16]}… b2={h2[:16]}… equal={h1 == h2}")

    ok = all(checks.values())
    if strict and h1 != h2:
        ok = False
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
