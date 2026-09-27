#!/usr/bin/env python3
"""Compose a branded evidence film from a phone recording.

Creates a split-screen film: phone screen on the left, brand info card
on the right with the portal logo, tagline, and test metadata.
"""
import argparse
import pathlib
import subprocess
import tempfile
from PIL import Image, ImageDraw, ImageFont

FONT_UI = "/System/Library/Fonts/SFNS.ttf"
FONT_MONO = "/System/Library/Fonts/Menlo.ttc"

# Brand visual metadata (mirrors lib/portal_build.py PORTALS)
BRANDS = {
    "tollgate": {
        "name": "TollGate",
        "tagline": "Pay-as-you-go internet access",
        "color": "#f97316",
        "rgb": (249, 115, 22),
        "bg": (10, 14, 26),
    },
    "net4sats": {
        "name": "net4sats",
        "tagline": "Prepaid internet — network access for sats",
        "color": "#0891b2",
        "rgb": (8, 145, 178),
        "bg": (4, 17, 31),
    },
}


def run(cmd):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-300:])
    return r


def dur(p):
    return float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(p)],
        capture_output=True, text=True).stdout.strip())


def brand_card(brand_meta: dict, commit: str, message: str, out_png: str):
    """Create a brand-colored info card for the right side of the film."""
    W, H = 1380, 1080
    bg = brand_meta["bg"]
    accent = brand_meta["rgb"]

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    # Accent bar
    d.rectangle([0, 0, W, 8], fill=accent)

    # Brand name (large)
    d.text((60, 60), brand_meta["name"],
           font=ImageFont.truetype(FONT_UI, 56), fill=accent)

    # Tagline
    d.text((60, 140), brand_meta["tagline"],
           font=ImageFont.truetype(FONT_UI, 20), fill=(100, 116, 139))

    # Divider
    d.line([(60, 180), (W - 60, 180)], fill=(30, 41, 59), width=2)

    # Test metadata
    y = 210
    meta_lines = [
        ("Environment", "Cuttlefish + hwsim + OpenWrt"),
        ("Phone", "Android (AOSP, virtual)"),
        ("Router", "tollgate-wrt (Go backend)"),
        ("Mint", "signut.cashu.exchange (signet)"),
        ("Payment", "Cashu ecash, 8 sats"),
    ]
    if commit:
        meta_lines.append(("Commit", commit))
    if message:
        meta_lines.append(("Message", message[:50]))

    for label, value in meta_lines:
        d.text((60, y), label.upper(),
               font=ImageFont.truetype(FONT_MONO, 14), fill=(71, 85, 105))
        d.text((60, y + 22), value,
               font=ImageFont.truetype(FONT_MONO, 18), fill=(203, 213, 225))
        y += 55

    # Flow diagram (simple text)
    d.line([(60, y + 20), (W - 60, y + 20)], fill=(30, 41, 59), width=1)
    y += 40
    flow = [
        "① Phone connects to WiFi",
        "② Captive portal detected",
        "③ Portal SPA loads",
        "④ Token pasted, payment sent",
        "⑤ Router verifies at mint",
        "⑥ Gate opens, internet flows",
    ]
    for step in flow:
        d.text((60, y), step,
               font=ImageFont.truetype(FONT_UI, 18), fill=(148, 163, 184))
        y += 32

    # Footer
    d.text((60, H - 40), "Powered by " + brand_meta["name"],
           font=ImageFont.truetype(FONT_UI, 14), fill=(51, 65, 85))

    img.save(out_png)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", required=True, help="Phone recording mp4")
    ap.add_argument("--brand", required=True, choices=list(BRANDS.keys()))
    ap.add_argument("--brand-name", default="")
    ap.add_argument("--tagline", default="")
    ap.add_argument("--color", default="")
    ap.add_argument("--commit", default="", help="Git commit for the card")
    ap.add_argument("--message", default="", help="Commit message")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    meta = BRANDS[args.brand]
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="compose-"))
    phone_dur = min(dur(args.phone), 180)

    # Create brand card
    card = tmp / "card.png"
    brand_card(meta, args.commit, args.message, str(card))

    # Phone: 540x960 centered in 540x1080
    run(["ffmpeg", "-y", "-loglevel", "error",
         "-i", args.phone,
         "-t", str(phone_dur),
         "-vf", "scale=540:960,pad=540:1080:0:60:black,fps=30,format=yuv420p",
         "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "21",
         str(tmp / "phone.mp4")])

    # Card: loop for phone duration
    run(["ffmpeg", "-y", "-loglevel", "error",
         "-loop", "1", "-t", str(phone_dur), "-i", str(card),
         "-vf", "scale=1380:1080,fps=30,format=yuv420p",
         "-c:v", "libx264", "-preset", "fast", "-crf", "21",
         str(tmp / "card.mp4")])

    # Side-by-side
    run(["ffmpeg", "-y", "-loglevel", "error",
         "-i", str(tmp / "phone.mp4"), "-i", str(tmp / "card.mp4"),
         "-filter_complex", "[0:v][1:v]hstack=inputs=2,format=yuv420p",
         "-c:v", "libx264", "-preset", "fast", "-crf", "21",
         "-shortest", args.out])

    print(f"composed: {args.out} ({dur(args.out):.0f}s)")


if __name__ == "__main__":
    main()
