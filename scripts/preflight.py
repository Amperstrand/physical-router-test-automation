#!/usr/bin/env python3
"""preflight.py — environment autodetection checklist before running tests.

Probes every router in config/routers.json (SSH, brand, device code, SSIDs,
firmware, tollgate-wrt + nodogsplash), ADB devices, docker/cloud-lab
availability, and local QEMU virtual-lab liveness — then prints a human
checklist and notes which pytest lanes are runnable with what.

Writes results/preflight-latest.json (gitignored). The pytest
``--preflight=auto`` session fixture reuses this snapshot.

Usage:
    python3 scripts/preflight.py                    # probe + print + snapshot
    python3 scripts/preflight.py --inventory OTHER  # alternate inventory
    python3 scripts/preflight.py --output PATH      # alternate snapshot path

Exit code 0 even when probes fail — this is a diagnostic, not a gate.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.preflight import DEFAULT_SNAPSHOT_PATH, format_checklist, run_preflight

_DESCRIPTION = "preflight.py — environment autodetection checklist before running tests."


def main() -> int:
    parser = argparse.ArgumentParser(description=_DESCRIPTION)
    parser.add_argument(
        "--inventory", default=None,
        help="Router inventory JSON (default: config/routers.json or TOLLGATE_ROUTER_INVENTORY)",
    )
    parser.add_argument(
        "--output", default=None,
        help=f"Snapshot output path (default: {DEFAULT_SNAPSHOT_PATH})",
    )
    args = parser.parse_args()

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_path = args.output or os.path.join(repo_root, DEFAULT_SNAPSHOT_PATH)
    snapshot = run_preflight(
        inventory_path=args.inventory,
        out_path=out_path,
        repo_root=repo_root,
    )
    print(format_checklist(snapshot))
    runnable = [name for name, info in snapshot.get("lanes", {}).items() if info.get("runnable")]
    print(f"\nRunnable now: {', '.join(runnable) if runnable else 'nothing'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
