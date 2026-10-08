import pytest
import requests

pytestmark = [pytest.mark.story("R1"),pytest.mark.rust_basic_only, pytest.mark.api, pytest.mark.smoke]


def test_discovery_returns_kind_10021(rust_basic_server):
    """S1: GET / returns 200 with a valid Nostr kind 10021 advertisement event."""
    resp = requests.get(f"{rust_basic_server['http_url']}/", timeout=5)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    assert data["kind"] == 10021, f"Expected kind 10021, got {data.get('kind')}"
    assert len(data.get("pubkey", "")) == 64, "pubkey must be 64 hex chars"
    assert len(data.get("sig", "")) == 128, "sig must be 128 hex chars"
    tag_names = {t[0] for t in data.get("tags", []) if isinstance(t, list) and t}
    assert "metric" in tag_names, f"Missing metric tag: {data.get('tags')}"
    assert "step_size" in tag_names, "Missing step_size tag"
    assert "price_per_step" in tag_names, "Missing price_per_step tag"
    assert "tips" in tag_names, "Missing tips tag"


# ---------------------------------------------------------------------------
# CORS tests — aligned with Go module policy (tollgate-module-basic-go #349)
# ---------------------------------------------------------------------------
#
# The Go module's CorsMiddleware (post-#349) echoes the Origin header only
# for local/private or same-host origins, never a wildcard. Vary: Origin is
# added on every echo. Cross-host and null origins get no ACAO header.
#
# The Rust basic binary (tollgate-module-basic-rust) currently emits
# Access-Control-Allow-Origin: * on every route — both via the tower_http
# CorsLayer(Any) in src/http/mod.rs and via per-route hardcoded header tuples.
# This is a security disagreement: the API is protected by the LAN firewall,
# not credentials, so a wildcard lets any website read API responses from a
# browser on the TollGate network (OWASP guidance for network-location-
# protected services).
#
# The tests below assert the Go-aligned policy. They are xfail-marked against
# the current Rust binary to document the desired behavior without breaking
# CI. Once tollgate-module-basic-rust implements origin echo (see follow-up
# issue), remove the xfail markers.
#
# See: https://github.com/OpenTollGate/physical-router-test-automation/issues/88
# See: Go commit 5bdc549 (fix(cors): echo only local/same-host origins)


@pytest.mark.xfail(
    reason="Rust binary emits ACAO: * — Go-aligned origin-echo policy not yet "
           "implemented (tollgate-module-basic-rust follow-up issue)",
    strict=True,
)
def test_cors_echoes_local_origin(rust_basic_server):
    """CORS: GET / with a private-LAN Origin echoes that origin + Vary: Origin."""
    url = rust_basic_server["http_url"]
    resp = requests.get(
        f"{url}/",
        headers={"Origin": "http://127.0.0.1:2050"},
        timeout=5,
    )
    assert resp.status_code == 200
    aco = resp.headers.get("access-control-allow-origin", "")
    assert aco == "http://127.0.0.1:2050", (
        f"Expected local origin echoed, got {aco!r}"
    )
    # Vary: Origin must be present whenever the origin is echoed (MDN caching guidance)
    vary = resp.headers.get("vary", "")
    assert "Origin" in vary, f"Expected Vary: Origin on echo, got {vary!r}"


@pytest.mark.xfail(
    reason="Rust binary emits ACAO: * — Go-aligned origin-echo policy not yet "
           "implemented (tollgate-module-basic-rust follow-up issue)",
    strict=True,
)
def test_cors_no_acao_for_cross_host_origin(rust_basic_server):
    """CORS: GET / with a cross-host Origin must not set Access-Control-Allow-Origin."""
    url = rust_basic_server["http_url"]
    resp = requests.get(
        f"{url}/",
        headers={"Origin": "https://evil.example.com"},
        timeout=5,
    )
    assert resp.status_code == 200
    aco = resp.headers.get("access-control-allow-origin", "")
    assert aco == "", (
        f"Cross-host origin must not get ACAO, got {aco!r}"
    )


@pytest.mark.xfail(
    reason="Rust binary emits ACAO: * — Go-aligned origin-echo policy not yet "
           "implemented (tollgate-module-basic-rust follow-up issue)",
    strict=True,
)
def test_cors_no_acao_for_null_origin(rust_basic_server):
    """CORS: GET / with Origin: null must never be echoed."""
    url = rust_basic_server["http_url"]
    resp = requests.get(
        f"{url}/",
        headers={"Origin": "null"},
        timeout=5,
    )
    assert resp.status_code == 200
    aco = resp.headers.get("access-control-allow-origin", "")
    assert aco == "", f"null origin must never be echoed, got {aco!r}"


@pytest.mark.xfail(
    reason="Rust binary emits ACAO: * — Go-aligned origin-echo policy not yet "
           "implemented (tollgate-module-basic-rust follow-up issue)",
    strict=True,
)
def test_cors_no_acao_without_origin_header(rust_basic_server):
    """CORS: GET / without Origin header must not set Access-Control-Allow-Origin.

    Non-browser clients (curl) never need ACAO; the wildcard fallback that the
    Rust binary currently emits serves no legitimate client.
    """
    url = rust_basic_server["http_url"]
    resp = requests.get(f"{url}/", timeout=5)
    assert resp.status_code == 200
    aco = resp.headers.get("access-control-allow-origin", "")
    assert aco == "", f"no-origin request must not get ACAO, got {aco!r}"


@pytest.mark.xfail(
    reason="Rust binary emits ACAO: * — Go-aligned same-host-any-port policy not "
           "yet implemented (tollgate-module-basic-rust follow-up issue)",
    strict=True,
)
def test_cors_same_host_any_port_echoed(rust_basic_server):
    """CORS: same-host different-port Origin is echoed (portal-on-:2051 case).

    The captive portal is served from uhttpd :2051 while the API stays on :2121.
    The Go module's isSameHost() rule allows this because the host matches
    regardless of port.
    """
    url = rust_basic_server["http_url"]
    # 127.0.0.1:2121 is the API; 127.0.0.1:2051 is the portal
    resp = requests.get(
        f"{url}/",
        headers={"Origin": "http://127.0.0.1:2051"},
        timeout=5,
    )
    assert resp.status_code == 200
    aco = resp.headers.get("access-control-allow-origin", "")
    assert aco == "http://127.0.0.1:2051", (
        f"Same-host any-port origin should be echoed, got {aco!r}"
    )
    vary = resp.headers.get("vary", "")
    assert "Origin" in vary, f"Expected Vary: Origin on echo, got {vary!r}"


# ---------------------------------------------------------------------------
# Current-binary-behavior test (documents what the Rust binary does TODAY)
# ---------------------------------------------------------------------------
# This test PASSES against the current binary and documents the wildcard
# behavior. Once the Rust binary is fixed to echo origins, this test should
# be removed along with the xfail markers above.


def test_cors_current_binary_emits_wildcard(rust_basic_server):
    """Document current Rust binary CORS behavior: emits Access-Control-Allow-Origin: *.

    This is a known security disagreement with the Go module (see issue #88
    and Go commit 5bdc549). The Rust binary uses tower_http CorsLayer(Any)
    plus per-route hardcoded ``("access-control-allow-origin", "*")`` header
    tuples in every route handler.

    Once tollgate-module-basic-rust implements Go-aligned origin echo,
    remove this test and un-xfail the policy tests above.
    """
    resp = requests.get(f"{rust_basic_server['http_url']}/", timeout=5)
    assert resp.status_code == 200
    aco = resp.headers.get("access-control-allow-origin", "")
    assert aco == "*", (
        f"Current binary emits wildcard; if this fails, the binary may have "
        f"been updated to echo origins — remove this test and un-xfail the "
        f"Go-aligned policy tests. Got {aco!r}"
    )