"""Unit tests for :mod:`lib.install_paths` (no router, no network).

These cover the parts that must be right before any bench time is spent:
artifact selection by package manager, the release-manifest sha256 check, the
artifact-identity gate, the POLICY gate, and the happy-path report parser.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile

import pytest

from lib import install_paths as ip

APK_SHA = "104e9ce00b8f01c09840c9aa6d8976c6a6712a4cd05376ddf5f5a237eb2b4e72"
IPK_SHA = "b90e9966dfb485b8d601144bada9b411711d3d98c1a78444360ddac997a052c4"

MANIFEST_FIXTURE = f"""{APK_SHA}  tollgate-wrt_0.6.0_alpha4_pre16_aarch64_cortex-a53.apk
{IPK_SHA}  tollgate-wrt_0.6.0_alpha4_pre16_aarch64_cortex-a53.ipk
c0ffee{"0" * 58}  tollgate-wrt_0.6.0_alpha4_pre16_x86_64.apk

# a comment line that must be ignored
"""


# ---------------------------------------------------------------------------
# manifest + selection
# ---------------------------------------------------------------------------


def test_parse_manifest_ignores_comments_and_handles_binary_marker():
    parsed = ip.parse_manifest(MANIFEST_FIXTURE)
    assert parsed["tollgate-wrt_0.6.0_alpha4_pre16_aarch64_cortex-a53.apk"] == APK_SHA
    assert parsed["tollgate-wrt_0.6.0_alpha4_pre16_aarch64_cortex-a53.ipk"] == IPK_SHA
    assert len(parsed) == 3
    # leading-star (binary mode) and trailing-comment sum lines are tolerated
    binary_line = f"{APK_SHA} *tollgate-wrt_1.0_aarch64_cortex-a53.apk"
    assert ip.parse_manifest(binary_line) == {"tollgate-wrt_1.0_aarch64_cortex-a53.apk": APK_SHA}


def test_parse_manifest_rejects_malformed_lines():
    assert ip.parse_manifest("not a manifest\n") == {}
    assert ip.parse_manifest("deadbeef  too-short-digest.apk\n") == {}


@pytest.mark.parametrize(
    ("version", "expected"),
    [("25.12.5", "apk"), ("25.12.0", "apk"), ("24.10.1", "opkg"), ("23.05.3", "opkg")],
)
def test_package_manager_for_openwrt_version(version, expected):
    assert ip.package_manager_for_openwrt_version(version) == expected


def test_package_manager_rejects_garbage():
    with pytest.raises(ip.InstallPathError):
        ip.package_manager_for_openwrt_version("knots")


def test_select_artifact_apk_image_picks_the_apk_with_manifest_hash():
    selection = ip.select_artifact(
        ip.parse_manifest(MANIFEST_FIXTURE),
        ip.BENCH_ARCH,
        "apk",
        release_tag="v0.6.0-alpha4-pre16",
        version="0.6.0_alpha4_pre16",
    )
    artifact = selection.require()
    assert artifact.fmt == "apk"
    assert artifact.sha256 == APK_SHA
    assert artifact.filename == "tollgate-wrt_0.6.0_alpha4_pre16_aarch64_cortex-a53.apk"
    assert artifact.url.endswith("/v0.6.0-alpha4-pre16/" + artifact.filename)
    assert not selection.skipped


def test_select_artifact_defaults_to_the_pre17_release():
    """The default release under test must be the one the policy gate can assert."""
    with pytest.raises(ip.ArtifactNotFound) as excinfo:
        # the pre16 manifest fixture cannot serve the pre17 default
        ip.select_artifact(ip.parse_manifest(MANIFEST_FIXTURE), ip.BENCH_ARCH, "apk").require()
    assert ip.FEED_VERSION_DEFAULT in str(excinfo.value)
    assert ip.FEED_RELEASE_DEFAULT in str(excinfo.value)


def test_select_artifact_opkg_image_is_an_explicit_skip_not_a_pass():
    selection = ip.select_artifact(ip.parse_manifest(MANIFEST_FIXTURE), ip.BENCH_ARCH, "opkg")
    assert selection.skipped is True
    assert selection.artifact is None
    assert ip.IPK_REJECTION_NEEDLE in selection.skip_reason  # names the real failure mode
    assert "exit 99" in selection.skip_reason
    with pytest.raises(ip.ArtifactNotFound):
        selection.require()


def test_select_artifact_unknown_arch_fails_loudly():
    with pytest.raises(ip.ArtifactNotFound):
        ip.select_artifact(ip.parse_manifest(MANIFEST_FIXTURE), "mips_24kc", "apk").require()


def test_artifact_filename_and_format_mapping():
    assert ip.artifact_filename("0.6.0_alpha4_pre16", "x86_64", "ipk") == (
        "tollgate-wrt_0.6.0_alpha4_pre16_x86_64.ipk"
    )
    assert ip.format_for_package_manager("opkg") == "ipk"
    with pytest.raises(ip.InstallPathError):
        ip.artifact_filename("1", "x", "rpm")


def test_release_asset_url_shape():
    url = ip.release_asset_url("v0.6.0-alpha4-pre16", "SHA256SUMS")
    assert url == "https://github.com/FreedomTechFeed/packages/releases/download/v0.6.0-alpha4-pre16/SHA256SUMS"


def test_verify_sha256(tmp_path):
    blob = tmp_path / "x.apk"
    blob.write_bytes(b"payload")
    digest = hashlib.sha256(b"payload").hexdigest()
    ok, actual = ip.verify_sha256(blob, digest.upper())
    assert ok and actual == digest
    ok, actual = ip.verify_sha256(blob, "0" * 64)
    assert not ok and actual == digest


# ---------------------------------------------------------------------------
# artifact-identity gate
# ---------------------------------------------------------------------------


def _make_ipk(path, binary_bytes: bytes, member: str = "./usr/bin/tollgate-wrt"):
    """Build a minimal but structurally real .ipk (outer gz tar + data.tar.gz)."""
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w:gz") as data:
        info = tarfile.TarInfo(name=member)
        info.size = len(binary_bytes)
        info.mode = 0o755
        data.addfile(info, io.BytesIO(binary_bytes))
    with tarfile.open(path, mode="w:gz") as outer:
        payload = inner.getvalue()
        info = tarfile.TarInfo(name="./data.tar.gz")
        info.size = len(payload)
        outer.addfile(info, io.BytesIO(payload))
    return path


def test_binary_sha256_from_ipk_reads_the_payload_binary(tmp_path):
    binary = b"\x7fELF-fake-aarch64-binary"
    ipk = _make_ipk(tmp_path / "tollgate-wrt.ipk", binary)
    assert ip.binary_sha256_from_ipk(ipk) == hashlib.sha256(binary).hexdigest()


def test_binary_sha256_from_ipk_without_binary_is_an_error(tmp_path):
    ipk = _make_ipk(tmp_path / "tollgate-wrt.ipk", b"x", member="./usr/bin/other")
    with pytest.raises(ip.InstallPathError) as excinfo:
        ip.binary_sha256_from_ipk(ipk)
    assert "no /usr/bin/tollgate-wrt in payload" in str(excinfo.value)


def test_binary_sha256_from_ipk_rejects_an_apk(tmp_path):
    """Passing the wrong format is a loud error, never a silent empty hash."""
    apk = tmp_path / "tollgate-wrt.apk"
    apk.write_bytes(b"ADBd-not-an-ipk")
    with pytest.raises(ip.InstallPathError):
        ip.binary_sha256_from_ipk(apk)


def test_binary_sha256_from_apk_uses_apk_tools_and_is_format_specific(tmp_path, monkeypatch):
    """The .apk path must go through apk-tools, not tar (apk v3 is ADB-prefixed)."""
    binary = b"\x7fELF-fake-aarch64-binary-from-apk"
    expected = hashlib.sha256(binary).hexdigest()
    stub = tmp_path / "apk.static"
    stub.write_text(
        "#!/bin/sh\n"
        "# emulate: apk extract --allow-untrusted --destination DIR FILE\n"
        "dest=''\n"
        "while [ $# -gt 0 ]; do case \"$1\" in --destination) dest=\"$2\"; shift 2;; *) shift;; esac; done\n"
        'mkdir -p "$dest/usr/bin"\n'
        f"printf '%s' '{binary.decode()}' > \"$dest/usr/bin/tollgate-wrt\"\n"
        'echo "$dest/usr/bin/tollgate-wrt"\n'
    )
    stub.chmod(0o755)
    monkeypatch.setenv("TOLLGATE_APK_TOOL", str(stub))

    apk = tmp_path / "tollgate-wrt.apk"
    apk.write_bytes(b"ADBd-not-a-tar")
    assert ip.binary_sha256_from_apk(apk) == expected
    assert ip.binary_sha256_from_artifact(apk, "apk") == expected

    # ...and it is *not* the ipk hash: the two formats are different builds.
    ipk = _make_ipk(tmp_path / "tollgate-wrt.ipk", b"\x7fELF-a-different-build")
    assert ip.binary_sha256_from_artifact(ipk, "ipk") != expected


def test_apk_tool_missing_gives_actionable_error(monkeypatch):
    monkeypatch.delenv("TOLLGATE_APK_TOOL", raising=False)
    monkeypatch.setattr(ip.shutil, "which", lambda _name: None)
    with pytest.raises(ip.InstallPathError) as excinfo:
        ip.apk_tool()
    assert "apk-tools-static" in str(excinfo.value)


def test_apk_tool_env_override_must_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("TOLLGATE_APK_TOOL", str(tmp_path / "nope"))
    with pytest.raises(ip.InstallPathError):
        ip.apk_tool()


def test_identity_violations_passes_on_match_and_fails_on_mismatch():
    good = "dce8b1f1c89a0d04d705aa4ed15071aaf66a658dda791dee0996f99c76fd56bb"
    assert ip.identity_violations(good + "  /usr/bin/tollgate-wrt", good, "x.apk") == []
    problems = ip.identity_violations("5ddda42b" + "0" * 56, good, "x.apk")
    assert len(problems) == 1 and "artifact-identity FAILED" in problems[0]
    assert ip.identity_violations("", good, "x.apk")
    assert ip.identity_violations(good, "", "x.apk")


def test_apk_vs_ipk_hashes_are_never_cross_compared():
    """Regression guard for the release-identity trap (see the dry-run report)."""
    apk_derived = "dce8b1f1c89a0d04d705aa4ed15071aaf66a658dda791dee0996f99c76fd56bb"
    ipk_derived = "5ddda42bf55c3e007ce01016d9aa076661307e604c8df4e3b4d6e11c86c21956"
    problems = ip.identity_violations(apk_derived, ipk_derived, "tollgate-wrt_..._a53.ipk")
    assert problems and "NOT the artifact under test" in problems[0]


def test_identity_gate_refuses_a_hash_from_the_sibling_format():
    """Pinning the trap: same-format only, and a cross-format hash RAISES.

    ``v0.6.0-alpha4-pre16``: the apk payload ``usr/bin/tollgate-wrt`` is
    12 242 208 B / ``dce8b1f1…``; the ipk payload is 12 295 456 B /
    ``5ddda42b…``.  Comparing the installed binary against the sibling
    format's hash is a false failure — the gate must refuse, not compare.
    """
    apk_artifact = ip.Artifact(
        filename="tollgate-wrt_0.6.0_alpha4_pre16_aarch64_cortex-a53.apk",
        sha256="104e9ce0",
        fmt="apk",
        arch="aarch64_cortex-a53",
        release_tag="v0.6.0-alpha4-pre16",
    )
    apk_payload = "dce8b1f1c89a0d04d705aa4ed15071aaf66a658dda791dee0996f99c76fd56bb"
    ipk_payload = "5ddda42bf55c3e007ce01016d9aa076661307e604c8df4e3b4d6e11c86c21956"

    # the correct invocation: the artifact's own format
    assert (
        ip.identity_gate(
            artifact=apk_artifact,
            installed_sha256=apk_payload,
            expected_sha256=apk_payload,
            expected_format="apk",
        )
        == []
    )
    # the trap: a hash derived from the sibling .ipk
    with pytest.raises(ip.CrossFormatIdentityComparison) as excinfo:
        ip.identity_gate(
            artifact=apk_artifact,
            installed_sha256=apk_payload,
            expected_sha256=ipk_payload,
            expected_format="ipk",
        )
    message = str(excinfo.value)
    assert "DIFFERENT BUILDS" in message
    assert "same format" in message
    # and the gate still gets the *real* comparison wrong when formats agree
    assert ip.identity_gate(
        artifact=apk_artifact,
        installed_sha256=ipk_payload,  # router runs the ipk build
        expected_sha256=apk_payload,
        expected_format="apk",
    )
    assert ip.sibling_format("apk") == "ipk"
    assert ip.sibling_format("ipk") == "apk"
    with pytest.raises(ip.InstallPathError):
        ip.sibling_format("deb")


# ---------------------------------------------------------------------------
# release ordering + the flash-free POLICY pre-flight (the pre17 floor)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("v0.6.0-alpha4-pre16", "v0.6.0-alpha4-pre17"),
        ("v0.6.0-alpha4-pre17", "v0.6.0-alpha4-pre18"),
        ("v0.6.0-alpha4-pre17", "v0.6.0-alpha4"),  # the alpha itself is later
        ("v0.6.0-alpha4", "v0.6.0-beta1"),
        ("v0.6.0-beta1", "v0.6.0-rc1"),
        ("v0.6.0-rc1", "v0.6.0"),
        ("v0.6.0-alpha4-pre17", "v0.6.0-alpha5-pre1"),  # "pre17 or newer"
        ("0.6.0-alpha4-pre16", "v0.6.0-alpha4-pre17"),  # leading v optional
        ("v0.5.9", "v0.6.0-alpha4-pre1"),
    ],
)
def test_release_rank_orders_pre_releases_before_their_stage(older, newer):
    assert ip.release_rank(older) < ip.release_rank(newer)
    assert ip.release_at_least(newer, older)
    assert not ip.release_at_least(older, newer)


def test_release_rank_rejects_garbage():
    with pytest.raises(ip.InstallPathError):
        ip.release_rank("nightly")
    with pytest.raises(ip.InstallPathError):
        ip.release_rank("")


def test_default_release_under_test_is_pre17_or_newer():
    """The policy assertion's target release defaults to pre17 — never older."""
    assert ip.FEED_RELEASE_DEFAULT == ip.POLICY_TARGET_RELEASE_DEFAULT
    assert ip.release_at_least(ip.FEED_RELEASE_DEFAULT, ip.POLICY_MIN_RELEASE_DEFAULT)
    assert ip.release_at_least(ip.POLICY_TARGET_RELEASE_DEFAULT, "v0.6.0-alpha4-pre17")


def test_policy_preflight_flags_pre16_as_unsupported_by_name():
    """The published pre16 cannot satisfy the guard assertion — say it by name."""
    result = ip.policy_preflight("v0.6.0-alpha4-pre16")
    assert result.supported is False
    assert result.target_release == "v0.6.0-alpha4-pre16"
    assert result.minimum_release == ip.POLICY_MIN_RELEASE_DEFAULT
    message = result.message()
    assert "v0.6.0-alpha4-pre16" in message  # the release is NAMED
    assert "UNSUPPORTED for the POLICY/guard assertion" in message
    assert "31-admin-board-not-guest-reachable.nft" in message
    assert "8090" in message
    with pytest.raises(ip.PolicyAssertionUnsupported):
        ip.require_policy_supported("v0.6.0-alpha4-pre16")


@pytest.mark.parametrize(
    "tag", ["v0.6.0-alpha4-pre17", "v0.6.0-alpha4-pre18", "v0.6.0-alpha4", "v0.6.0", "v0.6.1"]
)
def test_policy_preflight_accepts_pre17_and_newer(tag):
    result = ip.policy_preflight(tag)
    assert result.supported is True
    assert "supports the POLICY/guard assertion" in result.message()
    assert ip.require_policy_supported(tag).supported


def test_policy_preflight_honours_the_env_and_the_guard_path(monkeypatch):
    monkeypatch.setenv(ip.POLICY_TARGET_RELEASE_ENV, "v0.6.0-alpha4-pre16")
    monkeypatch.setenv(ip.POLICY_GUARD_PATH_ENV, "/etc/nftables.d/99-custom-guard.nft")
    result = ip.policy_preflight()
    assert result.target_release == "v0.6.0-alpha4-pre16"
    assert result.guard_path == "/etc/nftables.d/99-custom-guard.nft"
    assert result.supported is False
    assert "99-custom-guard.nft" in result.message()


def test_guard_path_is_configurable_and_defaults_to_the_566_file(monkeypatch):
    monkeypatch.delenv(ip.POLICY_GUARD_PATH_ENV, raising=False)
    assert ip.guard_nft_path() == ip.GUARD_NFT_FILE
    assert ip.guard_nft_basename() == ip.GUARD_NFT_BASENAME
    monkeypatch.setenv(ip.POLICY_GUARD_PATH_ENV, "/etc/nftables.d/77-other.nft")
    assert ip.guard_nft_path() == "/etc/nftables.d/77-other.nft"
    assert ip.guard_nft_basename() == "77-other.nft"
    # an override must be honoured by the assertions, not just by the accessor
    assert ip.guard_nft_path("/root/explicit.nft") == "/root/explicit.nft"
    problems = ip.policy_violations({22}, guard_present=False, guard_drops_br_lan=False, ssh_alive=True)
    assert any("77-other.nft" in problem for problem in problems), problems


def test_readiness_and_policy_gates_use_the_configured_guard_path(monkeypatch):
    monkeypatch.setenv(ip.POLICY_GUARD_PATH_ENV, "/etc/nftables.d/77-other.nft")
    readiness = ip.artifact_policy_readiness(
        ["etc/nftables.d/77-other.nft"], GUARD_SETUP_SCRIPT
    )
    assert readiness["ships_guard_nft"] is True
    assert readiness["guard_path"] == "/etc/nftables.d/77-other.nft"
    assert readiness["problems"] == []
    # the default guard basename would NOT satisfy the override
    monkeypatch.delenv(ip.POLICY_GUARD_PATH_ENV, raising=False)
    other = ip.artifact_policy_readiness(["etc/nftables.d/77-other.nft"], GUARD_SETUP_SCRIPT)
    assert other["ships_guard_nft"] is False


def test_fetch_release_manifest_names_an_unpublished_release(monkeypatch):
    """A 404 on SHA256SUMS is a named outcome, not a mystery HTTPError."""
    import urllib.error
    from email.message import Message

    def _boom(_url, **_kwargs):
        raise urllib.error.HTTPError(_url, 404, "Not Found", Message(), None)

    monkeypatch.setattr(ip, "http_get", _boom)
    with pytest.raises(ip.ReleaseNotPublished) as excinfo:
        ip.fetch_release_manifest("v0.6.0-alpha4-pre17")
    message = str(excinfo.value)
    assert "v0.6.0-alpha4-pre17" in message and "not published" in message

    # a non-404 must NOT be swallowed
    def _server_error(_url, **_kwargs):
        raise urllib.error.HTTPError(_url, 500, "Server Error", Message(), None)

    monkeypatch.setattr(ip, "http_get", _server_error)
    with pytest.raises(urllib.error.HTTPError):
        ip.fetch_release_manifest("v0.6.0-alpha4-pre17")


def test_opkg_selection_is_always_an_explicit_skip_with_a_reason():
    """Never a silent pass: the skipped branch carries its reason and refuses."""
    selection = ip.select_artifact({}, "aarch64_cortex-a53", "opkg")
    assert selection.skipped is True
    assert selection.artifact is None
    assert "v2 package format error" in selection.skip_reason
    assert "never treated as a pass" in selection.skip_reason
    with pytest.raises(ip.ArtifactNotFound):
        selection.require()


# ---------------------------------------------------------------------------
# payload inspection: does the artifact actually ship the #566 policy material?
# ---------------------------------------------------------------------------


def _fake_apk_tool(tmp_path, files: dict[str, str]) -> str:
    """Write a stub apk.static that materialises *files* under --destination."""
    stub = tmp_path / "apk.static"
    body = "\n".join(
        f'mkdir -p "$dest/$(dirname {name})"; printf %s {payload!r} > "$dest/{name}"'
        for name, payload in files.items()
    )
    stub.write_text(
        "#!/bin/sh\n"
        "dest=''\n"
        "while [ $# -gt 0 ]; do case \"$1\" in --destination) dest=\"$2\"; shift 2;; *) shift;; esac; done\n"
        + body
        + "\nexit 0\n"
    )
    stub.chmod(0o755)
    return str(stub)


GUARD_SETUP_SCRIPT = (
    "uci -q del_list nodogsplash.@nodogsplash[0].users_to_router='allow tcp port 8090'\n"
    "uci -q del_list nodogsplash.@nodogsplash[0].users_to_router='allow tcp port 8443'\n"
)


def test_payload_files_and_text_for_an_ipk(tmp_path):
    ipk = _make_ipk(tmp_path / "tg.ipk", b"binary", member="./usr/bin/tollgate-wrt")
    # add a second member so the listing is meaningful
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w:gz") as data:
        for name, payload in (
            ("./usr/bin/tollgate-wrt", b"binary"),
            ("./etc/uci-defaults/99-tollgate-setup", GUARD_SETUP_SCRIPT.encode()),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            data.addfile(info, io.BytesIO(payload))
    with tarfile.open(ipk, mode="w:gz") as outer:
        payload = inner.getvalue()
        info = tarfile.TarInfo(name="./data.tar.gz")
        info.size = len(payload)
        outer.addfile(info, io.BytesIO(payload))

    files = ip.payload_files(ipk, "ipk")
    assert "usr/bin/tollgate-wrt" in files
    assert "etc/uci-defaults/99-tollgate-setup" in files
    assert "8090" in ip.payload_text(ipk, "ipk", ip.SETUP_SCRIPT)
    with pytest.raises(ip.InstallPathError):
        ip.payload_text(ipk, "ipk", "/etc/not-there")


def test_payload_files_and_text_for_an_apk(tmp_path, monkeypatch):
    stub = _fake_apk_tool(
        tmp_path,
        {
            "usr/bin/tollgate-wrt": "binary",
            "etc/uci-defaults/99-tollgate-setup": "uci del_list 8090\n",
        },
    )
    monkeypatch.setenv("TOLLGATE_APK_TOOL", stub)
    apk = tmp_path / "tg.apk"
    apk.write_bytes(b"ADBd")
    files = ip.payload_files(apk, "apk")
    assert any(f.endswith("usr/bin/tollgate-wrt") for f in files)
    assert "del_list 8090" in ip.payload_text(apk, "apk", ip.SETUP_SCRIPT)


def test_artifact_policy_readiness_flags_a_release_without_the_guard():
    """pre16 ships neither the guard file nor the port removal — say so loudly."""
    readiness = ip.artifact_policy_readiness(
        ["usr/bin/tollgate-wrt", "etc/nftables.d/20-nds-enforce.nft"], ""
    )
    assert readiness["ships_guard_nft"] is False
    assert readiness["problems"]
    assert ip.GUARD_NFT_BASENAME in readiness["problems"][0]
    assert any("8090" in p for p in readiness["problems"])
    assert any("8443" in p for p in readiness["problems"])


def test_artifact_policy_readiness_passes_when_everything_ships():
    readiness = ip.artifact_policy_readiness(
        ["etc/nftables.d/20-nds-enforce.nft", f"etc/nftables.d/{ip.GUARD_NFT_BASENAME}"],
        GUARD_SETUP_SCRIPT,
    )
    assert readiness["problems"] == []
    assert readiness["ships_guard_nft"] and all(readiness["setup_removes_admin_ports"].values())


def test_version_violations():
    assert ip.version_violations("tollgate-wrt-0.6.0_alpha4_pre16-r1", "tollgate-wrt_0.6.0_alpha4_pre16_aarch64_cortex-a53.apk") == []
    assert ip.version_stem_from_filename("tollgate-wrt_0.6.0_alpha4_pre16_aarch64_cortex-a53.ipk") == (
        "0.6.0_alpha4_pre16"
    )
    assert ip.version_stem_from_filename("tollgate-wrt_0.6.0_alpha4_pre16_x86_64.apk") == (
        "0.6.0_alpha4_pre16"
    )
    assert ip.version_violations("tollgate-wrt-0.6.0_alpha4_pre16-r1", "tollgate-wrt_0.6.0_alpha4_pre16_x86_64.apk") == []
    assert ip.version_violations("", "x.apk")
    assert ip.version_violations("tollgate-wrt-0.5.0-r0", "tollgate-wrt_0.6.0_alpha4_pre16_a53.apk")


# ---------------------------------------------------------------------------
# POLICY gate
# ---------------------------------------------------------------------------

UCI_SHOW_FIXTURE = """
nodogsplash.@nodogsplash[0].users_to_router='allow tcp port 22'
nodogsplash.@nodogsplash[0].users_to_router='allow tcp port 23'
nodogsplash.@nodogsplash[0].users_to_router='allow tcp port 53'
nodogsplash.@nodogsplash[0].users_to_router='allow tcp port 67'
nodogsplash.@nodogsplash[0].users_to_router='allow tcp port 80'
nodogsplash.@nodogsplash[0].users_to_router='allow tcp port 443'
nodogsplash.@nodogsplash[0].users_to_router='allow tcp port 2121'
nodogsplash.@nodogsplash[0].users_to_router='allow tcp port 2050'
nodogsplash.@nodogsplash[0].users_to_router='allow tcp port 2051'
nodogsplash.@nodogsplash[0].users_to_router='allow tcp port 8080'
""".strip()

UCI_SHOW_PRE_566 = UCI_SHOW_FIXTURE + "\nnodogsplash.@nodogsplash[0].users_to_router='allow tcp port 8090'\n"


def test_parse_users_to_router_ports():
    assert ip.parse_users_to_router_ports(UCI_SHOW_FIXTURE) == ip.POLICY_ALLOW_PORTS
    assert ip.parse_users_to_router_ports("") == set()


def test_policy_gate_passes_on_the_expected_post_566_state():
    ports = ip.parse_users_to_router_ports(UCI_SHOW_FIXTURE)
    assert ip.policy_violations(ports, guard_present=True, guard_drops_br_lan=True, ssh_alive=True) == []


def test_policy_gate_catches_the_566_regression():
    ports = ip.parse_users_to_router_ports(UCI_SHOW_PRE_566)
    problems = ip.policy_violations(ports, guard_present=True, guard_drops_br_lan=True, ssh_alive=True)
    assert any("forbidden ports [8090]" in p for p in problems)


def test_policy_gate_catches_missing_ports_guard_and_dead_ssh():
    ports = ip.POLICY_ALLOW_PORTS - {22, 443}
    problems = ip.policy_violations(ports, guard_present=False, guard_drops_br_lan=False, ssh_alive=False)
    assert any("missing [22, 443]" in p for p in problems)
    assert any("guard file" in p for p in problems)
    assert any("SSH (port 22)" in p for p in problems)


def test_policy_gate_flags_unexpected_extra_ports():
    ports = set(ip.POLICY_ALLOW_PORTS) | {9999}
    problems = ip.policy_violations(ports, guard_present=True, guard_drops_br_lan=True, ssh_alive=True)
    assert any("unexpected ports [9999]" in p for p in problems)


def test_policy_gate_flags_guard_file_without_the_br_lan_drop():
    problems = ip.policy_violations(
        set(ip.POLICY_ALLOW_PORTS), guard_present=True, guard_drops_br_lan=False, ssh_alive=True
    )
    assert len(problems) == 1 and "does not drop traffic from br-lan" in problems[0]


# ---------------------------------------------------------------------------
# surfaces
# ---------------------------------------------------------------------------

HEALTHY_PROBE = ":2051 200\n:2050 200\n:2121 200\n:8080 307\n:8090 000\n"


def test_parse_surface_probe_and_violations_healthy():
    codes = ip.parse_surface_probe(HEALTHY_PROBE)
    assert codes == {2051: "200", 2050: "200", 2121: "200", 8080: "307", 8090: "000"}
    assert ip.surface_violations(codes) == []


def test_surface_violations_catch_guest_reachable_admin_board_and_3080():
    codes = ip.parse_surface_probe(":2051 200\n:2050 200\n:2121 200\n:8080 200\n:8090 200\n")
    problems = ip.surface_violations(codes)
    assert any(":8080 returned 200" in p for p in problems)
    assert any("admin board is guest-reachable" in p for p in problems)


def test_surface_violations_report_unprobed_ports():
    problems = ip.surface_violations(ip.parse_surface_probe(":2051 200\n"))
    assert any(":2050 not probed" in p for p in problems)
    assert any(":8090 answered" in p for p in problems) is False  # absent -> treated as dropped


# ---------------------------------------------------------------------------
# happy-path suite reuse
# ---------------------------------------------------------------------------


def _fake_playwright_runner(tmp_path):
    """A repo with @playwright/test 'installed': node_modules/.bin/playwright."""
    runner = tmp_path / ip.PLAYWRIGHT_RELATIVE_CLI
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text("#!/bin/sh\n")
    runner.chmod(0o755)
    return tmp_path


def test_happy_path_command_drives_the_existing_spec(tmp_path):
    cmd = ip.happy_path_suite_command(repo_root=_fake_playwright_runner(tmp_path))
    assert ip.HAPPY_PATH_SPEC in cmd
    assert "--grep" in cmd and ip.HAPPY_PATH_GREP in cmd
    assert ip.HAPPY_PATH_PROJECT in " ".join(cmd)
    assert cmd[1] == "test"
    assert cmd[0].endswith(ip.PLAYWRIGHT_RELATIVE_CLI)


def test_playwright_cli_refuses_a_global_cli_and_names_the_fix(tmp_path):
    """`npx playwright test` with only a global playwright fails: say why."""
    with pytest.raises(ip.InstallPathError) as excinfo:
        ip.playwright_cli(tmp_path)
    message = str(excinfo.value)
    assert "npm install" in message
    assert "@playwright/test" in message


def _playwright_report(entries):
    """Build a minimal Playwright JSON report for the given (title, status) pairs."""
    specs = [
        {"title": title, "ok": status == "passed", "tests": [{"status": status}]}
        for title, status in entries
    ]
    return {"suites": [{"title": "cap.spec.mjs", "specs": specs, "suites": []}]}


def test_parse_happy_path_report_all_green():
    report = _playwright_report([(t, "passed") for t in ip.HAPPY_PATH_EXPECTED_TITLES])
    result = ip.parse_happy_path_report(report)
    assert result.total == 4
    assert result.passed == list(ip.HAPPY_PATH_EXPECTED_TITLES)
    assert result.violations() == []


def test_parse_happy_path_report_empty_run_is_not_a_pass():
    result = ip.parse_happy_path_report({"suites": []})
    assert result.total == 0
    problems = result.violations()
    assert problems and "ran 0 tests" in problems[0]


def test_parse_happy_path_report_surfaces_failures_skips_and_missing():
    entries = [
        (ip.HAPPY_PATH_EXPECTED_TITLES[0], "passed"),
        (ip.HAPPY_PATH_EXPECTED_TITLES[1], "failed"),
        (ip.HAPPY_PATH_EXPECTED_TITLES[2], "skipped"),
    ]
    result = ip.parse_happy_path_report(_playwright_report(entries))
    problems = result.violations()
    joined = " ".join(problems)
    assert "failures:" in joined
    assert "were skipped" in joined
    assert "missing from the run" in joined
    # the skip-only downgrade is explicit and opt-in
    assert len(result.violations(allow_skip=True)) == 2


def test_parse_happy_path_report_flattens_nested_suites():
    nested = {
        "suites": [
            {
                "title": "file",
                "suites": [
                    {
                        "title": "describe",
                        "specs": [{"title": ip.HAPPY_PATH_EXPECTED_TITLES[0], "tests": [{"status": "passed"}]}],
                    }
                ],
            }
        ]
    }
    result = ip.parse_happy_path_report(nested)
    assert result.total == 1 and result.passed == [ip.HAPPY_PATH_EXPECTED_TITLES[0]]


# ---------------------------------------------------------------------------
# install + probe command builders
# ---------------------------------------------------------------------------


def test_install_commands_carry_the_required_flags():
    assert ip.apk_install_command("/tmp/tg.apk") == (
        "apk add --no-check-certificate --allow-untrusted /tmp/tg.apk"
    )
    assert ip.install_command("opkg", "/tmp/tg.ipk") == "opkg install --force-overwrite /tmp/tg.ipk"
    assert ip.install_command("apk", "/tmp/tg.apk").startswith("apk add --no-check-certificate")
    with pytest.raises(ip.InstallPathError):
        ip.install_command("rpm", "/tmp/x")


def test_version_and_hash_commands_are_package_manager_specific():
    assert ip.package_version_command("apk").startswith("apk info -v")
    assert ip.package_version_command("opkg").startswith("opkg list-installed")
    assert ip.installed_binary_hash_command() == "sha256sum /usr/bin/tollgate-wrt 2>/dev/null"
    assert "DISTRIB_ARCH" in ip.openwrt_release_command()


def test_surface_probe_snippet_probes_every_port_and_never_pings():
    snippet = ip.SURFACE_PROBE_SNIPPET
    for port in ("2051", "2050", "2121", "8080", "8090"):
        assert port in snippet
    assert "ping" not in snippet


def test_installer_command_is_felixs_documented_form():
    cmd = ip.installer_command("192.168.1.1", "lab-secret", "felix@coinos.io")
    assert cmd[0] == "bash" and cmd[1] == "-c"
    shell = cmd[2]
    assert shell.startswith(f"bash <(curl -fsSL {ip.INSTALLER_SCRIPT_URL})")
    # the default release is the policy-assertable one (pre17), never a silent pin
    assert f"--tag {ip.FEED_RELEASE_DEFAULT}" in shell
    assert ip.release_at_least(ip.FEED_RELEASE_DEFAULT, ip.POLICY_MIN_RELEASE_DEFAULT)
    assert shell.endswith("192.168.1.1 lab-secret felix@coinos.io")
    # an explicit tag still wins
    pinned = ip.installer_command("192.168.1.1", "lab-secret", "f@c.io", tag="v0.6.0-alpha4-pre16")[2]
    assert "--tag v0.6.0-alpha4-pre16" in pinned


def test_installer_command_quotes_a_password_with_specials():
    import shlex

    shell = ip.installer_command("192.168.1.1", "p@ss'word", "a@b.c")[2]
    # the shlex-escaped password survives a round-trip through shlex.split
    assert "p@ss'word" in shlex.split(shell)
    assert "a@b.c" in shlex.split(shell)


def test_installer_supports_tag_detection():
    assert ip.installer_supports_tag("        --tag)     FEED_TAG_OVERRIDE=\"${2:-}\" ;;\n")
    assert ip.installer_supports_tag("  --tag <tag>        Install an exact feed release tag")
    assert not ip.installer_supports_tag("ROUTER_IP=\"${1:-}\"\nROUTER_PASS=\"${2:-}\"\n")


def test_tcp_probe_and_is_ipv4_helpers():
    # TEST-NET-1, nothing listens there: probe returns False, never raises
    assert ip.tcp_probe("192.0.2.1", 9, timeout=0.3) is False
    assert ip.is_ipv4("192.168.1.1") and not ip.is_ipv4("not-an-ip")


# ---------------------------------------------------------------------------
# apk-tools smoke: the real apk.static path (skipped when the tool is absent)
# ---------------------------------------------------------------------------


def test_real_apk_extract_when_tool_available(tmp_path):
    """End-to-end proof that our apk extraction invocation is correct.

    Skipped unless an apk binary and a real .apk artifact are available — the
    dry-run report records the executed version of this check.
    """
    artifact = os.environ.get("TOLLGATE_APK_ARTIFACT", "")
    if not artifact or not os.path.isfile(artifact):
        pytest.skip("set TOLLGATE_APK_ARTIFACT=<published .apk> to run this")
    try:
        digest = ip.binary_sha256_from_apk(artifact)
    except ip.InstallPathError as exc:
        pytest.skip(str(exc))
    assert len(digest) == 64
    assert digest == ip.binary_sha256_from_apk(artifact)  # deterministic


def test_apk_tarball_is_not_extractable_with_plain_tar(tmp_path):
    """Documents *why* apk-tools is required: tar cannot read the ADB format."""
    artifact = os.environ.get("TOLLGATE_APK_ARTIFACT", "")
    if not artifact or not os.path.isfile(artifact):
        pytest.skip("set TOLLGATE_APK_ARTIFACT=<published .apk> to run this")
    with open(artifact, "rb") as handle:
        magic = handle.read(4)
    assert magic == b"ADBd", f"expected apk v3 magic, got {magic!r}"
    result = subprocess.run(
        ["tar", "-tzf", artifact], capture_output=True, text=True, timeout=30
    )
    assert result.returncode != 0
    assert "gzip" in (result.stderr or "").lower() or "tar archive" in (result.stderr or "")


def test_report_json_roundtrip(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"numTotalTests": 4}))
    assert ip.parse_report_json(path) == {"numTotalTests": 4}
