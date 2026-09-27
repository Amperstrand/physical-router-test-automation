"""
Test balance page reachability after payment.

BUG: After successful Cashu payment, the net4sats balance page doesn't load.

ROOT CAUSE: The captive portal SPA (captive-portal.tsx) redirects to
`http://${window.location.hostname}/net4sats/balance.html` which defaults
to port 80. On GL-MT3000 with the deployed configuration:

  - Port 80: NO LISTENER (uhttpd main listens on :443 only)
  - Port 2050: nodogsplash gateway
  - Port 2051: uhttpd portal (captive portal SPA)
  - Port 8080: uhttpd luci (admin dashboard)
  - Port 8090: uhttpd net4sats (balance.html lives here)
  - Port 2121: tollgate-wrt backend API

The redirect goes to port 80 which has NO web server listening.
The connection fails and the user sees nothing after payment.

Additionally, OS captive portal mini-browsers (iOS/Android) auto-close
within 1-2 seconds of detecting internet connectivity. The SPA's 3-second
setTimeout before redirect may never fire.
"""
import pytest
import re

pytestmark = [pytest.mark.go_only, pytest.mark.api]


def _net4sats_layout_present(router) -> bool:
    """True iff the net4sats (configwizzard) deployment layout is installed.

    /www/net4sats/balance.html plus its :8090 uhttpd instance come from the
    configwizzard deployment (scripts/deploy-configwizzard.sh, cloud lab
    --portal net4sats) — not from the ipk, which ships the portal SPA to
    /etc/tollgate/tollgate-captive-portal-site/ served by uhttpd :2051.
    """
    result = router.ssh("test -f /www/net4sats/balance.html && echo YES || echo NO", timeout=10)
    return result.strip() == "YES"


@pytest.fixture(autouse=True)
def _skip_if_no_net4sats_portal(router):
    if not _net4sats_layout_present(router):
        pytest.skip(
            "net4sats (configwizzard) portal layout not deployed — "
            "builtin portal serves the SPA from uhttpd :2051 (PRTA #103)"
        )


@pytest.mark.api
class TestBalancePageReachable:
    """Verify the balance page is reachable at the URL the SPA redirects to."""

    def test_balance_html_exists_on_router(self, router):
        """balance.html must exist in /www/net4sats/ on the router."""
        result = router.ssh(
            "ls -la /www/net4sats/balance.html 2>&1", timeout=10
        )
        assert "No such file" not in result, (
            f"balance.html not found at /www/net4sats/balance.html\n{result}"
        )

    def test_port_80_has_listener(self, router):
        """Port 80 must have a web server listening for the balance page redirect to work.

        The captive portal SPA redirects to http://<hostname>/net4sats/balance.html
        which uses the default HTTP port 80. If nothing listens on port 80,
        the redirect fails silently.
        """
        result = router.ssh(
            "netstat -tlnp 2>/dev/null | grep ':80 ' || echo 'NO_LISTENER'",
            timeout=10
        )
        assert "NO_LISTENER" not in result, (
            "No web server listening on port 80. "
            "The captive portal SPA redirects to http://<host>/net4sats/balance.html "
            "(port 80) but nothing is listening there. "
            "uhttpd instances: check 'uci show uhttpd | grep listen'. "
            "Fix: either add a uhttpd listener on :80, "
            "or change the SPA redirect to use the correct port (:8090)."
        )

    def test_balance_page_reachable_on_port_80(self, router):
        """balance.html should be reachable at http://<router>:80/net4sats/balance.html."""
        host = router.host
        result = router.ssh(
            f"wget -q -O /dev/null -T 3 http://127.0.0.1:80/net4sats/balance.html 2>&1; echo EXIT:$?",
            timeout=10
        )
        exit_code = result.strip().split("EXIT:")[-1].strip()
        assert exit_code == "0", (
            f"Cannot reach balance.html on port 80 (wget exit={exit_code}). "
            f"No web server on port 80 makes the post-payment redirect fail."
        )

    def test_balance_page_reachable_on_port_8090(self, router):
        """balance.html should be reachable at http://<router>:8090/balance.html."""
        result = router.ssh(
            "wget -q -O /dev/null -T 3 http://127.0.0.1:8090/balance.html 2>&1; echo EXIT:$?",
            timeout=10
        )
        exit_code = result.strip().split("EXIT:")[-1].strip()
        assert exit_code == "0", (
            f"Cannot reach balance.html on port 8090 (wget exit={exit_code})."
        )

    def test_balance_page_reachable_on_portal_port(self, router):
        """balance.html should also be reachable from the portal uhttpd instance on :2051."""
        result = router.ssh(
            "wget -q -O /dev/null -T 3 http://127.0.0.1:2051/balance.html 2>&1; echo EXIT:$?",
            timeout=10
        )
        exit_code = result.strip().split("EXIT:")[-1].strip()
        # balance.html may or may not be in the portal home dir
        # The portal home is /etc/tollgate/net4sats-captive-portal-site/
        if exit_code != "0":
            pytest.skip(
                "balance.html not served from portal :2051 home dir — "
                "it lives in /www/net4sats/ (served by :8090) not in portal home"
            )


@pytest.mark.api
class TestPostPaymentRedirect:
    """Test the redirect mechanism the SPA uses after successful payment."""

    def test_spa_redirect_url_uses_correct_port(self, router):
        """The captive portal SPA must redirect to a port with a listener.

        The SPA code (captive-portal.tsx) does:
            window.location.href = `http://${window.location.hostname}/net4sats/balance.html`;

        This defaults to port 80. Verify port 80 has a listener.
        If not, the redirect URL in the SPA must be updated.
        """
        # Check which ports have listeners
        listeners = router.ssh(
            "netstat -tlnp 2>/dev/null | grep -oP ':\d+' | sort -t: -k2 -n | uniq",
            timeout=10
        )
        has_80 = ":80 " in router.ssh("netstat -tlnp 2>/dev/null | grep ':80 '", timeout=10)

        if not has_80:
            # Check what ports DO have listeners
            http_ports = []
            for line in listeners.strip().split("\n"):
                port = line.strip().lstrip(":")
                if port.isdigit() and int(port) < 10000:
                    http_ports.append(port)

            pytest.fail(
                f"The captive portal SPA redirects to port 80 but no listener exists. "
                f"Available HTTP ports: {http_ports}. "
                f"The SPA redirect URL must include the correct port "
                f"(likely :8090 for /net4sats/balance.html)."
            )

    def test_uhttpd_instances_configured(self, router):
        """Verify uhttpd instances and their home directories."""
        config = router.ssh("uci show uhttpd 2>/dev/null", timeout=10)

        # Parse uhttpd sections
        instances = {}
        current_section = None
        for line in config.strip().split("\n"):
            m = re.match(r"uhttpd\.(\w+)\.(\w+)='(.+)'", line)
            if m:
                section, key, val = m.groups()
                if section not in instances:
                    instances[section] = {}
                instances[section][key] = val

        # Verify essential instances exist
        assert "main" in instances, "uhttpd main instance missing"
        assert "portal" in instances or "net4sats" in instances, (
            "Neither portal (:2051) nor net4sats (:8090) uhttpd instance found"
        )

        # Check if any instance listens on port 80
        has_port_80 = False
        for section, cfg in instances.items():
            listen = cfg.get("listen_http", "")
            if ":80" in listen or ":80 " in listen:
                has_port_80 = True
                break

        if not has_port_80:
            # This is the bug — document it clearly
            all_listens = []
            for section, cfg in instances.items():
                listen = cfg.get("listen_http", cfg.get("listen_https", ""))
                home = cfg.get("home", "?")
                all_listens.append(f"  {section}: {listen} (home={home})")

            pytest.fail(
                f"No uhttpd instance listens on port 80.\n"
                f"Instances:\n" + "\n".join(all_listens) + "\n"
                f"The SPA redirect to http://<host>/net4sats/balance.html "
                f"will fail — nothing serves port 80."
            )

    def test_captive_portal_spa_redirect_timing(self, router):
        """The SPA success→redirect timeout should be shorter than OS captive portal browser auto-close.

        OS captive portal browsers (iOS/Android) auto-close 1-2s after detecting
        connectivity. The SPA uses a 3-second setTimeout before redirecting.
        This means the redirect may never fire on real devices.

        This is a design issue, not a runtime bug — documented for awareness.
        """
        # This test documents the known issue
        portal_js = router.ssh(
            "ls /etc/tollgate/net4sats-captive-portal-site/assets/splash-*.js 2>/dev/null",
            timeout=10
        ).strip()

        if not portal_js:
            pytest.skip("Cannot find portal JS bundle to inspect")

        # Check if the redirect timeout exists in the JS
        js_content = router.ssh(f"cat {portal_js} 2>/dev/null", timeout=10)

        # Look for setTimeout with 3000 (3 seconds)
        has_3s_timeout = "3000" in js_content and "balance" in js_content.lower()

        if has_3s_timeout:
            import warnings
            warnings.warn(
                "Captive portal SPA uses 3-second redirect timeout. "
                "OS captive portal browsers auto-close within 1-2 seconds. "
                "Consider reducing timeout or showing balance inline."
            )
