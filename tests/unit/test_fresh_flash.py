"""Unit tests for :mod:`lib.fresh_flash` — the drain-before-flash gate.

The interesting cases are all refusals: a non-empty wallet must stop the flash
(real money), an unverified image must stop the flash, and a "fresh" image that
still carries tollgate state must stop the scenario.
"""

from __future__ import annotations

import os

import pytest

from lib import fresh_flash as ff

SAMPLE_FILENAME = "openwrt-25.12.5-mediatek-filogic-glinet_gl-mt3000-squashfs-sysupgrade.bin"


# ---------------------------------------------------------------------------
# image validation
# ---------------------------------------------------------------------------


def test_validate_image_filename_accepts_the_bench_image():
    assert ff.validate_image_filename(SAMPLE_FILENAME) == []


@pytest.mark.parametrize(
    "filename",
    [
        "openwrt-24.10.1-mediatek-filogic-glinet_gl-mt3000-squashfs-sysupgrade.bin",
        "openwrt-25.12.5-mediatek-filogic-glinet_gl-mt6000-squashfs-sysupgrade.bin",
        "openwrt-25.12.5-ramips-mt7621-glinet_gl-mt3000-squashfs-sysupgrade.bin",
        "openwrt-25.12.5-mediatek-filogic-glinet_gl-mt3000-squashfs-factory.img",
        "tollgate-os-glinet_gl-mt3000.img",
    ],
)
def test_validate_image_filename_rejects_wrong_release_target_device_or_mode(filename):
    assert ff.validate_image_filename(filename), f"{filename} should have been rejected"


def test_validate_image_filename_rejects_empty():
    assert ff.validate_image_filename("")


def test_resolve_image_path_prefers_explicit_then_local_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(ff, "LOCAL_IMAGE_CANDIDATES", ())
    missing = str(tmp_path / "nope.bin")
    with pytest.raises(ff.ImageInvalid):
        ff.resolve_image_path(missing)

    local = tmp_path / ff.BENCH_IMAGE_FILENAME
    local.write_bytes(b"img")
    monkeypatch.setattr(ff, "LOCAL_IMAGE_CANDIDATES", (str(local),))
    assert ff.resolve_image_path() == str(local)
    assert ff.resolve_image_path(str(local)) == str(local)


def test_expected_sha_from_upstream_sums():
    sums = (
        "a" * 64 + " *openwrt-25.12.5-mediatek-filogic-glinet_gl-mt3000-kernel.bin\n"
        + ff.BENCH_IMAGE_SHA256 + " *" + SAMPLE_FILENAME + "\n"
    )
    assert ff.expected_sha_from_upstream_sums(sums, SAMPLE_FILENAME) == ff.BENCH_IMAGE_SHA256
    assert ff.expected_sha_from_upstream_sums(sums, "not-there.bin") is None
    assert ff.expected_sha_from_upstream_sums("", SAMPLE_FILENAME) is None


def test_verify_image_checks_name_and_hash(tmp_path):
    image = tmp_path / SAMPLE_FILENAME
    image.write_bytes(b"not the real image")
    problems = ff.verify_image(str(image))
    assert len(problems) == 1 and "sha256" in problems[0] and "refusing to flash" in problems[0]

    bad_name = tmp_path / "random.bin"
    bad_name.write_bytes(b"x")
    assert ff.verify_image(str(bad_name))  # name problems win

    monkeypatch_target = tmp_path / "good.bin"
    monkeypatch_target.write_bytes(b"contents")
    from lib.install_paths import sha256_file

    good = tmp_path / SAMPLE_FILENAME
    good.write_bytes(b"contents")
    assert ff.verify_image(str(good), sha256_file(good)) == []


# ---------------------------------------------------------------------------
# wallet gate — real money
# ---------------------------------------------------------------------------


def test_parse_wallet_balance_from_json_and_from_text():
    assert ff.parse_wallet_balance('{"mints":[{"url":"x","balance":1200}]}') == 1200
    assert ff.parse_wallet_balance("total: 42 sats") == 42
    assert ff.parse_wallet_balance("no numbers here") == 0
    assert ff.parse_wallet_balance("") == 0


def test_parse_ecash_listing_from_find_and_ls():
    listing = (
        "/etc/tollgate/ecash/token-1\n"
        "/etc/tollgate/ecash/token-2\n"
        "total 8\n"
        "drwxr-xr-x    2 root     root          4096 Sep 24 22:00 .\n"
        "-rw-r--r--    1 root     root           128 Sep 24 22:00 proof.json\n"
        "-rw-r--r--    1 root     root             0 Sep 24 22:00 empty.tmp\n"
    )
    files = ff.parse_ecash_listing(listing)
    assert "/etc/tollgate/ecash/token-1" in files
    assert "/etc/tollgate/ecash/proof.json" in files
    assert not any("empty.tmp" in f for f in files)
    assert ff.parse_ecash_listing("") == []


def test_parse_wallet_state_is_empty_when_nothing_held():
    state = ff.parse_wallet_state('{"total":0}', "total 0\n")
    assert state.empty and state.probed
    assert "total_sats=0" in state.summary()


def test_flash_guard_allows_an_empty_wallet():
    ff.flash_guard(ff.parse_wallet_state("", ""), allow_nonempty=False)


def test_flash_guard_refuses_a_nonempty_wallet_with_the_drain_command():
    state = ff.parse_wallet_state('{"total":2100}', "/etc/tollgate/ecash/token-1\n")
    assert not state.empty
    with pytest.raises(ff.FlashRefused) as excinfo:
        ff.flash_guard(state, allow_nonempty=False)
    message = str(excinfo.value)
    assert "REFUSING TO FLASH" in message
    assert ff.DRAIN_COMMAND in message
    assert "--allow-nonempty-wallet" in message
    assert "total_sats=2100" in message


def test_flash_guard_refuses_on_ecash_files_even_with_a_zero_balance_reading():
    """A zero JSON total must not paper over proofs sitting on disk."""
    state = ff.parse_wallet_state('{"total":0}', "/etc/tollgate/ecash/token-9\n")
    with pytest.raises(ff.FlashRefused):
        ff.flash_guard(state, allow_nonempty=False)


def test_flash_guard_can_be_overridden_explicitly():
    state = ff.parse_wallet_state('{"total":2100}', "/etc/tollgate/ecash/token-1\n")
    ff.flash_guard(state, allow_nonempty=True)  # explicit operator opt-in only


def test_the_drain_command_is_the_documented_cli_form():
    assert ff.DRAIN_COMMAND == "tollgate wallet drain cashu --yes"
    assert "ecash" in ff.ECASH_LISTING_COMMAND


# ---------------------------------------------------------------------------
# the two flash gates: the destructive switch AND the money gate
# ---------------------------------------------------------------------------


def test_flashing_is_off_unless_the_operator_switches_it_on(monkeypatch):
    monkeypatch.delenv(ff.FLASH_ENABLE_ENV, raising=False)
    assert ff.flashing_enabled() is False
    with pytest.raises(ff.FlashRefused) as excinfo:
        ff.flash_enable_gate()
    message = str(excinfo.value)
    assert ff.FLASH_ENABLE_ENV in message
    assert "REFUSING TO FLASH" in message
    assert ff.DRAIN_COMMAND in message

    for value in ("true", "TRUE", "1", "yes"):
        monkeypatch.setenv(ff.FLASH_ENABLE_ENV, value)
        assert ff.flashing_enabled() is True
        ff.flash_enable_gate()  # must not raise

    for value in ("", "0", "false", "no", "maybe"):
        monkeypatch.setenv(ff.FLASH_ENABLE_ENV, value)
        assert ff.flashing_enabled() is False


def test_flash_preconditions_reports_every_blocker_at_once(monkeypatch):
    monkeypatch.delenv(ff.FLASH_ENABLE_ENV, raising=False)
    money = ff.parse_wallet_state('{"total":7}', "/etc/tollgate/ecash/token-1\n")
    blockers = ff.flash_preconditions(money, allow_nonempty=False)
    assert len(blockers) == 2, blockers  # the switch AND the money
    assert any(ff.FLASH_ENABLE_ENV in blocker for blocker in blockers)
    assert any("NOT empty" in blocker for blocker in blockers)
    assert any(ff.DRAIN_COMMAND in blocker for blocker in blockers)


def test_flash_preconditions_is_clean_only_with_switch_on_and_empty_wallet(monkeypatch):
    empty = ff.parse_wallet_state("", "")
    monkeypatch.delenv(ff.FLASH_ENABLE_ENV, raising=False)
    blockers = ff.flash_preconditions(empty)
    assert len(blockers) == 1, blockers  # only the switch blocks it
    assert ff.FLASH_ENABLE_ENV in blockers[0]
    monkeypatch.setenv(ff.FLASH_ENABLE_ENV, "true")
    assert ff.flash_preconditions(empty) == []


def test_flash_preconditions_still_refuses_money_with_the_switch_on(monkeypatch):
    monkeypatch.setenv(ff.FLASH_ENABLE_ENV, "true")
    money = ff.parse_wallet_state('{"total":7}', "/etc/tollgate/ecash/token-1\n")
    blockers = ff.flash_preconditions(money, allow_nonempty=False)
    assert len(blockers) == 1
    assert "NOT empty" in blockers[0]
    # ... unless the loss is explicitly accepted
    assert ff.flash_preconditions(money, allow_nonempty=True) == []


# ---------------------------------------------------------------------------
# flash + post-flash assertions
# ---------------------------------------------------------------------------


def test_sysupgrade_command_always_wipes_config():
    assert ff.sysupgrade_command("/tmp/img.bin") == "sysupgrade -n /tmp/img.bin"
    assert ff.sysupgrade_command("/tmp/img.bin", keep_config=True) == "sysupgrade /tmp/img.bin"
    assert ff.remote_image_path(SAMPLE_FILENAME) == f"/tmp/{SAMPLE_FILENAME}"


def test_post_flash_readdress_and_board_commands():
    cmd = ff.post_flash_readdress_command("enp0s31f6")
    assert "192.168.1.200/24" in cmd and "enp0s31f6" in cmd
    assert "board_name" in ff.board_identity_command()


def test_fresh_image_ready_violations_accepts_a_real_fresh_board():
    output = "glinet,gl-mt3000\nOpenWrt 25.12.5 r33051-f5dae5ece4|aarch64_cortex-a53"
    assert ff.fresh_image_ready_violations(output) == []


def test_fresh_image_ready_violations_flags_stale_or_wrong_board():
    problems = ff.fresh_image_ready_violations("glinet,gl-mt6000\nOpenWrt 24.10.1|mips_24kc")
    assert len(problems) == 3
    assert any("gl-mt3000" in p for p in problems)
    assert any("25.12.x" in p for p in problems)
    assert any("arch" in p for p in problems)


def test_no_tollgate_state_violations_passes_on_a_stock_image():
    assert ff.no_tollgate_state_violations("", "") == []
    assert ff.no_tollgate_state_violations("", "total 0\ndrwxr-xr-x 2 root root 0 /etc/tollgate\n") == []


def test_no_tollgate_state_violations_catches_a_package_or_surviving_config():
    problems = ff.no_tollgate_state_violations(
        "tollgate-wrt-0.6.0_alpha4_pre16-r1", "config.json\nidentities.json\n"
    )
    assert any("already installed" in p for p in problems)
    assert any("config.json" in p for p in problems)
    assert any("identities.json" in p for p in problems)


def test_local_image_candidate_is_documented():
    # the operator's pre-downloaded copy; path is asserted to be absolute
    assert all(os.path.isabs(p) for p in ff.LOCAL_IMAGE_CANDIDATES)
