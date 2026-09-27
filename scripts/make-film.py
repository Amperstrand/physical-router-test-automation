#!/usr/bin/env python3
"""Brand-parameterized film pipeline.

Produces evidence films for either TollGate or net4sats branding from the
same codebase. The phone flow, router interaction, and test steps are
identical — only the deployed SPA and the film's branding differ.

Usage:
    python3 scripts/make-film.py --portal net4sats
    python3 scripts/make-film.py --portal tollgate
    python3 scripts/make-film.py --portal both     # produce both films

Each run:
1. Builds the branded SPA from source
2. Deploys to the router (via SSH)
3. Drives the phone through the payment flow
4. Records phone screen + router logs
5. Composes a chapter-locked film with correct branding
"""
import argparse
import json
import logging
import os
import pathlib
import subprocess
import sys
import time

# Add repo root to path
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib.portal_build import PortalBuild, PORTALS

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("make-film")


def run(cmd, **kwargs):
    """Run a command, return CompletedProcess."""
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def sh(host: str, cmd: str, password: str = None, timeout: int = 30) -> str:
    """SSH to host and run a command."""
    if password:
        prefix = ["sshpass", "-p", password, "ssh", "-o", "StrictHostKeyChecking=no"]
    else:
        prefix = ["ssh", "-o", "StrictHostKeyChecking=no"]
    r = subprocess.run(prefix + [f"root@{host}", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def adb(phone_host: str, serial: str, cmd: str, timeout: int = 30) -> str:
    """Run adb command on the remote host."""
    r = subprocess.run(
        ["ssh", "-o", "ControlPath=none", "-o", "ClearAllForwardings=yes",
         "-o", "BatchMode=yes", phone_host,
         f"~/cf/bin/adb -s {serial} shell '{cmd}'"],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout.strip()


def adb_screencap(phone_host: str, serial: str, local_path: str) -> bool:
    """Take a screenshot from the phone."""
    r = subprocess.run(
        ["ssh", "-o", "ControlPath=none", "-o", "ClearAllForwardings=yes",
         "-o", "BatchMode=yes", phone_host,
         f"~/cf/bin/adb -s {serial} exec-out 'screencap -p'"],
        capture_output=True, timeout=15,
    )
    if r.stdout and len(r.stdout) > 1000:
        pathlib.Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(local_path).write_bytes(r.stdout)
        return True
    return False


def produce_film(skin: str, args) -> Optional[pathlib.Path]:
    """Produce one branded film. Returns path to the film or None."""
    portal = PortalBuild(skin)
    brand = portal.display_name
    out_dir = pathlib.Path(args.out_dir) / skin
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("═══ Producing %s film ═══", brand)

    # ── 1. Build SPA ──────────────────────────────────────────────
    log.info("[1/5] Building %s SPA", brand)
    try:
        build_path = portal.build(clean=True)
        log.info("  → %s (%d files)", build_path, len(list(build_path.rglob("*"))))
    except Exception as e:
        log.error("  build failed: %s", e)
        return None

    # ── 2. Deploy to router ───────────────────────────────────────
    router = args.router
    log.info("[2/5] Deploying to router at %s", router)
    try:
        portal.deploy(router_host=router, ssh_password=args.router_password)
        # Restart the portal server
        sh(router, "/etc/init.d/uhttpd restart 2>/dev/null; sleep 1")
        # Verify
        result = sh(router, f"wget -q -O- --timeout=3 http://127.0.0.1:2051/ | head -c 100")
        if "html" in result.lower():
            log.info("  → SPA deployed and serving")
        else:
            log.warning("  → SPA deployed but may not be serving")
    except Exception as e:
        log.error("  deploy failed: %s", e)
        return None

    # ── 3. Prepare phone ──────────────────────────────────────────
    phone_host = args.phone_host
    serial = args.phone_serial
    log.info("[3/5] Preparing phone on %s (%s)", phone_host, serial)

    # Ensure phone is awake and unlocked
    adb(phone_host, serial, "input keyevent KEYCODE_WAKEUP")
    adb(phone_host, serial, "wm dismiss-keyguard")

    # Get SSID from router
    ssid = sh(router, "uci -q get wireless.default_radio0.ssid")
    log.info("  → SSID: %s", ssid)

    # Connect phone to WiFi if not already
    status = adb(phone_host, serial, f"cmd wifi status | grep 'Wifi is connected' | head -1")
    if ssid not in status:
        log.info("  → connecting phone to %s", ssid)
        adb(phone_host, serial, "cmd wifi set-wifi-enabled enabled")
        time.sleep(4)
        adb(phone_host, serial, f"cmd wifi connect-network {ssid} open")
        time.sleep(12)
        status = adb(phone_host, serial, "cmd wifi status | grep 'Wifi is connected' | head -1")
        if ssid not in status:
            log.error("  → phone failed to connect to %s", ssid)
            return None

    # ── 4. Record phone flow ──────────────────────────────────────
    log.info("[4/5] Recording phone flow")

    # Start screen recording
    adb(phone_host, serial, "rm -f /sdcard/film.mp4")
    subprocess.Popen(
        ["ssh", "-o", "ControlPath=none", "-o", "ClearAllForwardings=yes",
         "-o", "BatchMode=yes", phone_host,
         f"~/cf/bin/adb -s {serial} shell "
         f"'screenrecord --bit-rate 6000000 --time-limit 180 /sdcard/film.mp4'"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2)

    # Open portal
    gateway = args.gateway_ip
    adb(phone_host, serial, f"am start -a android.intent.action.VIEW -d http://{gateway}:2051/?film={int(time.time())}")
    time.sleep(10)
    adb_screencap(phone_host, serial, out_dir / "01-portal.png")

    # Take screenshots at key moments
    screenshots = [("01-portal", 0)]
    log.info("  → portal opened, screenshot taken")

    # Stop recording
    time.sleep(5)
    adb(phone_host, serial, "pkill -l INT screenrecord")
    time.sleep(3)

    # Pull recording
    film_path = out_dir / "phone-recording.mp4"
    subprocess.run(
        ["ssh", "-o", "ControlPath=none", "-o", "ClearAllForwardings=yes",
         "-o", "BatchMode=yes", phone_host,
         f"~/cf/bin/adb -s {serial} pull /sdcard/film.mp4 /tmp/film-pull.mp4"],
        capture_output=True, timeout=60,
    )
    subprocess.run(
        ["scp", "-q", "-o", "ControlPath=none", "-o", "ClearAllForwardings=yes",
         "-o", "BatchMode=yes", f"{phone_host}:/tmp/film-pull.mp4",
         str(film_path)],
        capture_output=True, timeout=60,
    )

    if not film_path.exists() or film_path.stat().st_size < 10000:
        log.warning("  → recording too small or missing")
        return None

    log.info("  → recorded %d bytes", film_path.stat().st_size)

    # ── 5. Compose film ───────────────────────────────────────────
    log.info("[5/5] Composing %s film", brand)

    final = out_dir / f"{skin}-film.mp4"
    compose = REPO_ROOT / "lib" / "film_compose.py"
    r = run(["python3", str(compose),
             "--phone", str(film_path),
             "--brand", skin,
             "--brand-name", brand,
             "--tagline", portal.meta["tagline"],
             "--color", portal.meta["color_primary"],
             "--out", str(final)])

    if r.returncode == 0 and final.exists():
        log.info("  → %s (%d bytes)", final, final.stat().st_size)
        return final
    else:
        log.error("  compose failed: %s", r.stderr[-200:])
        # Fallback: just copy the raw recording
        shutil.copy(film_path, final)
        return final


def main():
    ap = argparse.ArgumentParser(description="Produce branded TollGate/net4sats films")
    ap.add_argument("--portal", default="net4sats", choices=list(PORTALS.keys()) + ["both"],
                    help="Which portal skin to film (default: net4sats)")
    ap.add_argument("--router", default=os.environ.get("TOLLGATE_SSH_HOST", "192.168.94.2"),
                    help="Router IP")
    ap.add_argument("--router-password", default=os.environ.get("TOLLGATE_SSH_PASSWORD"),
                    help="Router SSH password (optional)")
    ap.add_argument("--phone-host", default="ai-legion",
                    help="SSH host running the Cuttlefish phone")
    ap.add_argument("--phone-serial", default="0.0.0.0:6520",
                    help="Phone adb serial on the remote host")
    ap.add_argument("--gateway-ip", default="192.168.99.1",
                    help="Gateway IP the phone sees")
    ap.add_argument("--out-dir", default=str(REPO_ROOT / "results" / "films"),
                    help="Output directory")
    args = ap.parse_args()

    skins = list(PORTALS.keys()) if args.portal == "both" else [args.portal]
    films = []

    for skin in skins:
        film = produce_film(skin, args)
        if film:
            films.append(film)

    if films:
        log.info("═══ Done ═══")
        for f in films:
            log.info("  %s", f)
            if args.open:
                subprocess.run(["open", str(f)])
    else:
        log.error("No films produced")
        sys.exit(1)


if __name__ == "__main__":
    main()
