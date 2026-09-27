#!/usr/bin/env python3
"""Repack the locally-built v0.6.0 tar.gz-ipk with an env-logging preinst (exit 0).

Purpose: capture the exact environment opkg gives maintainer scripts, and
complete the real upgrade (postinst runs unmodified).
"""
import gzip
import io
import os
import shutil
import tarfile
import time

SRC = "/tmp/v060.ipk"
OUT = "/tmp/probe2.ipk"
WORK = "/tmp/ipkx2"

shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK)

with tarfile.open(SRC, "r:gz") as tf:
    print("outer members:", tf.getnames())
    tf.extractall(WORK)

with tarfile.open(os.path.join(WORK, "control.tar.gz"), "r:gz") as tf:
    ctl_names = tf.getnames()
    print("ctl members:", ctl_names)
    cname = [n for n in ctl_names if n.rstrip("/").lstrip("./").endswith("control")][0]
    control = tf.extractfile(cname).read()

preinst = (
    b"#!/bin/sh\n"
    b'{ echo "=== PREINST ENV ==="; env; echo "PATH=[$PATH]"; '
    b'echo "jq at: $(command -v jq || echo NONE)"; echo "id: $(id)"; '
    b"command -v jq > /dev/null && echo JQ_FOUND || echo JQ_MISSING; } "
    b"> /tmp/preinst-env.txt 2>&1\n"
    b"exit 0\n"
)

buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
    ti = tarfile.TarInfo(cname)
    ti.size = len(control)
    ti.mode = 0o644
    tf.addfile(ti, io.BytesIO(control))
    pname = "./preinst" if cname.startswith("./") else "preinst"
    ti = tarfile.TarInfo(pname)
    ti.size = len(preinst)
    ti.mode = 0o755
    tf.addfile(ti, io.BytesIO(preinst))
ctl_gz = gzip.compress(buf.getvalue(), 9)

with tarfile.open(OUT, "w:gz", format=tarfile.GNU_FORMAT) as tf:

    def add(name: str, data: bytes, mode: int) -> None:
        ti = tarfile.TarInfo(name)
        ti.size = len(data)
        ti.mode = mode
        ti.mtime = int(time.time())
        tf.addfile(ti, io.BytesIO(data))

    add("debian-binary", b"2.0\n", 0o644)
    add("control.tar.gz", ctl_gz, 0o644)
    add("data.tar.gz", open(os.path.join(WORK, "data.tar.gz"), "rb").read(), 0o644)

print("probe2 rebuilt OK ->", OUT)
