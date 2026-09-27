"""Unit tests for :mod:`lib.cudy_flash` and the ``scripts/cudy-flash.py`` CLI.

Everything here runs offline: no network, no SSH, no router, no credential
literal.  The interesting cases are the REFUSALS, because they are what stands
between an operator and a bricked WR3000:

* a WR3000 **2.0** box must be refused (the OpenWrt TOH page says its CPU is not
  supported) — and an unidentifiable label must fail closed too;
* a **sha256 / size** mismatch must refuse, and a ``<image>.sha256`` sidecar that
  disagrees with the pinned hash must refuse as well;
* the sysupgrade stage must refuse without ``--yes-i-mean-it``, and the wallet
  gate must fail closed when the probe did not answer (unknown != empty);
* the vendor upload must refuse unless the page is POSITIVELY the CudyOS UI —
  a bootloader page and an already-running-OpenWrt page both refuse;
* staging must use an ssh stdin redirect: these builds have no sftp-server.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from lib import cudy_flash as cf
from lib import fresh_flash as ff

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLI = PROJECT_ROOT / "scripts" / "cudy-flash.py"

#: the operator's real local images (verified below when this host has them)
REAL_TRANSITIONAL = Path(os.path.expanduser(cf.LOCAL_TRANSITIONAL_CANDIDATES[0]))
REAL_MAINLINE = Path(os.path.expanduser(cf.LOCAL_MAINLINE_CANDIDATES[0]))


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def _vendor_page(tmp_path: Path) -> Path:
    """A captured vendor-UI page written to disk for the offline rehearsal."""
    page = tmp_path / "vendor.html"
    page.write_text(VENDOR_HTML, encoding="utf-8")
    return page


def _transitional_copy(tmp_path: Path, payload: bytes = b"cudy transitional") -> Path:
    return _write(tmp_path / cf.TRANSITIONAL_IMAGE_FILENAME, payload)


def _mainline_copy(tmp_path: Path, payload: bytes = b"mainline image") -> Path:
    return _write(tmp_path / cf.MAINLINE_IMAGE_FILENAME, payload)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# the hard model guard: WR3000 v1.0 only, 2.0 REFUSED
# ---------------------------------------------------------------------------


def test_classify_model_accepts_a_v1_label():
    verdict = cf.classify_model("Cudy WR3000 V1.0")
    assert verdict.supported and verdict.variant == "1.0"
    assert "WR3000 V1.0" in " ".join(verdict.evidence)
    cf.require_model_supported(verdict)  # must not raise

    # "R31" alone is the documented v1 second marker
    assert cf.classify_model("Hardware: R31").supported


@pytest.mark.parametrize(
    "label",
    [
        "WR3000 2.0",
        "Model WR3000 V2.0",
        "Cudy WR3000 V2.0 (R32)",
        "WR3000V2",
        "WR3000 2",
        "cudy wr3000 v2.0",
    ],
)
def test_classify_model_refuses_the_unsupported_2_0_box(label):
    verdict = cf.classify_model(label)
    assert verdict.variant == "2.0" and not verdict.supported
    with pytest.raises(cf.ModelRefused) as excinfo:
        cf.require_model_supported(verdict)
    message = str(excinfo.value)
    assert "REFUSING TO FLASH" in message
    assert "2.0" in message
    assert "not" in message.lower() and "supported" in message.lower()


def test_require_model_supported_never_lets_a_2_0_through():
    """The explicit override clears an UNKNOWN label — never a 2.0 one."""
    verdict = cf.classify_model("WR3000 V2.0")
    with pytest.raises(cf.ModelRefused):
        cf.require_model_supported(verdict, allow_unknown=True)


def test_an_unidentifiable_label_fails_closed_but_can_be_overridden():
    verdict = cf.classify_model("some router")
    assert verdict.variant == "unknown"
    with pytest.raises(cf.ModelRefused) as excinfo:
        cf.require_model_supported(verdict)
    assert cf.ModelVerdict().describe().startswith("variant=unknown")
    assert "could not be positively identified" in str(excinfo.value)
    # ... and the operator can override an unknown verdict explicitly
    assert cf.require_model_supported(verdict, allow_unknown=True) is verdict


def test_classify_model_on_empty_input_is_unknown():
    assert cf.classify_model("").variant == "unknown"
    assert cf.classify_model(None).variant == "unknown"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# image identity: filename, size, sha256, sidecar
# ---------------------------------------------------------------------------


def test_mainline_filename_is_derived_from_the_board_token_not_the_mt3000():
    assert cf.MAINLINE_IMAGE_FILENAME == "openwrt-25.12.5-mediatek-filogic-cudy_wr3000-v1-squashfs-sysupgrade.bin"
    assert cf.mainline_image_filename("24.10.8", "mediatek-filogic", "cudy_wr3000-v1").startswith("openwrt-24.10.8-")
    # it must NOT be the MT3000 filename lib/fresh_flash.py pins for the other box
    assert cf.MAINLINE_IMAGE_FILENAME != ff.BENCH_IMAGE_FILENAME
    assert "gl-mt3000" not in cf.MAINLINE_IMAGE_FILENAME


def test_validate_transitional_filename_requires_the_exact_vendored_name():
    assert cf.validate_transitional_filename(cf.TRANSITIONAL_IMAGE_FILENAME) == []
    for wrong in (
        "openwrt-mediatek-filogic-cudy_wr3000-v1-sysupgrade(1).bin",
        "openwrt-25.12.5-mediatek-filogic-cudy_wr3000-v1-squashfs-sysupgrade.bin",
        "cudy3000.bin",
        "",
    ):
        problems = cf.validate_transitional_filename(wrong)
        assert problems, f"{wrong!r} should have been rejected"
        assert "recovery" in problems[0] or "is not" in problems[0]


def test_validate_mainline_filename_rejects_the_mt3000_and_the_wrong_board():
    assert cf.validate_mainline_filename(cf.MAINLINE_IMAGE_FILENAME) == []
    problems = cf.validate_mainline_filename(ff.BENCH_IMAGE_FILENAME)
    assert problems and any("MT3000" in p for p in problems)
    wrong_board = cf.mainline_image_filename("25.12.5", "mediatek-filogic", "cudy_wr3000-v2")
    assert cf.validate_mainline_filename(wrong_board)
    assert cf.validate_mainline_filename("openwrt-25.12.5-mediatek-filogic-cudy_wr3000-v1-rootfs.tar.gz")


def test_verify_image_refuses_a_sha256_mismatch(tmp_path):
    image = _mainline_copy(tmp_path, b"not the real image")
    problems = cf.verify_cudy_image(image, expected_sha256=cf.MAINLINE_IMAGE_SHA256)
    assert len(problems) == 1 and "sha256" in problems[0] and "refusing to flash" in problems[0]


def test_verify_image_refuses_a_size_mismatch(tmp_path):
    image = _mainline_copy(tmp_path, b"short")
    problems = cf.verify_cudy_image(image, expected_sha256=_sha(image), expected_size=cf.MAINLINE_IMAGE_SIZE)
    assert len(problems) == 1 and "size" in problems[0]


def test_verify_image_accepts_the_real_bytes_when_the_hash_is_supplied(tmp_path):
    image = _transitional_copy(tmp_path, b"exactly these bytes")
    assert cf.verify_cudy_image(
        image, expected_sha256=_sha(image), filename_validator=cf.validate_transitional_filename
    ) == []
    mainline = _mainline_copy(tmp_path, b"exactly these bytes")
    assert cf.verify_cudy_image(
        mainline, expected_sha256=_sha(mainline), filename_validator=cf.validate_mainline_filename
    ) == []


def test_verify_image_reports_a_missing_file(tmp_path):
    assert "does not exist" in cf.verify_cudy_image(str(tmp_path / "nope.bin"), expected_sha256="0" * 64)[0]


def test_verify_transitional_and_mainline_pin_both_real_hashes():
    """The two hashes the docs publish are the two the code pins."""
    assert cf.TRANSITIONAL_IMAGE_SHA256 == "8ee579d1b970488ee06f47964ac27ef88bc627b563cea00b88f1cb3a2917ec64"
    assert cf.MAINLINE_IMAGE_SHA256 == "be876cf5335ab757874cd19f806b01f2271d8c20a1a0e68f1680019346c4408a"
    assert cf.TRANSITIONAL_IMAGE_SIZE == 9964331
    assert cf.MAINLINE_IMAGE_SIZE == 9699606


def test_sha256_sidecar_is_honoured_and_a_conflict_refuses(tmp_path):
    image = _transitional_copy(tmp_path, b"payload")
    # no sidecar -> no interference
    assert cf.read_sha256_sidecar(str(image)) is None
    assert cf.verify_cudy_image(image, expected_sha256=_sha(image)) == []

    # an agreeing sidecar (sha256sum-style row) is accepted
    sidecar = Path(str(image) + ".sha256")
    sidecar.write_text(f"{_sha(image)}  {image.name}\n", encoding="utf-8")
    assert cf.read_sha256_sidecar(str(image)) == _sha(image)
    assert cf.verify_cudy_image(image, expected_sha256=_sha(image)) == []

    # a DISAGREEING sidecar refuses rather than silently picking one
    sidecar.write_text("a" * 64 + f"  {image.name}\n", encoding="utf-8")
    problems = cf.verify_cudy_image(image, expected_sha256=_sha(image))
    assert len(problems) == 1 and "disagrees" in problems[0]

    # a bare-hex sidecar is parsed too
    sidecar.write_text(_sha(image) + "\n", encoding="utf-8")
    assert cf.read_sha256_sidecar(str(image)) == _sha(image)
    sidecar.write_text("not a hash at all\n", encoding="utf-8")
    assert cf.read_sha256_sidecar(str(image)) is None


def test_resolve_prefers_the_explicit_path_then_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "LOCAL_TRANSITIONAL_CANDIDATES", ())
    monkeypatch.setattr(cf, "LOCAL_MAINLINE_CANDIDATES", ())
    with pytest.raises(ff.ImageInvalid):
        cf.resolve_transitional_image(str(tmp_path / "nope.bin"))
    with pytest.raises(ff.ImageInvalid):
        cf.resolve_mainline_image(str(tmp_path / "nope.bin"))
    local = _mainline_copy(tmp_path)
    monkeypatch.setattr(cf, "LOCAL_MAINLINE_CANDIDATES", (str(local),))
    assert cf.resolve_mainline_image() == str(local)
    assert cf.resolve_mainline_image(str(local)) == str(local)


def test_expected_sha_lookup_reuses_the_openwrt_sums_parser():
    sums = "f" * 64 + " *" + cf.MAINLINE_IMAGE_FILENAME + "\n"
    assert cf.expected_sha_from_upstream_sums(sums, cf.MAINLINE_IMAGE_FILENAME) == "f" * 64
    assert cf.expected_sha_from_upstream_sums("", cf.MAINLINE_IMAGE_FILENAME) is None


# ---------------------------------------------------------------------------
# wallet gate (fail closed) — reused from lib.fresh_flash
# ---------------------------------------------------------------------------


def test_wallet_probe_failure_is_a_blocker_never_a_pass(monkeypatch):
    monkeypatch.setenv(ff.FLASH_ENABLE_ENV, "true")
    unprobed = ff.parse_probed_wallet_state("", "", balance_exit_code=255)
    assert not unprobed.probed
    wallet_problems = ff.flash_preconditions(unprobed)
    assert len(wallet_problems) == 1 and "could NOT be read" in wallet_problems[0]
    blockers = cf.preconditions(stage="sysupgrade", enable=True, wallet_problems=wallet_problems)
    assert blockers == wallet_problems
    # a non-empty wallet is refused with the drain command
    money = ff.parse_wallet_state('{"total":2100}', "/etc/tollgate/ecash/token-1\n")
    assert not money.empty
    with pytest.raises(ff.FlashRefused) as excinfo:
        ff.flash_guard(money, allow_nonempty=False)
    assert ff.DRAIN_COMMAND in str(excinfo.value)


def test_preconditions_reports_every_blocker_at_once(monkeypatch):
    monkeypatch.delenv(ff.FLASH_ENABLE_ENV, raising=False)
    blockers = cf.preconditions(
        stage="oem-upload",
        enable=False,
        model=cf.classify_model("WR3000 2.0"),
        image_problems=["image sha256 nope"],
        page_problems=["page unknown"],
    )
    assert len(blockers) == 4, blockers
    assert any(ff.FLASH_ENABLE_ENV in b for b in blockers)
    assert any("v1.0" in b for b in blockers)
    assert any("sha256" in b for b in blockers)
    assert any("page unknown" in b for b in blockers)


def test_preconditions_is_clean_with_the_switch_on_and_a_v1_box(monkeypatch):
    monkeypatch.setenv(ff.FLASH_ENABLE_ENV, "true")
    assert cf.preconditions(stage="oem-upload", enable=True, model=cf.classify_model("WR3000 V1.0")) == []


# ---------------------------------------------------------------------------
# missing-confirm refusal
# ---------------------------------------------------------------------------


def test_a_destructive_stage_refuses_without_the_confirm_flag():
    with pytest.raises(ff.FlashRefused) as excinfo:
        cf.require_confirm(False, action="the OEM upload")
    message = str(excinfo.value)
    assert cf.CONFIRM_FLAG in message
    assert ff.FLASH_ENABLE_ENV in message
    cf.require_confirm(True, action="anything")  # explicit confirmation goes through


# ---------------------------------------------------------------------------
# page classification (evidence-first, fail closed)
# ---------------------------------------------------------------------------

VENDOR_HTML = """
<html><head><title>Cudy</title></head><body>
<form method="post" action="/cgi-bin/luci/"><input type="hidden" name="luci_username=admin">
<input type="password" name="password"><input type="submit" value="Login"></form>
<ul><li><a href="/cgi-bin/luci/admin/wizard">Quick Setup</a></li>
<li><a href="/cgi-bin/luci/admin/panel">Advanced Settings</a></li>
<li><a href="/cgi-bin/luci/admin/tools">Diagnostic Tools</a></li></ul>
</body></html>
"""

FIRMWARE_HTML = """
<html><body><h2>Firmware Upgrade</h2>
<form method="post" action="/cgi-bin/luci/admin/panel/firmware" enctype="multipart/form-data">
  <input type="file" name="image" id="fw">
  <input type="submit" name="flash" value="Upgrade">
</form>
<p>Advanced Settings</p><p>Diagnostic Tools</p>
</body></html>
"""

BOOTLOADER_HTML = """
<html><body><h1>System Recovery</h1>
<p>U-Boot recovery: upload a firmware image or use TFTP.</p>
<form method="post" action="/cgi-bin/recovery" enctype="multipart/form-data">
<input type="file" name="firmware"><input type="submit" value="Upload"></form>
</body></html>
"""

OPENWRT_HTML = """
<html><body><h1>OpenWrt 25.12.5</h1>
<p>Powered by OpenWrt. /etc/openwrt_release</p>
<p>Status Overview: kernel version, release</p></body></html>
"""


def test_classify_page_positive_vendor_ui():
    verdict = cf.classify_page(VENDOR_HTML, url=cf.oem_url(cf.OEM_LOGIN_PATH))
    assert verdict.is_vendor_ui and verdict.upload_permitted
    assert "quick setup" in " ".join(verdict.evidence)
    cf.require_uploadable_page(verdict)  # must not raise


def test_classify_page_bootloader_is_never_an_upload_target():
    verdict = cf.classify_page(BOOTLOADER_HTML, url="http://192.168.1.1/")
    assert verdict.is_bootloader
    with pytest.raises(cf.PageUnclassified) as excinfo:
        cf.require_uploadable_page(verdict)
    assert "BOOTLOADER" in str(excinfo.value)


def test_classify_page_running_openwrt_says_stage_1_is_already_done():
    verdict = cf.classify_page(OPENWRT_HTML, url="http://192.168.1.1/")
    assert verdict.is_running_openwrt
    with pytest.raises(cf.PageUnclassified) as excinfo:
        cf.require_uploadable_page(verdict)
    assert "already running OpenWrt" in str(excinfo.value)


def test_classify_page_unknown_fails_closed_and_is_overridable():
    verdict = cf.classify_page("<html><body>hello</body></html>")
    assert verdict.kind == "unknown"
    with pytest.raises(cf.PageUnclassified) as excinfo:
        cf.require_uploadable_page(verdict)
    assert "could not be positively classified" in str(excinfo.value)
    assert cf.require_uploadable_page(verdict, allow_unknown=True) is verdict


def test_a_vendor_page_that_also_mentions_openwrt_is_still_the_vendor_ui():
    """CudyOS is LuCI-derived and may name OpenWrt: vendor markers must win."""
    html = VENDOR_HTML + "<p>OpenWrt 25.12.5 powered by OpenWrt</p>"
    assert cf.classify_page(html).is_vendor_ui


# ---------------------------------------------------------------------------
# parsing the vendor UI page + its upload response
# ---------------------------------------------------------------------------


def test_parse_upload_forms_finds_the_firmware_form_and_its_file_field():
    form = cf.find_upload_form(FIRMWARE_HTML)
    assert form is not None and form.looks_like_upload
    assert form.action == "/cgi-bin/luci/admin/panel/firmware"
    assert form.method == "post"
    assert form.enctype.startswith("multipart/")
    assert form.file_field == "image"
    assert "flash" in form.submit_fields
    assert cf.upload_file_field(FIRMWARE_HTML) == "image"


def test_parse_upload_forms_is_empty_when_a_page_has_no_file_input():
    assert cf.find_upload_form(VENDOR_HTML) is None
    assert cf.upload_file_field(VENDOR_HTML) == ""
    assert all(not form.looks_like_upload for form in cf.parse_upload_forms(VENDOR_HTML))


def test_evidence_summary_records_the_facts_a_live_run_must_pin():
    summary = cf.evidence_summary(FIRMWARE_HTML, url="http://192.168.10.1/cgi-bin/luci/admin/panel")
    assert "vendor-ui" in summary
    assert "file-field     : image" in summary
    assert "action=/cgi-bin/luci/admin/panel/firmware" in summary
    # and it never claims to have uploaded anything
    assert "upload" in summary.lower()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Firmware upgrade in progress. Do not power off. Rebooting...", "accepted"),
        ("Invalid firmware image: signature verification failed", "refused"),
        ("The image signature is not valid", "refused"),
        ("", "unknown"),
        ("<html><body>nothing useful</body></html>", "unknown"),
    ],
)
def test_classify_upload_response(body, expected):
    outcome = cf.classify_upload_response(body)
    assert outcome.kind == expected


def test_a_page_that_says_both_is_a_refusal():
    """A refusal must win: 'invalid ... try again' is not a pass."""
    outcome = cf.classify_upload_response("Invalid image. Please check the image and try again.")
    assert outcome.kind == "refused"
    violations = cf.upload_response_violations(outcome)
    assert violations and "REFUSED" in violations[0]
    assert cf.TRANSITIONAL_IMAGE_FILENAME in violations[0]


def test_an_unknown_upload_answer_is_never_reported_as_a_pass():
    violations = cf.upload_response_violations(cf.classify_upload_response(""))
    assert violations and "unknown" in violations[0]
    assert "NOT a pass" in violations[0]
    assert cf.upload_response_violations(cf.classify_upload_response("rebooting")) == []


# ---------------------------------------------------------------------------
# stage-2 transport: NO sftp-server
# ---------------------------------------------------------------------------


def test_staging_uses_an_ssh_stdin_redirect_and_never_scp_or_sftp():
    command = cf.stage_image_command("192.168.1.1", "img.bin", "/local/img.bin")
    assert "sshpass -e ssh" in command
    assert "'cat > /tmp/img.bin'" in command
    assert "< /local/img.bin" in command
    assert "scp" not in command and "sftp" not in command
    assert "no sftp-server" in cf.SFTP_UNSUPPORTED_NOTE
    assert "scp" in cf.SFTP_UNSUPPORTED_NOTE and "FAIL" in cf.SFTP_UNSUPPORTED_NOTE


def test_flash_command_builders_wipe_config_and_verify_on_device():
    assert cf.sysupgrade_command("/tmp/img.bin") == "sysupgrade -n /tmp/img.bin"
    assert cf.sysupgrade_command("/tmp/img.bin", keep_config=True) == "sysupgrade /tmp/img.bin"
    assert cf.remote_image_path(cf.MAINLINE_IMAGE_FILENAME) == "/tmp/" + cf.MAINLINE_IMAGE_FILENAME
    assert cf.sha256_on_device_command("img.bin") == "sha256sum /tmp/img.bin | cut -d' ' -f1"
    assert "board_name" in cf.board_identity_command()
    assert "192.168.1.200/24" in cf.readdress_command("enp0s31f6")


# ---------------------------------------------------------------------------
# post-flash identity + freshness
# ---------------------------------------------------------------------------


def test_post_transitional_identity_accepts_the_cudy_snapshot():
    output = f"{cf.BOARD_NAME_TOKEN}\nOpenWrt SNAPSHOT r22906-c9cb6411c1|{cf.ARCH}"
    assert cf.post_transitional_identity_violations(output) == []


def test_post_transitional_identity_flags_a_wrong_board_or_a_release_build():
    problems = cf.post_transitional_identity_violations("glinet,gl-mt3000\nOpenWrt 25.12.5|mips_24kc")
    assert len(problems) == 3
    assert any(cf.BOARD_NAME_TOKEN in p for p in problems)
    assert any("SNAPSHOT" in p for p in problems)
    assert any("arch" in p for p in problems)


def test_post_mainline_identity_accepts_the_release_and_rejects_a_stale_snapshot():
    good = f"{cf.BOARD_NAME_TOKEN}\nOpenWrt {cf.MAINLINE_RELEASE} r33051-abc|{cf.ARCH}"
    assert cf.post_mainline_identity_violations(good) == []
    stale = f"{cf.BOARD_NAME_TOKEN}\nOpenWrt SNAPSHOT r22906-c9cb6411c1|{cf.ARCH}"
    problems = cf.post_mainline_identity_violations(stale)
    assert any("stage 2 did not land" in p for p in problems)
    wrong = "glinet,gl-mt3000\nOpenWrt 24.10.1|mips_24kc"
    assert len(cf.post_mainline_identity_violations(wrong)) == 3  # board, release, arch


def test_no_tollgate_state_violations_is_reused_from_fresh_flash():
    assert cf.no_tollgate_state_violations("", "") == []
    problems = cf.no_tollgate_state_violations("tollgate-wrt-0.6.0_alpha4_pre17-r1", "config.json\n")
    assert any("already installed" in p for p in problems)
    assert any("config.json" in p for p in problems)


# ---------------------------------------------------------------------------
# the Wi-Fi trap and the module identity ladder
# ---------------------------------------------------------------------------


def test_zero_wireless_interfaces_is_reported_as_a_problem():
    assert cf.count_wifi_ifaces('{"interfaces":[{"ifname":"phy0-ap0"},{"ifname":"phy1-ap0"}]}') == 2
    assert cf.count_wifi_ifaces('{"up":true,"interfaces":[]}') == 0
    violations = cf.wifi_iface_violations('{"up":true,"interfaces":[]}')
    assert len(violations) == 1
    assert "ZERO wireless interfaces" in violations[0]
    assert "disabled 1" in violations[0]
    assert cf.WIFI_IFACE_ENABLE_COMMANDS[0] in violations[0]
    assert cf.wifi_iface_violations('{"ifname":"phy0-ap0"}') == []


def test_module_identity_violations_require_kind_and_full_mode():
    good = '{"kind":10021,"price_per_step":[{"amount":1}]}'
    assert cf.parse_kind(good) == 10021
    assert cf.module_identity_violations(good) == []

    assert any("no `kind`" in p for p in cf.module_identity_violations(""))
    assert any("kind:10" in p for p in cf.module_identity_violations('{"kind":10020,"price_per_step":[]}'))
    degraded = cf.module_identity_violations('{"kind":10021}')
    assert len(degraded) == 1 and "DEGRADED" in degraded[0]


# ---------------------------------------------------------------------------
# the stage-3 handoff points at the machinery that already exists
# ---------------------------------------------------------------------------


def test_install_handoff_names_the_existing_install_path():
    handoff = cf.tollgate_install_handoff()
    assert handoff.order[-1].startswith("tollgate-wrt LAST")
    assert "nodogsplash" in handoff.order[2]
    assert "keepalive" in handoff.order[1]
    assert handoff.make_target == "install-path-e2e"
    assert handoff.script.endswith("install-path-e2e.py --flash-and-run")
    assert handoff.library == "lib/install_paths.py"
    assert any("install-path-e2e" in c for c in handoff.commands())
    for name in ("nodogsplash", "trustedmac", "tollgate-wrt", "keepalive"):
        assert any(name in step for step in handoff.order), name


def test_the_root_password_is_set_explicitly_because_a_fresh_image_has_none():
    assert cf.SET_ROOT_PASSWORD_COMMAND == "passwd root"
    assert cf.ROOT_PASSWORD_ENV == "TOLLGATE_ROUTER_PASSWORD"
    assert "EMPTY" in cf.SET_ROOT_PASSWORD_NOTE and "chpasswd" in cf.SET_ROOT_PASSWORD_NOTE


def test_the_lock_policy_is_explicit_and_does_not_silently_take_the_bench_flock():
    from lib import bench_lock

    assert "does NOT take" in cf.LOCK_CHOICE_NOTE
    assert "bench-mt3000.lock" in cf.LOCK_CHOICE_NOTE
    assert cf.take_cudy_lock() is False
    assert cf.DEFAULT_LOCK_PATH.endswith("bench-cudy-wr3000.lock")
    # it is a DIFFERENT lock from the MT3000 bench's single-owner flock
    assert cf.DEFAULT_LOCK_PATH != bench_lock.lock_path()


# ---------------------------------------------------------------------------
# no credential literal anywhere in the new code
# ---------------------------------------------------------------------------


def test_no_credential_literal_leaks_into_the_new_sources():
    """The lab default lives in tests/browser/admin_spa.spec.mjs — and ONLY there.

    The literal is *derived from that file at runtime* rather than written here,
    so this test cannot itself become the leak it guards against.
    """
    convention = PROJECT_ROOT / cf.PASSWORD_CONVENTION_REF
    assert convention.is_file(), cf.PASSWORD_CONVENTION_REF
    match = re.search(r"TOLLGATE_LUCI_PASSWORD\s*\|\|\s*'([^']+)'", convention.read_text(encoding="utf-8"))
    assert match, "the repo's password convention moved — update PASSWORD_CONVENTION_REF"
    literal = match.group(1)
    for relative in ("lib/cudy_flash.py", "scripts/cudy-flash.py", "tests/unit/test_cudy_flash.py"):
        assert literal not in (PROJECT_ROOT / relative).read_text(encoding="utf-8"), relative


def test_vendor_password_comes_from_env_only(monkeypatch):
    for name in cf.PASSWORD_ENVS:
        monkeypatch.delenv(name, raising=False)
    assert cf.vendor_password() == ""  # no literal fallback, ever
    monkeypatch.setenv("TOLLGATE_LUCI_PASSWORD", "from-env")
    assert cf.vendor_password() == "from-env"
    monkeypatch.setenv("CUDY_PASSWORD", "cudy-first")
    assert cf.vendor_password() == "cudy-first"  # CUDY_PASSWORD wins


# ---------------------------------------------------------------------------
# the CLI itself, exercised locally (offline, fail-closed)
# ---------------------------------------------------------------------------


def _run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(PROJECT_ROOT),
        env={**os.environ, **(env or {})},
    )


@pytest.mark.parametrize("command", ["check", "oem-upload", "sysupgrade", "capacity", "install-tollgate", "verify"])
def test_cli_help_is_available_for_every_subcommand(command):
    result = _run_cli(command, "--help")
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_cli_check_fails_closed_offline_and_says_why(tmp_path):
    """No switch, unverifiable images, a 2.0 box: refuse and NAME every gate.

    The image files exist but do not match the pinned sha256/size, so the check
    fails closed for its own reproducible reason (no reliance on this host's
    image cache).
    """
    bogus_transitional = _transitional_copy(tmp_path, b"bogus stage-1 bytes")
    bogus_mainline = _mainline_copy(tmp_path, b"bogus stage-2 bytes")
    result = _run_cli(
        "check",
        "--image",
        str(bogus_transitional),
        "--mainline-image",
        str(bogus_mainline),
        "--model",
        "WR3000 2.0",
        env={"TOLLGATE_ENABLE_SYSUPGRADE_FLASHING": ""},
    )
    assert result.returncode != 0
    out = result.stdout + result.stderr
    assert "destructive switch OFF" in out
    assert out.count("PROBLEM") >= 2  # both images unverifiable
    assert "variant=2.0" in out
    assert "would REFUSE" in out
    # named exit bits: the switch (8), the images (1) and the model (2)
    assert result.returncode & 8 and result.returncode & 1 and result.returncode & 2


def test_cli_check_can_classify_a_captured_page_offline(tmp_path):
    page = tmp_path / "bootloader.html"
    page.write_text(BOOTLOADER_HTML, encoding="utf-8")
    result = _run_cli(
        "check",
        "--page-html",
        str(page),
        "--url",
        "http://192.168.1.1/",
        env={"TOLLGATE_ENABLE_SYSUPGRADE_FLASHING": "true"},
    )
    out = result.stdout + result.stderr
    assert "bootloader" in out
    assert result.returncode & 4  # EXIT_PAGE: the page is not uploadable


def test_cli_oem_upload_refuses_the_switch_off_before_anything_else(tmp_path):
    result = _run_cli(
        "oem-upload",
        "--model",
        "WR3000 V1.0",
        "--image",
        str(_transitional_copy(tmp_path)),
        env={"TOLLGATE_ENABLE_SYSUPGRADE_FLASHING": ""},
    )
    assert result.returncode == 8
    out = result.stdout + result.stderr
    assert "REFUSING TO FLASH" in out
    assert "TOLLGATE_ENABLE_SYSUPGRADE_FLASHING" in out
    assert "drain" in out.lower()


def test_cli_oem_upload_refuses_a_2_0_box_even_with_the_switch_on(tmp_path):
    result = _run_cli(
        "oem-upload",
        "--model",
        "WR3000 V2.0",
        "--image",
        str(_transitional_copy(tmp_path)),
        "--assume-wr3000-v1",
        env={"TOLLGATE_ENABLE_SYSUPGRADE_FLASHING": "true"},
    )
    assert result.returncode == 2  # EXIT_MODEL
    assert "REFUSING TO FLASH" in result.stdout + result.stderr


def test_cli_oem_upload_refuses_an_unverified_image(tmp_path):
    image = _transitional_copy(tmp_path, b"wrong bytes")
    result = _run_cli(
        "oem-upload",
        "--model",
        "WR3000 V1.0",
        "--image",
        str(image),
        env={"TOLLGATE_ENABLE_SYSUPGRADE_FLASHING": "true"},
    )
    assert result.returncode != 0
    assert "sha256" in result.stdout + result.stderr


def test_cli_oem_upload_offline_rehearsal_uploads_nothing(tmp_path):
    """--page-html is a pure offline rehearsal: classify a captured page, upload nothing.

    Needs a verifiably-real transitional image on this host (a fabricated file
    cannot match the pinned sha256), so it is skipped where the operator's copy
    is absent — never faked.
    """
    if not REAL_TRANSITIONAL.is_file():
        pytest.skip("vendored transitional image not on this host")
    result = _run_cli(
        "oem-upload",
        "--model",
        "WR3000 V1.0",
        "--image",
        str(REAL_TRANSITIONAL),
        "--page-html",
        str(_vendor_page(tmp_path)),
        "--url",
        "http://192.168.10.1",
        env={"TOLLGATE_ENABLE_SYSUPGRADE_FLASHING": "true"},
    )
    out = result.stdout + result.stderr
    assert "vendor-ui" in out
    assert "no network call, nothing uploaded" in out
    # the rehearsal stops before the confirm gate: it never uploads
    assert "upload: POST" not in out
    assert "yes-i-mean-it" not in out


def test_cli_sysupgrade_refuses_an_unverified_image_before_touching_the_box(tmp_path):
    """Order matters: the image gate runs before the wallet probe / ssh."""
    fake = tmp_path / cf.MAINLINE_IMAGE_FILENAME
    fake.write_bytes(b"not the verified image")
    result = _run_cli(
        "sysupgrade",
        "--model",
        "WR3000 V1.0",
        "--mainline-image",
        str(fake),
        "--host",
        "192.0.2.1",  # TEST-NET-1: guaranteed unreachable
        env={"TOLLGATE_ENABLE_SYSUPGRADE_FLASHING": "true"},
    )
    assert result.returncode != 0
    out = result.stdout + result.stderr
    assert "REFUSING" in out and "sha256" in out
    assert "wallet" not in out  # it never got as far as probing


def test_cli_sysupgrade_defaults_to_no_confirm():
    """The confirm flags default to False, so a bare invocation cannot flash."""
    module = _load_cli_module()
    for command in ("sysupgrade", "oem-upload"):
        parsed = module.build_parser().parse_args([command, "--model", "WR3000 V1.0"])
        assert parsed.yes_i_mean_it is False


def _load_cli_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("cudy_flash_cli", CLI)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# the operator's real local images verify (when this host has them)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not REAL_TRANSITIONAL.is_file(), reason="vendored transitional image not on this host")
def test_the_local_transitional_image_matches_the_pinned_identity():
    assert cf.verify_transitional_image(str(REAL_TRANSITIONAL)) == []
    assert os.path.getsize(REAL_TRANSITIONAL) == cf.TRANSITIONAL_IMAGE_SIZE


@pytest.mark.skipif(not REAL_MAINLINE.is_file(), reason="mainline image not on this host")
def test_the_local_mainline_image_matches_the_pinned_identity():
    assert cf.verify_mainline_image(str(REAL_MAINLINE)) == []
    assert os.path.getsize(REAL_MAINLINE) == cf.MAINLINE_IMAGE_SIZE


# ---------------------------------------------------------------------------
# stage 1 pinned from the 2026-09-27 hardware run
# ---------------------------------------------------------------------------


def test_stage1_endpoint_and_file_field_are_the_pinned_hardware_values():
    assert cf.OEM_UPLOAD_ENDPOINT == "/cgi-bin/luci/admin/system/upgrade"
    assert cf.OEM_UPLOAD_FILE_FIELD == "cbid.upgrade.1.firmware"
    assert cf.OEM_UPLOAD_FILE_ACCEPT == ".bin"
    assert cf.OEM_PANEL_PATH == "/cgi-bin/luci/admin/panel"
    assert cf.OEM_REBOOT_UPGRADE_PATHS == (
        "/cgi-bin/luci/admin/system/reboot?upgrade=",
        "/reboot/apply?upgrade=true",
    )
    assert cf.OEM_FIRMWARE_PATHS == (cf.OEM_PANEL_PATH,)
    # the notes record the fragment trap and the two-step flow
    assert "2.6 KB" in cf.OEM_UPLOAD_FRAGMENT_NOTE
    assert "window.upload_file" in cf.OEM_UPLOAD_FRAGMENT_NOTE
    assert cf.OEM_UPLOAD_ENDPOINT in cf.OEM_UPLOAD_FLOW
    assert cf.OEM_UPLOAD_FILE_FIELD in cf.OEM_UPLOAD_FLOW
    assert cf.HARDWARE_VERIFIED_DATE == "2026-09-27"
    assert "EMPTY" in cf.TRANSITIONAL_ROOT_PASSWORD_NOTE


def test_stage1_sequence_is_accepted_only_on_upload_200_then_proceed_302():
    outcome = cf.classify_stage1_sequence(200, 302)
    assert outcome.accepted and outcome.kind == "accepted"
    assert cf.stage1_sequence_violations(outcome) == []
    # a 200 file POST and a 200 proceed POST is NOT the verified shape
    unknown = cf.classify_stage1_sequence(200, 200)
    assert unknown.kind == "unknown"
    assert cf.stage1_sequence_violations(unknown)
    # Cudy's signature refusal comes back as an error status
    refused = cf.classify_stage1_sequence(400, 0)
    assert refused.kind == "refused"
    assert "REFUSED" in cf.stage1_sequence_violations(refused)[0]


def test_stage1_sequence_never_reports_an_unknown_as_a_pass():
    violations = cf.stage1_sequence_violations(cf.classify_stage1_sequence(0, 0))
    assert violations
    assert "NOT a pass" in violations[0] or "not a pass" in violations[0].lower()
    assert "two-step" in violations[0]


# ---------------------------------------------------------------------------
# the flash-capacity wall (measured 2026-09-27)
# ---------------------------------------------------------------------------


def test_measured_flash_layout_and_payload_numbers_are_pinned():
    assert cf.FLASH_TOTAL_BYTES == 0xF10000
    assert cf.OVERLAY_FREE_BYTES_MEASURED == 4_600_000
    assert cf.TOLLGATE_BINARY_TOLLGATE_WRT_BYTES == 12_361_280
    assert cf.TOLLGATE_BINARY_TOLLGATE_BYTES == 7_373_632
    assert cf.TOLLGATE_UNCOMPRESSED_BYTES == 21 * 1024 * 1024
    assert cf.TOLLGATE_PACKAGE_COMPRESSED_BYTES == 8_500_000


def test_the_measured_payload_does_not_fit_the_measured_overlay():
    verdict = cf.check_install_capacity(
        cf.TOLLGATE_UNCOMPRESSED_BYTES, available_bytes=cf.OVERLAY_FREE_BYTES_MEASURED
    )
    assert not verdict.fits
    problems = cf.capacity_problems(verdict)
    assert len(problems) == 1 and problems[0].startswith("REFUSING TO INSTALL")
    assert str(cf.TOLLGATE_UNCOMPRESSED_BYTES) in problems[0]
    assert str(cf.OVERLAY_FREE_BYTES_MEASURED) in problems[0]
    assert "free overlay" in problems[0]
    assert "No space left on device" in problems[0]
    assert "volatile" in problems[0].lower()


def test_the_measured_payload_fits_the_measured_tmpfs():
    verdict = cf.check_install_capacity(
        cf.TOLLGATE_UNCOMPRESSED_BYTES,
        available_bytes=cf.TMPFS_FREE_BYTES_MEASURED,
        mode=cf.MODE_VOLATILE,
    )
    assert verdict.fits and cf.capacity_problems(verdict) == []


def test_capacity_preflight_names_the_free_space_it_compared():
    verdict = cf.check_install_capacity(1000, available_bytes=500)
    assert not verdict.fits
    assert "payload size 1000 B" in verdict.reason and "500 B" in verdict.reason


def test_capacity_preflight_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        cf.check_install_capacity(1, available_bytes=1, mode="ramdisk")


def test_df_and_tar_parsers():
    assert cf.parse_df_kb("4600\n") == 4600 * 1024
    assert cf.parse_df_kb("") is None and cf.parse_df_kb("garbage") is None
    listing = (
        "-rw-r--r-- root/root 12361280 2026-09-27 00:00 usr/bin/tollgate-wrt\n"
        "-rw-r--r-- root/root  7373632 2026-09-27 00:00 usr/bin/tollgate\n"
        "drwxr-xr-x root/root        0 2026-09-27 00:00 etc/\n"
    )
    assert cf.tar_uncompressed_bytes(listing) == 12361280 + 7373632
    assert cf.tar_uncompressed_bytes("") == 0


# ---------------------------------------------------------------------------
# the volatile (tmpfs) install — implemented, NOT hardware-verified
# ---------------------------------------------------------------------------


def test_volatile_plan_is_explicitly_not_persistent():
    plan = cf.volatile_install_plan()
    assert plan.persistent is False
    assert cf.VOLATILE_TMP_DIR.startswith("/tmp/")
    assert plan.big_binaries == ("usr/bin/tollgate-wrt", "usr/bin/tollgate")
    assert "LOST ON REBOOT" in cf.VOLATILE_INSTALL_NOTE
    assert "tmpfs" in cf.VOLATILE_INSTALL_NOTE
    assert "persistent=False" in plan.describe()  # the plan SAYS it is not persistent


def test_volatile_plan_symlinks_the_big_binaries_and_flashes_the_small_parts():
    commands = cf.volatile_install_plan().commands("/tmp/tollgate-wrt-0.6.0.apk")
    joined = "\n".join(commands)
    assert f"mkdir -p {cf.VOLATILE_TMP_DIR}" in commands
    assert f"tar -xzf /tmp/tollgate-wrt-0.6.0.apk -C {cf.VOLATILE_TMP_DIR}" in joined
    for relative in cf.VOLATILE_BIG_BINARIES:
        assert f"ln -sf {cf.VOLATILE_TMP_DIR}/{relative} /{relative}" in commands
    for prefix in cf.VOLATILE_FLASH_PREFIXES:
        assert f"cp -a {cf.VOLATILE_TMP_DIR}/{prefix}. /{prefix}" in joined
    # idempotent by construction: one mkdir -p, and every link is `ln -sf`
    assert commands.count("mkdir -p " + cf.VOLATILE_TMP_DIR) == 1
    assert all("scp" not in c and "sftp" not in c for c in commands)


def test_volatile_persistence_violations_refuse_a_persistent_claim():
    assert cf.volatile_persistence_violations(claim_persistent=False) == []
    violations = cf.volatile_persistence_violations(claim_persistent=True)
    assert violations and "REFUSING TO CALL THIS PERSISTENT" in violations[0]
    assert "LOST on reboot" in violations[0]


# ---------------------------------------------------------------------------
# the capacity subcommand (offline)
# ---------------------------------------------------------------------------


def test_cli_capacity_refuses_the_measured_payload_against_the_measured_overlay():
    result = _run_cli(
        "capacity",
        "--payload-bytes",
        str(cf.TOLLGATE_UNCOMPRESSED_BYTES),
        env={"TOLLGATE_ENABLE_SYSUPGRADE_FLASHING": "true"},
    )
    out = result.stdout + result.stderr
    assert "payload size" in out and "free overlay" in out
    assert result.returncode & 128  # EXIT_CAPACITY


def test_cli_capacity_volatile_mode_is_green_when_tmpfs_has_room():
    result = _run_cli(
        "capacity",
        "--payload-bytes",
        str(cf.TOLLGATE_UNCOMPRESSED_BYTES),
        "--tmpfs-free-bytes",
        str(cf.TMPFS_FREE_BYTES_MEASURED),
        "--mode",
        "volatile",
        env={"TOLLGATE_ENABLE_SYSUPGRADE_FLASHING": "true"},
    )
    out = result.stdout + result.stderr
    assert "fits=True" in out
    assert "persistent=False" in out
    assert result.returncode == 0

