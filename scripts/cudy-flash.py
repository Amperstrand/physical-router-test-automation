#!/usr/bin/env python3
"""Cudy WR3000 **v1** flash lane CLI — two stages, then the TollGate handoff.

    check           read-only, offline: image identity, model guard, page class,
                    the exact staged commands — changes nothing
    oem-upload      STAGE 1 (destructive): vendor-UI upload of the Cudy-signed
                    transitional image.  The endpoint/file-field are PINNED from a
                    real hardware run (2026-09-27); ``--dump-page`` still logs the
                    real page HTML and uploads nothing.
    sysupgrade      STAGE 2 (destructive): stage the mainline image over ssh
                    stdin (NO sftp-server on these builds), re-verify the sha256
                    ON DEVICE, then ``sysupgrade -n``
    capacity        read-only flash-capacity preflight (payload vs free overlay)
    install-tollgate STAGE 3 handoff: set the root password, enable the AP
                    wifi-iface sections, then enter the kit's EXISTING install
                    path (``scripts/install-path-e2e.py`` / ``make install-path-e2e``).
                    A 16 MB box cannot hold the 21 MB *default* payload, so prefer
                    the project's ``upx-ultra-brute`` compressed variant
                    (5.60 MB uncompressed; VERIFIED ON HARDWARE 2026-09-27 to
                    install persistently).  ``--volatile`` is the FALLBACK: it
                    installs the big binaries into tmpfs, is LOST ON REBOOT, and
                    is never reported as persistent.
    verify          read-only post-install ladder: identity, Wi-Fi ifaces,
                    module ``kind:10021``

Every mutating subcommand is gated the house way: it needs the destructive
switch ``TOLLGATE_ENABLE_SYSUPGRADE_FLASHING=true`` AND (for the sysupgrade)
the wallet gate, which fails closed — a wallet probe that did not answer is
*unknown*, never "empty".  Each subcommand refuses loudly and names its gate.

EVIDENCE: stages 1 and 2 were exercised end to end on a real Cudy WR3000 v1 on
2026-09-27, and the ``upx-ultra-brute`` compressed payload was installed
persistently on the same box on 2026-09-27 (see docs/cudy-wr3000-flashing.md).
The volatile install and the capacity preflight are implemented and unit-tested
but have NOT been run on hardware.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib import cudy_flash as cf  # noqa: E402
from lib import fresh_flash as ff  # noqa: E402
from lib import install_paths as ip  # noqa: E402

#: named exit bits so a caller can tell WHICH gate refused (never a bare 1)
EXIT_OK = 0
EXIT_IMAGE = 1
EXIT_MODEL = 2
EXIT_PAGE = 4
EXIT_SWITCH_OFF = 8
EXIT_WALLET = 16
EXIT_CREDENTIAL = 32
EXIT_REMOTE = 64
EXIT_CAPACITY = 128


# ---------------------------------------------------------------------------
# small transports (HTTP for the vendor UI, ssh for the OpenWrt stages)
# ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Suppress redirect following, so a 302 is observable (the stage-1 success signal)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ANN001
        return None


class HttpSession:
    """Minimal cookie-jar HTTP client for the vendor UI (no third-party deps)."""

    def __init__(self, *, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        # a SECOND opener that does not follow redirects, so the `Proceed` POST's 302 is
        # visible as a 302 (the verified stage-1 success signal) instead of being chased.
        self.opener_noredirect = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect
        )

    def get(self, url: str) -> tuple[int, str]:
        request = urllib.request.Request(url, headers={"User-Agent": "prta-cudy-flash/1"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:  # a 403 login page is a real answer
            return exc.code, exc.read().decode("utf-8", "replace")

    def post_form(self, url: str, fields: dict[str, str]) -> tuple[int, str]:
        data = urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "prta-cudy-flash/1"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def post_form_noredirect(self, url: str, fields: dict[str, str]) -> tuple[int, str]:
        """``post_form`` without following a redirect — a 302 comes back as 302."""
        data = urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "prta-cudy-flash/1"},
        )
        try:
            with self.opener_noredirect.open(request, timeout=self.timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def post_file(self, url: str, field: str, path: str) -> tuple[int, str]:
        """Multipart upload of *path* as *field* (the file input's real ``name``)."""
        boundary = "----prta-cudy-" + uuid.uuid4().hex
        filename = os.path.basename(path)
        with open(path, "rb") as handle:
            payload = handle.read()
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
                b"Content-Type: application/octet-stream\r\n\r\n",
                payload,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "prta-cudy-flash/1",
            },
        )
        try:
            with self.opener.open(request, timeout=300.0) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")


def _ssh_password() -> str:
    return (
        os.environ.get("TOLLGATE_SSH_PASSWORD")
        or os.environ.get("TOLLGATE_LUCI_PASSWORD")
        or ""
    )


def ssh(host: str, command: str, *, timeout: int = 60, password: str | None = None) -> tuple[str, int]:
    """Run a command over ssh (sshpass -e so no secret ever reaches argv)."""
    pw = _ssh_password() if password is None else password
    args = [
        "sshpass", "-e", "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "LogLevel=ERROR",
        f"root@{host}",
        command,
    ]
    return _run(args, pw, timeout=timeout, stdin_text=None)


def ssh_stdin(
    host: str, command: str, stdin_text: str, *, timeout: int = 60, password: str | None = None
) -> tuple[str, int]:
    """Run a command over ssh feeding *stdin_text* on stdin (no secret in argv).

    Used for ``passwd root``: the new password is written to the remote command's
    stdin, so it never appears in a local process list.
    """
    pw = _ssh_password() if password is None else password
    args = [
        "sshpass", "-e", "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "LogLevel=ERROR",
        f"root@{host}",
        command,
    ]
    return _run(args, pw, timeout=timeout, stdin_text=stdin_text)


def _run(args: list[str], password: str, *, timeout: int, stdin_text: str | None) -> tuple[str, int]:
    try:
        result = subprocess.run(
            args,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "SSHPASS": password},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"ssh failed: {exc}", 255
    return (result.stdout + result.stderr).strip(), result.returncode


def wait_for_ssh(host: str, *, timeout: int = 300) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ip.tcp_probe(host, 22, timeout=3):
            out, rc = ssh(host, "echo ready", timeout=15)
            if rc == 0 and "ready" in out:
                return True
        time.sleep(5)
    return False


# ---------------------------------------------------------------------------
# shared gates
# ---------------------------------------------------------------------------


def _lock_note(args: argparse.Namespace) -> None:
    print(f"lock :    {cf.LOCK_CHOICE_NOTE}")
    if cf.take_cudy_lock():
        print(f"lock :    serialising on {cf.cudy_lock_path()} (TOLLGATE_CUDY_TAKE_BENCH_LOCK=true)")
    else:
        print(f"lock :    NOT taking any lock ({cf.LOCK_ENV} not set)")


def _switch_line() -> tuple[bool, str]:
    enabled = ff.flashing_enabled()
    return enabled, (
        f"{ff.FLASH_ENABLE_ENV}={'true' if enabled else '<unset>'} "
        f"-> destructive switch {'ON' if enabled else 'OFF'}"
    )


def _require_switch() -> None:
    ff.flash_enable_gate()  # raises ff.FlashRefused naming the env var and the drain step


def _require_password() -> str:
    password = cf.vendor_password()
    if password:
        return password
    raise SystemExit(
        "REFUSING: no vendor password in the environment. Stage 1 logs into CudyOS with the "
        f"lab password; supply it via one of {list(cf.PASSWORD_ENVS)} (it is deliberately not "
        f"stored in code — the repo's lab convention lives in {cf.PASSWORD_CONVENTION_REF})."
    )


def _image_report(label: str, path: str, problems: list[str]) -> int:
    print(f"{label}: {path}")
    if problems:
        for problem in problems:
            print(f"           PROBLEM: {problem}")
        return EXIT_IMAGE
    print("           sha256/size/name OK")
    sidecar = cf.read_sha256_sidecar(path)
    print(f"           sidecar: {sidecar or '(none)'}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# check — read-only, offline, fail-closed
# ---------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    print("== cudy-flash preconditions (read-only; changes nothing) ==")
    _lock_note(args)
    enabled, line = _switch_line()
    print(f"switch:   {line}")
    rc = 0 if enabled else EXIT_SWITCH_OFF

    try:
        path = cf.resolve_transitional_image(args.image)
        rc |= _image_report("stage1:", path, cf.verify_transitional_image(path))
    except ff.ImageInvalid as exc:
        print(f"stage1:   MISSING — {exc}")
        rc |= EXIT_IMAGE

    try:
        path = cf.resolve_mainline_image(args.mainline_image)
        rc |= _image_report("stage2:", path, cf.verify_mainline_image(path))
        print(f"           upstream provenance: {cf.UPSTREAM_SUMS_URL} (expected {cf.MAINLINE_IMAGE_SHA256})")
    except ff.ImageInvalid as exc:
        print(f"stage2:   MISSING — {exc}")
        rc |= EXIT_IMAGE

    verdict = cf.classify_model(args.model or "")
    print(f"model :   {verdict.describe()}")
    if not verdict.supported:
        rc |= EXIT_MODEL
        print("          -> stage 1 would REFUSE: pass --model '<label text>' from a v1.0 box")

    if args.page_html:
        html = Path(args.page_html).read_text(encoding="utf-8", errors="replace")
    else:
        html = ""
    if html:
        page = cf.classify_page(html, url=args.url)
        print(f"page  :   {page.describe()}")
        print(cf.evidence_summary(html, url=args.url))
        if not page.upload_permitted:
            rc |= EXIT_PAGE
    else:
        print("page  :   (not supplied; pass --page-html <file> to classify a captured page)")

    print()
    print("== the exact commands a live run would issue ==")
    print(f"stage1 modal   : {cf.oem_url(cf.OEM_PANEL_PATH)}  -> Firmware modal (loads the upgrade JS)")
    print(
        f"stage1 upload  : POST {cf.oem_url(cf.OEM_UPLOAD_ENDPOINT)} field={cf.OEM_UPLOAD_FILE_FIELD} "
        f"accept={cf.OEM_UPLOAD_FILE_ACCEPT}  (pinned 2026-09-27)"
    )
    print(
        f"stage1 proceed : POST {cf.oem_url(cf.OEM_UPLOAD_ENDPOINT)} -> 302 -> "
        + " -> ".join(cf.OEM_REBOOT_UPGRADE_PATHS)
    )
    print(f"host readdress : {cf.readdress_command('<nic>')}   # fresh box answers on {cf.OPENWRT_LAN_ADDRESS}")
    print(f"stage2 stage   : {cf.stage_image_command(cf.OPENWRT_LAN_ADDRESS, cf.MAINLINE_IMAGE_FILENAME, '<local image>')}")
    print(f"stage2 verify  : {cf.sha256_on_device_command(cf.MAINLINE_IMAGE_FILENAME)}")
    print(f"stage2 flash   : {cf.sysupgrade_command(cf.remote_image_path(cf.MAINLINE_IMAGE_FILENAME))}")
    print(f"no-sftp note   : {cf.SFTP_UNSUPPORTED_NOTE}")
    print(f"capacity       : payload {cf.TOLLGATE_UNCOMPRESSED_BYTES} B uncompressed vs overlay free "
          f"{cf.OVERLAY_FREE_BYTES_MEASURED} B -> the DEFAULT payload does NOT fit. Use the "
          f"`{cf.TOLLGATE_COMPRESSED_VARIANT}` variant "
          f"({cf.TOLLGATE_COMPRESSED_PAYLOAD_BYTES} B; VERIFIED ON HARDWARE "
          f"{cf.TOLLGATE_COMPRESSED_VERIFIED_DATE}), or `install-tollgate --volatile` (tmpfs, "
          f"FALLBACK, NOT persistent). Full preflight: `cudy-flash.py capacity`")
    print(f"wifi enable    : {' && '.join(cf.WIFI_IFACE_ENABLE_COMMANDS)}")
    print(f"set password   : {cf.SET_ROOT_PASSWORD_COMMAND}   # {cf.SET_ROOT_PASSWORD_NOTE}")
    print(f"stage3 handoff : {cf.tollgate_install_handoff().describe()}")
    print()
    print("honesty: no hardware has been touched. Stages 1 and 2 were verified on real hardware")
    print(f"         {cf.HARDWARE_VERIFIED_DATE}; the volatile install is NOT hardware-verified.")
    return rc


# ---------------------------------------------------------------------------
# oem-upload — stage 1
# ---------------------------------------------------------------------------


def _login(session: HttpSession, base: str, password: str, *, verbose: bool) -> None:
    status, html = session.get(base + cf.OEM_LOGIN_PATH)
    if "password" not in html.lower():
        # already authenticated, or not the page we expect — the caller classifies
        if verbose:
            print(f"login : GET {cf.OEM_LOGIN_PATH} -> {status}; no password field (maybe already in)")
        return
    login_page = cf.classify_page(html, url=base + cf.OEM_LOGIN_PATH)
    if login_page.is_bootloader:
        raise SystemExit(
            "REFUSING: the vendor UI answered with what looks like a BOOTLOADER page "
            f"({login_page.describe()}). That is a different lane; nothing was uploaded."
        )
    status, after = session.post_form(
        base + cf.OEM_LOGIN_PATH, {"luci_username": cf.OEM_LOGIN_USERNAME, "password": password}
    )
    if verbose:
        print(f"login : POST {cf.OEM_LOGIN_PATH} -> {status}")
    if "password" in after.lower() and "input" in after.lower() and "logout" not in after.lower():
        raise SystemExit(
            "REFUSING: the CudyOS login was REFUSED (the login form came back). Check the password "
            f"env ({list(cf.PASSWORD_ENVS)}); the Wi-Fi key on the label is NOT the UI password."
        )


def _fetch_firmware_page(session: HttpSession, base: str, *, verbose: bool) -> tuple[str, str]:
    """Try the candidate firmware doors; return ``(url, html)`` of the first that answers."""
    for path in cf.OEM_FIRMWARE_PATHS:
        url = base + path
        try:
            status, html = session.get(url)
        except (OSError, urllib.error.URLError) as exc:
            if verbose:
                print(f"page  : GET {path} -> transport error {exc}")
            continue
        if verbose:
            print(f"page  : GET {path} -> {status} ({len(html)} bytes)")
        if status == 200 and html.strip():
            return url, html
    raise SystemExit(
        f"REFUSING: none of the candidate firmware doors answered on {base} "
        f"({list(cf.OEM_FIRMWARE_PATHS)}). Capture the box's real menu with `--dump-page` "
        "against the door you reached by hand, then pin the path. Nothing was uploaded."
    )


def cmd_oem_upload(args: argparse.Namespace) -> int:
    print("== stage 1: Cudy-signed transitional image via the vendor UI ==")
    print(f"note : {cf.TRANSITIONAL_VARIANT_NOTE}")
    _lock_note(args)

    # gate 0: the destructive switch (this destroys CudyOS)
    _require_switch()

    # gate 1: the hardware must be a v1.0
    verdict = cf.require_model_supported(
        cf.classify_model(args.model or ""), allow_unknown=args.assume_wr3000_v1
    )
    print(f"model : {verdict.describe()}")

    # gate 2: the image must be the vendored transitional build
    image = cf.resolve_transitional_image(args.image)
    problems = cf.verify_transitional_image(image)
    if problems:
        raise SystemExit("REFUSING: transitional image is not the pinned build: " + "; ".join(problems))
    print(f"image : {image} (sha256 {cf.TRANSITIONAL_IMAGE_SHA256}, {cf.TRANSITIONAL_IMAGE_SIZE} bytes)")

    if args.page_html:
        # offline rehearsal: classify a captured page and stop (no upload)
        html = Path(args.page_html).read_text(encoding="utf-8", errors="replace")
        page = cf.classify_page(html, url=args.url)
        print(f"page  : {page.describe()}")
        print(cf.evidence_summary(html, url=args.url))
        print(
            f"pinned: endpoint={cf.OEM_UPLOAD_ENDPOINT} file_field={cf.OEM_UPLOAD_FILE_FIELD!r} "
            f"accept={cf.OEM_UPLOAD_FILE_ACCEPT} (from the 2026-09-27 hardware run)"
        )
        cf.require_uploadable_page(page, allow_unknown=False)
        print("(offline rehearsal from --page-html: no network call, nothing uploaded)")
        return EXIT_OK

    base = cf.oem_url(host=args.url)
    password = _require_password()
    session = HttpSession()
    _login(session, base, password, verbose=True)
    url, html = _fetch_firmware_page(session, base, verbose=True)

    page = cf.classify_page(html, url=url)
    print(f"page  : {page.describe()}")
    print(cf.evidence_summary(html, url=url))
    print(f"note  : {cf.OEM_UPLOAD_FRAGMENT_NOTE}")
    print(f"flow  : {cf.OEM_UPLOAD_FLOW}")

    if args.dump_page:
        dump_dir = Path(args.dump_dir).expanduser()
        dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (dump_dir / f"cudy-firmware-page-{stamp}.html").write_text(html, encoding="utf-8")
        (dump_dir / f"cudy-firmware-page-{stamp}.evidence.txt").write_text(
            cf.evidence_summary(html, url=url) + "\n", encoding="utf-8"
        )
        form = cf.find_upload_form(html)
        print(f"dump  : wrote {dump_dir}/cudy-firmware-page-{stamp}.{{html,evidence.txt}}")
        if form is not None and form.file_field:
            print(f"dump  : page form action={form.action or url} file_field={form.file_field!r}")
        print(
            "dump  : pinned (2026-09-27 hardware) "
            f"endpoint={cf.OEM_UPLOAD_ENDPOINT} file_field={cf.OEM_UPLOAD_FILE_FIELD!r} "
            f"accept={cf.OEM_UPLOAD_FILE_ACCEPT}"
        )
        print("(dump mode: NOTHING was uploaded)")
        return EXIT_OK

    # gate 3: only a positively-classified vendor UI may be uploaded to
    cf.require_uploadable_page(page, allow_unknown=False)

    # the endpoint + file field are PINNED from hardware; --fw-action/--fw-file-field override
    action = args.fw_action or cf.OEM_UPLOAD_ENDPOINT
    field = args.fw_file_field or cf.OEM_UPLOAD_FILE_FIELD
    target = urllib.parse.urljoin(base, action)
    print(f"upload: POST {target} field={field!r} file={os.path.basename(image)}  (upload step)")
    cf.require_confirm(
        args.yes_i_mean_it,
        action=(
            "the OEM upload (this REPLACES CudyOS and reboots the box; afterwards it answers on "
            f"{cf.OPENWRT_LAN_ADDRESS} with Wi-Fi DISABLED)"
        ),
    )
    status, response = session.post_file(target, field, image)
    print(f"answer: HTTP {status} -> {cf.classify_upload_response(response).describe()}")
    if status != 200:
        raise SystemExit(
            f"FAILED: the upload POST answered HTTP {status} (expected 200, the verified "
            "first half). Nothing more was sent."
        )

    # step 2: the `Proceed` button that appears after the upload — its POST is the 302
    print(f"proceed: POST {target}  (the `{cf.OEM_UPLOAD_PROCEED_LABEL}` button)")
    proceed_status, proceed_body = session.post_form_noredirect(target, {})
    print(f"answer: HTTP {proceed_status}")
    outcome = cf.classify_stage1_sequence(status, proceed_status, message=proceed_body)
    violations = cf.stage1_sequence_violations(outcome)
    if violations:
        raise SystemExit("REFUSING/FAILED: " + "; ".join(violations))

    # step 3: the reboot applies the 302 drives (best-effort; the box reboots under us)
    for path in cf.OEM_REBOOT_UPGRADE_PATHS:
        reboot_status, _ = session.get(urllib.parse.urljoin(base, path))
        print(f"reboot: GET {path} -> {reboot_status}")

    print()
    print("[next] the box is rebooting onto the transitional OpenWrt.")
    print(f"       {cf.TRANSITIONAL_ROOT_PASSWORD_NOTE}")
    print(f"       re-address the host NIC: {cf.readdress_command('<nic>')}")
    print(f"       it answers on {cf.OPENWRT_LAN_ADDRESS}; {cf.WIFI_IFACE_DISABLED_NOTE}")
    print("       then run stage 2:  scripts/cudy-flash.py sysupgrade --yes-i-mean-it")
    return EXIT_OK


# ---------------------------------------------------------------------------
# sysupgrade — stage 2
# ---------------------------------------------------------------------------


def cmd_sysupgrade(args: argparse.Namespace) -> int:
    print("== stage 2: mainline OpenWrt sysupgrade ==")
    _lock_note(args)
    _require_switch()

    model = cf.require_model_supported(
        cf.classify_model(args.model or ""), allow_unknown=args.assume_wr3000_v1
    )
    print(f"model : {model.describe()}")

    image = cf.resolve_mainline_image(args.mainline_image)
    problems = cf.verify_mainline_image(image)
    if problems:
        raise SystemExit("REFUSING: mainline image is not the verified build: " + "; ".join(problems))
    print(f"image : {image}")

    host = args.host
    # 1. wallet gate — `sysupgrade -n` wipes /etc/tollgate, including real ecash
    out, wallet_rc = ssh(host, ff.WALLET_BALANCE_COMMAND, timeout=60)
    listing, _ = ssh(host, ff.ECASH_LISTING_COMMAND, timeout=30)
    state = ff.parse_probed_wallet_state(out, listing, balance_exit_code=wallet_rc)
    print(f"wallet: {state.summary()}")
    ff.flash_guard(state, allow_nonempty=args.allow_nonempty_wallet)

    if not ip.tcp_probe(host, 22, timeout=5):
        raise SystemExit(
            f"REFUSING: nothing answers on {host}:22 — is the box on OpenWrt "
            f"({cf.OPENWRT_LAN_ADDRESS}) with the host NIC re-addressed? Nothing was flashed."
        )

    # 2. stage over ssh stdin (no sftp-server), then verify ON DEVICE
    remote = cf.remote_image_path(os.path.basename(image))
    local_sha = ip.sha256_file(image)
    print(f"stage : {cf.SFTP_UNSUPPORTED_NOTE}")
    command = cf.stage_image_command(host, os.path.basename(image), image)
    print(f"stage : {command}")
    if not args.dry_run:
        pw = os.environ.get("TOLLGATE_SSH_PASSWORD") or os.environ.get("TOLLGATE_LUCI_PASSWORD") or ""
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=1800, env={**os.environ, "SSHPASS": pw}
        )
        if result.returncode != 0:
            raise SystemExit(
                f"staging failed ({result.returncode}): {(result.stderr or result.stdout).strip()[:300]}"
            )
    print(f"verify: {cf.sha256_on_device_command(remote)}")
    device_sha, _ = ssh(host, cf.sha256_on_device_command(remote), timeout=180)
    if device_sha.strip() != local_sha:
        raise SystemExit(
            f"REFUSING: staged image sha256 on device {device_sha.strip()!r} != local {local_sha!r}"
        )
    print(f"verify: on-device sha256 matches {local_sha}")

    # 3. the flash
    flash = cf.sysupgrade_command(remote)
    print(f"flash : {flash}   (config is NOT kept)")
    if args.dry_run:
        print("(dry-run: nothing was flashed)")
        return EXIT_OK
    cf.require_confirm(args.yes_i_mean_it, action=f"`{flash}` (sysupgrade -n)")
    ssh(host, flash, timeout=30)

    # 4. wait, then verify identity + freshness
    if not wait_for_ssh(host, timeout=args.timeout):
        raise SystemExit(
            f"the box did not come back on {host}:22 — recover with the vendor UI or the Cudy "
            "recovery-TFTP route. A fresh image uses 192.168.1.1 with an EMPTY root password."
        )
    identity, _ = ssh(host, cf.board_identity_command(), timeout=30)
    print(f"verify: {identity}")
    troubles = cf.post_mainline_identity_violations(identity)
    installed, _ = ssh(host, ip.package_version_command("apk"), timeout=30)
    listing, _ = ssh(host, f"ls {ff.TOLLGATE_DIR} 2>/dev/null", timeout=20)
    troubles += cf.no_tollgate_state_violations(installed, listing)
    if troubles:
        raise SystemExit("post-flash checks failed: " + "; ".join(troubles))
    print("[verify] mainline image confirmed (correct board, no tollgate-wrt, no /etc/tollgate state)")
    if args.readdress:
        print(
            "[host] re-address the NIC: "
            + cf.readdress_command(args.readdress)
            + f"   # the fresh image defaults to {cf.OPENWRT_LAN_ADDRESS}"
        )
    print()
    print("[next] stage 3 handoff:")
    print(f"       {cf.SET_ROOT_PASSWORD_NOTE}")
    print(f"       wifi: {' && '.join(cf.WIFI_IFACE_ENABLE_COMMANDS)}")
    print(f"       {cf.tollgate_install_handoff().describe()}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# install-tollgate — stage 3 handoff
# ---------------------------------------------------------------------------


def _probe_free_space(host: str) -> tuple[int, int]:
    """(overlay_free_bytes, tmpfs_free_bytes); falls back to the measured defaults."""
    overlay = cf.OVERLAY_FREE_BYTES_MEASURED
    tmpfs = cf.TMPFS_FREE_BYTES_MEASURED
    overlay_out, _ = ssh(host, cf.OVERLAY_FREE_COMMAND, timeout=20)
    probed = cf.parse_df_kb(overlay_out)
    if probed is not None:
        overlay = probed
        print(f"probe : overlay free (df -k /overlay) = {overlay} B")
    tmpfs_out, _ = ssh(host, cf.TMPFS_FREE_COMMAND, timeout=20)
    tprobed = cf.parse_df_kb(tmpfs_out)
    if tprobed is not None:
        tmpfs = tprobed
        print(f"probe : tmpfs free (df -k /tmp) = {tmpfs} B")
    return overlay, tmpfs


def cmd_capacity(args: argparse.Namespace) -> int:
    print("== flash-capacity preflight (read-only) ==")
    payload = args.payload_bytes if args.payload_bytes is not None else cf.TOLLGATE_UNCOMPRESSED_BYTES
    overlay_free = args.overlay_free_bytes
    tmpfs_free = args.tmpfs_free_bytes
    if args.probe:
        if ip.tcp_probe(args.host, 22, timeout=5):
            probed_overlay, probed_tmpfs = _probe_free_space(args.host)
            overlay_free = probed_overlay if overlay_free is None else overlay_free
            tmpfs_free = probed_tmpfs if tmpfs_free is None else tmpfs_free
        else:
            print(f"probe : nothing on {args.host}:22 — using the 2026-09-27 measured defaults")
    overlay_free = cf.OVERLAY_FRESH_FREE_BYTES if overlay_free is None else overlay_free
    tmpfs_free = cf.TMPFS_FREE_BYTES_MEASURED if tmpfs_free is None else tmpfs_free

    variant = cf.payload_variant_name(payload)
    if variant == cf.TOLLGATE_COMPRESSED_VARIANT:
        wrt_bytes, cli_bytes = (
            cf.TOLLGATE_COMPRESSED_BINARY_TOLLGATE_WRT_BYTES,
            cf.TOLLGATE_COMPRESSED_BINARY_TOLLGATE_BYTES,
        )
    else:
        wrt_bytes, cli_bytes = (
            cf.TOLLGATE_BINARY_TOLLGATE_WRT_BYTES,
            cf.TOLLGATE_BINARY_TOLLGATE_BYTES,
        )
    print(
        f"payload: {payload} B uncompressed (tollgate-wrt {wrt_bytes} B"
        f" + tollgate {cli_bytes} B + small parts {cf.TOLLGATE_SMALL_PARTS_BYTES} B);"
        f" package is {cf.TOLLGATE_PACKAGE_COMPRESSED_BYTES} B compressed (default build)"
    )
    print(f"variant: {variant} (from the {payload} B uncompressed payload)")
    print(
        f"flash  : firmware {cf.FLASH_TOTAL_BYTES} B (16 MB NOR) = kernel {cf.FLASH_KERNEL_BYTES}"
        f" + rootfs {cf.FLASH_ROOTFS_BYTES} + overlay {cf.OVERLAY_TOTAL_BYTES} "
        f"(free on a fresh box ~{cf.OVERLAY_FRESH_FREE_BYTES} B; {cf.OVERLAY_FREE_BYTES_MEASURED} B"
        f" was the residual after the failed default attempt)"
    )
    flash = cf.check_install_capacity(payload, available_bytes=overlay_free, mode=cf.MODE_FLASH)
    volatile = cf.check_install_capacity(payload, available_bytes=tmpfs_free, mode=cf.MODE_VOLATILE)
    print(f"flash  : {flash.describe()}")
    print(f"volatile: {volatile.describe()}")

    # the compressed variant is the FIRST choice on a 16 MB box: show whether IT fits
    compressed = cf.check_install_capacity(
        cf.TOLLGATE_COMPRESSED_PAYLOAD_BYTES, available_bytes=overlay_free, mode=cf.MODE_FLASH
    )
    print(
        f"compressed `{cf.TOLLGATE_COMPRESSED_VARIANT}` payload "
        f"{cf.TOLLGATE_COMPRESSED_PAYLOAD_BYTES} B vs overlay free {overlay_free} B -> "
        f"fits={compressed.fits}{'' if compressed.fits else ' (also too big here)'}"
    )
    print(f"         {cf.TOLLGATE_COMPRESSED_PROVENANCE_NOTE}")

    chosen = volatile if args.mode == cf.MODE_VOLATILE else flash
    rc = EXIT_OK
    for problem in cf.capacity_problems(chosen):
        print(f"        PROBLEM: {problem}")
        rc |= EXIT_CAPACITY
    if chosen.fits:
        print(f"OK     : mode={args.mode} fits ({payload} B <= {chosen.available_bytes} B)")
    if args.mode == cf.MODE_VOLATILE and volatile.fits:
        print()
        print(cf.volatile_install_plan(payload).describe())
    return rc


def cmd_install_tollgate(args: argparse.Namespace) -> int:
    print("== stage 3: TollGate install handoff (reuses the kit's existing install path) ==")
    _lock_note(args)
    _require_switch()
    handoff = cf.tollgate_install_handoff()
    print(f"order : {' -> '.join(handoff.order)}")
    print(f"owner : {handoff.describe()}")
    for prerequisite in handoff.prerequisites:
        print(f"pre   : {prerequisite}")

    host = args.host
    if not ip.tcp_probe(host, 22, timeout=5):
        raise SystemExit(f"REFUSING: nothing answers on {host}:22 — flash stage 2 first. Nothing was changed.")

    # --- flash-capacity preflight (the ENOSPC wall the hardware run hit) -------------
    mode = cf.MODE_VOLATILE if args.volatile else cf.MODE_FLASH
    payload = args.payload_bytes if args.payload_bytes is not None else cf.TOLLGATE_UNCOMPRESSED_BYTES
    overlay_free, tmpfs_free = _probe_free_space(host)
    available = tmpfs_free if args.volatile else overlay_free
    verdict = cf.check_install_capacity(payload, available_bytes=available, mode=mode)
    print(f"capa  : mode={mode} payload={payload} B available={available} B fits={verdict.fits}")
    print(
        f"variant: payload={cf.payload_variant_name(payload)}; the `"
        f"{cf.TOLLGATE_COMPRESSED_VARIANT}` variant ({cf.TOLLGATE_COMPRESSED_PAYLOAD_BYTES} B) is "
        f"the FIRST choice on a 16 MB box — {cf.TOLLGATE_COMPRESSED_PROVENANCE_NOTE}"
    )
    problems = cf.capacity_problems(verdict)
    if problems:
        for problem in problems:
            print(f"        PROBLEM: {problem}")
        raise SystemExit(
            "REFUSING TO INSTALL: the payload does not fit; nothing was changed. "
            f"({mode} needs {payload} B, has {available} B)."
        )

    if args.volatile:
        plan = cf.volatile_install_plan(payload)
        print(f"volat : {plan.describe()}")
        for warning in plan.warnings:
            print(f"WARNING: {warning}")
        # a volatile install is NEVER persistent — refuse to ever say otherwise
        cf.volatile_persistence_violations(claim_persistent=False)
        package = args.package
        remote_package = cf.remote_image_path(os.path.basename(package)) if package else "/tmp/<package>.apk"
        print("volatile install steps (idempotent):")
        for command in plan.commands(remote_package):
            print(f"       {command}")
        if not (package and args.yes_i_mean_it and not args.dry_run):
            print()
            print("(pass --package <local .apk> AND --yes-i-mean-it to run these now; --dry-run")
            print(" prints them only.  IMPLEMENTED, NOT YET HARDWARE-VERIFIED.)")
            return EXIT_OK
        command = cf.stage_image_command(host, os.path.basename(package), package)
        print(f"stage : {command}")
        pw = _ssh_password()
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=1800,
            env={**os.environ, "SSHPASS": pw},
        )
        if result.returncode != 0:
            raise SystemExit(f"staging the package failed ({result.returncode})")
        for step in plan.commands(remote_package):
            out, rc = ssh(host, step, timeout=300)
            print(f"run   : {step} -> rc={rc}")
            if rc != 0:
                raise SystemExit(f"volatile install step failed: {step}\n{out.strip()[:200]}")
        print()
        print("[volatile] the big binaries are symlinked from " + cf.VOLATILE_TMP_DIR + ".")
        print("           " + cf.VOLATILE_INSTALL_NOTE)
        return EXIT_OK

    if not args.dry_run:
        # 1. the fresh box has an EMPTY root password: SET the lab password
        lab_password = os.environ.get(cf.ROOT_PASSWORD_ENV, "")
        if not lab_password:
            raise SystemExit(
                f"REFUSING: {cf.ROOT_PASSWORD_ENV} is unset. A freshly flashed OpenWrt has an EMPTY "
                "root password, and the lab password must be SET as part of the handover. "
                f"Re-run with {cf.ROOT_PASSWORD_ENV}=<lab password>."
            )
        # feed the password on the remote command's stdin: it never reaches argv
        out, rc = ssh_stdin(host, cf.SET_ROOT_PASSWORD_COMMAND, f"{lab_password}\n{lab_password}\n", timeout=60)
        print(f"setup : root password set (rc={rc})")
        if rc != 0:
            raise SystemExit(f"could not set the root password: {out.strip()[:200]}")

        # 2. a fresh image boots with the AP wifi-iface sections disabled
        if args.enable_wifi:
            for command in cf.WIFI_IFACE_ENABLE_COMMANDS:
                ssh(host, command, timeout=30)
            ubus, _ = ssh(host, cf.WIFI_VERIFY_COMMAND, timeout=30)
            violations = cf.wifi_iface_violations(ubus)
            print(f"setup : wireless ifaces = {cf.count_wifi_ifaces(ubus)}")
            if violations:
                print("WARNING: " + "; ".join(violations))

    print()
    print("run the existing install path (this lane does not reimplement it):")
    for command in handoff.commands():
        print(f"       {command}")
    if args.run_install_path:
        if args.dry_run:
            raise SystemExit("--run-install-path needs a real run (drop --dry-run)")
        script = PROJECT_ROOT / "scripts" / "install-path-e2e.py"
        print(f"exec  : {script} --flash-and-run --host {host}")
        result = subprocess.run(
            [sys.executable, str(script), "--flash-and-run", "--host", host],
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        )
        return result.returncode
    print()
    print("(handover prerequisites done; the install path itself runs under its own gates —")
    print(" rerun with --run-install-path to shell out to it now, or run the make target above;")
    print(" if the flash cannot hold the payload, prefer the `"
          + cf.TOLLGATE_COMPRESSED_VARIANT + "` variant (VERIFIED ON HARDWARE "
          + cf.TOLLGATE_COMPRESSED_VERIFIED_DATE + "); `--volatile` is the fallback — NOT persistent)")
    return EXIT_OK


# ---------------------------------------------------------------------------
# verify — read-only post-install ladder
# ---------------------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    print("== verify: post-install ladder (read-only) ==")
    host = args.host
    rc = EXIT_OK
    if not ip.tcp_probe(host, 22, timeout=5):
        print(f"ssh   : nothing on {host}:22 — cannot verify (stage 2 not done?)")
        return EXIT_REMOTE
    identity, _ = ssh(host, cf.board_identity_command(), timeout=30)
    print(f"board : {identity}")
    troubles = cf.post_mainline_identity_violations(identity) if args.expect_mainline else []
    for trouble in troubles:
        print(f"        PROBLEM: {trouble}")
        rc |= EXIT_REMOTE

    ubus, _ = ssh(host, cf.WIFI_VERIFY_COMMAND, timeout=30)
    print(f"wifi  : wireless ifaces = {cf.count_wifi_ifaces(ubus)}")
    for violation in cf.wifi_iface_violations(ubus):
        print(f"        PROBLEM: {violation}")
        rc |= EXIT_REMOTE

    installed, _ = ssh(host, ip.package_version_command("apk"), timeout=30)
    print(f"pkg   : {installed.strip()[:160] or '(no tollgate-wrt)'}")

    if args.api:
        text, api_rc = _fetch_api(args.api)
        print(f"api   : {text.strip()[:160]!r} (rc={api_rc})")
        for violation in cf.module_identity_violations(text):
            print(f"        PROBLEM: {violation}")
            rc |= EXIT_REMOTE
    print()
    print("honesty: this ladder checks the KIT's expectations; it is not a hardware-pass claim")
    print("         (see docs/cudy-wr3000-flashing.md 'NOT YET VERIFIED ON HARDWARE').")
    return rc


def _fetch_api(url: str) -> tuple[str, int]:
    session = HttpSession(timeout=6.0)
    try:
        status, text = session.get(url)
    except (OSError, urllib.error.URLError) as exc:
        return f"unreachable: {exc}", 1
    return text, 0 if status == 200 else 1


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, *, model: bool = True) -> None:
        p.add_argument("--image", default=os.environ.get("CUDY_TRANSITIONAL_IMAGE"))
        p.add_argument("--mainline-image", dest="mainline_image", default=os.environ.get("CUDY_MAINLINE_IMAGE"))
        p.add_argument("--host", default=os.environ.get("ROUTER_IP") or cf.OPENWRT_LAN_ADDRESS)
        p.add_argument("--url", default=None, help=f"vendor UI base URL (env {cf.URL_ENV})")
        if model:
            p.add_argument("--model", default=os.environ.get("CUDY_MODEL"),
                           help="label/model text read off the box (e.g. 'WR3000 V1.0')")
            p.add_argument("--assume-wr3000-v1", dest="assume_wr3000_v1", action="store_true",
                           help="proceed when the label is not identifiable (NEVER overrides a 2.0)")
        p.add_argument("--page-html", dest="page_html",
                       help="classify a CAPTURED page instead of hitting the network (offline)")

    check = sub.add_parser("check", help="read-only preconditions (offline-capable)")
    common(check)

    upload = sub.add_parser("oem-upload", help="STAGE 1: vendor-UI upload of the Cudy-signed image")
    common(upload)
    upload.add_argument("--dump-page", dest="dump_page", action="store_true",
                        help="log the real page HTML/selectors and upload NOTHING (evidence-first)")
    upload.add_argument("--dump-dir", dest="dump_dir", default=os.environ.get("CUDY_DUMP_DIR", "~/cudy-flash/dumps"))
    upload.add_argument("--fw-action", dest="fw_action", default=os.environ.get("CUDY_FW_ACTION"),
                        help="upload form action, once pinned from --dump-page evidence")
    upload.add_argument("--fw-file-field", dest="fw_file_field", default=os.environ.get("CUDY_FW_FILE_FIELD"),
                        help="file input name, once pinned from --dump-page evidence")
    upload.add_argument("--yes-i-mean-it", dest="yes_i_mean_it", action="store_true")

    sysup = sub.add_parser("sysupgrade", help="STAGE 2: mainline sysupgrade -n over ssh")
    common(sysup)
    sysup.add_argument("--yes-i-mean-it", dest="yes_i_mean_it", action="store_true")
    sysup.add_argument("--dry-run", dest="dry_run", action="store_true",
                       help="stage + verify on device but do NOT flash")
    sysup.add_argument("--allow-nonempty-wallet", dest="allow_nonempty_wallet", action="store_true")
    sysup.add_argument("--timeout", type=int, default=300)
    sysup.add_argument("--readdress", default=None, help="host NIC to re-address after the flash")

    cap = sub.add_parser("capacity", help="read-only flash-capacity preflight (payload vs free overlay)")
    common(cap, model=False)
    cap.add_argument("--payload-bytes", dest="payload_bytes", type=int, default=None,
                     help="uncompressed payload size (default: the measured tollgate-wrt payload)")
    cap.add_argument("--overlay-free-bytes", dest="overlay_free_bytes", type=int, default=None)
    cap.add_argument("--tmpfs-free-bytes", dest="tmpfs_free_bytes", type=int, default=None)
    cap.add_argument("--mode", choices=list(cf.INSTALL_MODES), default=cf.MODE_FLASH,
                     help="which mode's fit decides the exit bit")
    cap.add_argument("--probe", dest="probe", action="store_true",
                     help="probe the box over ssh for its real free space (falls back to measured)")

    install = sub.add_parser("install-tollgate", help="STAGE 3: handover into the existing install path")
    common(install, model=False)
    install.add_argument("--dry-run", dest="dry_run", action="store_true")
    install.add_argument("--enable-wifi", dest="enable_wifi", action="store_true", default=True)
    install.add_argument("--no-enable-wifi", dest="enable_wifi", action="store_false")
    install.add_argument("--volatile", dest="volatile", action="store_true",
                         help="install the big binaries into tmpfs (LOST ON REBOOT, never persistent)")
    install.add_argument("--payload-bytes", dest="payload_bytes", type=int, default=None,
                         help="uncompressed payload size for the capacity preflight")
    install.add_argument("--package", default=os.environ.get("CUDY_TOLLGATE_PACKAGE"),
                         help="local tollgate-wrt .apk to use for a --volatile install")
    install.add_argument("--yes-i-mean-it", dest="yes_i_mean_it", action="store_true")
    install.add_argument("--run-install-path", dest="run_install_path", action="store_true",
                         help="shell out to scripts/install-path-e2e.py --flash-and-run")

    verify = sub.add_parser("verify", help="read-only post-install ladder")
    common(verify, model=False)
    verify.add_argument("--api", default=os.environ.get("CUDY_API_URL") or cf.MODULE_API_URL.format(host=cf.OPENWRT_LAN_ADDRESS))
    verify.add_argument("--expect-mainline", dest="expect_mainline", action="store_true", default=True)
    verify.add_argument("--no-expect-mainline", dest="expect_mainline", action="store_false")
    return parser


COMMANDS = {
    "check": cmd_check,
    "oem-upload": cmd_oem_upload,
    "sysupgrade": cmd_sysupgrade,
    "capacity": cmd_capacity,
    "install-tollgate": cmd_install_tollgate,
    "verify": cmd_verify,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except cf.ModelRefused as exc:
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_MODEL
    except cf.PageUnclassified as exc:
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_PAGE
    except ff.FlashRefused as exc:
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_SWITCH_OFF


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
