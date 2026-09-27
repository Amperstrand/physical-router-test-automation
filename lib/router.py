"""
Router SSH interaction — delegates to tollgate_lab when available.

The canonical implementation lives in tollgate_lab/drivers/router.py.
This file provides backward-compatible imports for existing tests.

To use tollgate_lab directly:
    from tollgate_lab.drivers.router import Router, BackendConfig
"""

# Re-export from tollgate_lab for new code
try:
    from tollgate_lab.drivers.router import Router, BackendConfig
except ImportError:
    pass  # Fall through to local implementation below

import subprocess
import json
import os
import tempfile
import time
import re
import logging
import shlex

from lib.constants import BACKEND_PORT, CGI_PORT, TEST_MINT_URL
from lib.backend import BackendConfig

log = logging.getLogger("tollgate.router")


class Router:
    def __init__(self, host: str, phone_ip: str, phone_mac: str, domain: str,
                 identity_file: str | None = None, jump_host: str | None = None,
                 port: int | None = None, backend: BackendConfig | None = None):
        self.host = host
        self.phone_ip = phone_ip
        self.phone_mac = phone_mac
        self.domain = domain
        self.identity_file = identity_file
        # Normalize: localhost jump hosts are meaningless (same machine).
        if jump_host and jump_host in {"localhost", "127.0.0.1", "::1"}:
            jump_host = None
        self.jump_host = jump_host
        self.port = port
        self.backend = backend or BackendConfig()
        self._ssh_pw = os.environ.get("TOLLGATE_SSH_PASSWORD") or os.environ.get("TOLLGATE_LUCI_PASSWORD")

        self._control_dir = tempfile.mkdtemp(prefix="tollgate-ssh-")
        self._control_path = os.path.join(self._control_dir, "control")

        ssh_opts = [
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", f"ControlPath={self._control_path}",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=60",
        ]
        if not identity_file and self._ssh_pw:
            self._ssh_base = ["sshpass", "-p", self._ssh_pw, "ssh"] + ssh_opts
        else:
            self._ssh_base = ["ssh"] + ssh_opts
        if port:
            self._ssh_base.extend(["-p", str(port)])
        if identity_file:
            self._ssh_base.extend(["-i", identity_file])
        if jump_host:
            self._ssh_base.extend(["-J", jump_host])
        self._ssh_base.append(f"root@{host}")

    def close(self):
        try:
            subprocess.run(
                ["ssh", "-o", f"ControlPath={self._control_path}", "-O", "exit", f"root@{self.host}"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
        try:
            os.remove(self._control_path)
        except FileNotFoundError:
            pass
        try:
            os.rmdir(self._control_dir)
        except OSError:
            pass

    def resolve_phone_client(self, adb) -> tuple:
        mac = adb.wifi_mac()
        if mac:
            self.phone_mac = mac
        ip = adb.wifi_ip()
        if mac and ip:
            self.phone_ip = ip
            log.info(f"Phone auto-detected: MAC={mac} IP={ip}")
            return mac, ip
        if mac:
            try:
                leases = self.ssh("cat /tmp/dhcp.leases 2>/dev/null")
                for line in leases.strip().split("\n"):
                    fields = line.split()
                    if len(fields) >= 3 and fields[1].lower() == mac.lower():
                        self.phone_ip = fields[2]
                        log.info(f"Phone from DHCP lease: MAC={mac} IP={fields[2]}")
                        return mac, fields[2]
            except Exception:
                pass
        return self.phone_mac, self.phone_ip

    @property
    def gateway_ip(self) -> str:
        if self.domain:
            return self.domain
        gw = self._detect_gateway()
        if gw:
            return gw
        return self.host

    def _detect_gateway(self) -> str:
        try:
            out = self.ssh("ip -4 route show default 2>/dev/null | awk '{print $3}'")
            if out and not out.startswith("Usage"):
                return out.split("\n")[0].strip()
        except Exception:
            pass
        return ""

    def backend_url(self, path="/"):
        host = "127.0.0.1" if self.backend.is_rust_family else "[::1]"
        return f"http://{host}:{BACKEND_PORT}{path}"

    def get_nds_portal_port(self) -> int:
        """NDS gatewayport from UCI, cached. Falls back to 2050."""
        if not hasattr(self, '_nds_portal_port'):
            try:
                port = self.ssh(
                    "uci -q get nodogsplash.@nodogsplash[0].gatewayport"
                ).strip()
                self._nds_portal_port = int(port) if port else 2050
            except Exception:
                logging.warning("UCI query for nodogsplash gatewayport failed, assuming 2050")
                self._nds_portal_port = 2050
        return self._nds_portal_port

    def get_nds_gateway_domain(self) -> str:
        """NDS gatewaydomainname from UCI, cached. Empty string if not set."""
        if not hasattr(self, '_nds_gateway_domain'):
            try:
                domain = self.ssh(
                    "uci -q get nodogsplash.@nodogsplash[0].gatewaydomainname"
                ).strip()
                self._nds_gateway_domain = domain
            except Exception:
                self._nds_gateway_domain = ""
        return self._nds_gateway_domain

    def ensure_nds_gateway_domain_supported(self):
        """Patch /etc/init.d/nodogsplash to include gatewaydomainname if missing."""
        if self.ssh("grep -q gatewaydomainname /etc/init.d/nodogsplash").strip():
            return
        self.ssh(
            "sed -i 's/gatewayaddress gatewayport/gatewayaddress gatewayport gatewaydomainname/' "
            "/etc/init.d/nodogsplash"
        )
        self.ssh("/etc/init.d/nodogsplash restart")

    def _detect_cgi_port(self) -> int:
        """Auto-detect the NDS gateway port serving CGI scripts."""
        try:
            out = self.ssh(
                "netstat -tlnp 2>/dev/null | grep nodogsplash | head -1"
            )
            if out and ":" in out:
                port = out.split(":")[1].split()[0]
                return int(port)
        except Exception:
            pass
        return CGI_PORT

    def cgi_url(self, endpoint):
        port = self._detect_cgi_port()
        return f"http://127.0.0.1:{port}/cgi-bin/{endpoint}"

    def router_fetch(self, url: str, method: str = "GET", data: str | None = None, timeout: int = 10) -> str:
        if data is not None:
            return self.ssh(f"wget -qO- --post-data='{data}' '{url}'", timeout=timeout)
        return self.ssh(f"wget -qO- '{url}'", timeout=timeout)

    def router_fetch_status(self, url: str, timeout: int = 10) -> str:
        out = self.ssh(f"wget --spider '{url}' 2>&1", timeout=timeout)
        if "HTTP error" in out:
            import re
            m = re.search(r'HTTP error (\d{3})', out)
            if m:
                return m.group(1)
        if "Download completed" in out or "Writing to" in out:
            return "200"
        if "Failed" in out or "timed out" in out or "Connection refused" in out:
            return "000"
        return ""

    def _ssh_env(self):
        env = os.environ.copy()
        if self._ssh_pw:
            env["SSHPASS"] = self._ssh_pw
        return env

    def _kill_control_master(self):
        """Tear down a (possibly wedged) SSH control master.

        A hung ControlMaster blocks every multiplexed client until each
        client's own timeout fires — one wedged master cascaded into 225
        suite errors on 2026-09-05 (integ/local-lab-green run 1) while the
        router itself stayed healthy and fresh connections worked.
        ``ssh -O exit`` is the polite teardown; removing the socket covers
        a master that ignores it.
        """
        try:
            subprocess.run(
                ["ssh", "-o", f"ControlPath={self._control_path}", "-O", "exit",
                 f"root@{self.host}"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
        try:
            os.remove(self._control_path)
        except OSError:
            pass
        log.warning("SSH control master torn down after timeout; retrying on a fresh connection")

    def ssh(self, cmd: str, timeout: int = 30) -> str:
        try:
            r = subprocess.run(
                self._ssh_base + [cmd],
                capture_output=True, text=True, timeout=timeout,
                env=self._ssh_env(),
            )
        except subprocess.TimeoutExpired:
            # The client timed out, but the wedged ControlMaster is the
            # culprit: kill it and retry once on a fresh connection.
            self._kill_control_master()
            r = subprocess.run(
                self._ssh_base + [cmd],
                capture_output=True, text=True, timeout=timeout,
                env=self._ssh_env(),
            )
        if r.returncode != 0:
            noise = re.compile(r"Warning:.*Permanently added[^\n]*")
            cleaned = noise.sub("", r.stderr).strip()
            if cleaned:
                log.warning("ssh returned %d: %s", r.returncode, cleaned[:200])
        out = r.stdout.strip()
        return re.sub(r"Warning:.*Permanently added[^\n]*\n?", "", out).strip()

    def write_remote_text(self, remote_path: str, content: str, timeout: int = 15):
        result = self.ssh_stdin(f"cat > {shlex.quote(remote_path)}", content, timeout=timeout)
        if result.returncode == 0:
            return
        noise = re.compile(r"Warning:.*Permanently added[^\n]*")
        cleaned = noise.sub("", result.stderr).strip()
        raise RuntimeError(
            f"Failed to write {remote_path} ({result.returncode}): {cleaned[:300]}"
        )

    def write_remote_json(self, remote_path: str, payload, indent: int = 2, timeout: int = 15):
        self.write_remote_text(remote_path, json.dumps(payload, indent=indent), timeout=timeout)

    def ssh_stdin(self, cmd: str, data: str, timeout: int = 15):
        try:
            return subprocess.run(
                self._ssh_base + [cmd],
                input=data, capture_output=True, text=True, timeout=timeout,
                env=self._ssh_env(),
            )
        except subprocess.TimeoutExpired:
            self._kill_control_master()
            return subprocess.run(
                self._ssh_base + [cmd],
                input=data, capture_output=True, text=True, timeout=timeout,
                env=self._ssh_env(),
            )

    def scp_to(self, local_path: str, remote_path: str, timeout: int = 120):
        ssh_opts = [
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
        ]
        pw = os.environ.get("TOLLGATE_SSH_PASSWORD") or os.environ.get("TOLLGATE_LUCI_PASSWORD")
        if self.identity_file:
            cmd = ["scp", "-O", "-i", self.identity_file] + ssh_opts
        elif pw:
            cmd = ["sshpass", "-e", "scp", "-O"] + ssh_opts
        else:
            cmd = ["scp", "-O"] + ssh_opts
        if self.jump_host:
            cmd += ["-J", self.jump_host]
        if self.port:
            cmd += ["-P", str(self.port)]
        cmd += [str(local_path), f"root@{self.host}:{remote_path}"]
        env = os.environ.copy()
        if pw:
            env["SSHPASS"] = pw
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"SCP failed ({r.returncode}): {r.stderr.strip()[:300]}")


    def fix_nodogsplash_dhcp(self):
        """Ensure nodogsplash allows DHCP through its ndsRTR chain.

        Nodogsplash's ndsRTR chain drops ALL unauthenticated packets (mark
        0x10000) at rule 1, which silently kills DHCP DISCOVER from clients
        before they reach the port-67 ACCEPT rule further down the chain.
        Without this fix, phones can associate at L2 but never get an IP,
        causing Android to show "Connection failed" and auto-reconnect to
        a known-good network.
        """
        try:
            out = self.ssh("iptables -L ndsRTR -n 2>/dev/null", timeout=10)
            if "udp dpt:67" in out:
                # Check if DHCP accept is BEFORE the mark-based drop
                lines = out.strip().split("\n")
                dhcp_line = None
                drop_line = None
                for i, line in enumerate(lines):
                    if "udp dpt:67" in line and dhcp_line is None:
                        dhcp_line = i
                    if "0x10000" in line and drop_line is None:
                        drop_line = i
                if dhcp_line is not None and drop_line is not None and dhcp_line < drop_line:
                    log.debug("ndsRTR DHCP bypass already in place")
                    return
            self.ssh(
                "iptables -I ndsRTR 1 -p udp --dport 67 -j ACCEPT && "
                "iptables -I ndsRTR 1 -p udp --dport 68 -j ACCEPT",
                timeout=10,
            )
            log.info("Inserted DHCP bypass rules in ndsRTR chain")
        except Exception as e:
            log.warning(f"Could not fix nodogsplash DHCP: {e}")

    def fix_nodogsplash_auth_marks(self, ip: str | None = None, mac: str | None = None):
        """Repair NDS 5.0.2 auth-mark gating so authenticated clients can open
        NEW connections.

        On `ndsctl auth`, nodogsplash 5.0.2 marks the client's packets 0x30000
        (both the 0x10000 preauth and 0x20000 auth bits), but the accept rule
        in `filter ndsNET` tests `--mark 0x20000/0x30000` — a 0x30000-marked
        packet can never match, so it falls through ndsAUT to the catch-all
        REJECT. Payments succeed, `ndsctl status` shows Authenticated, and
        the client still cannot open new connections.

        Fix: insert one client-agnostic accept rule at the top of ndsNET
        matching the auth bit (`--mark 0x20000/0x20000`). Rewriting the
        per-client ndsOUT rule instead leaks: NDS cannot delete a rule it
        did not insert, so `ndsctl deauth` leaves the corrected mark in
        place and the client keeps internet access forever. With the
        ndsNET rule, NDS's own insert/remove lifecycle is untouched — when
        deauth removes the client's mark rule, packets lose the auth bit
        and are gated again.

        `ip`/`mac` are accepted for signature compatibility and restrict the
        0x30000 bug detection to one client; the inserted accept rule is
        always client-agnostic (it only matches packets NDS itself marked).
        """
        accept_rule = "-m mark --mark 0x20000/0x20000 -j ACCEPT"
        try:
            net = self.ssh("iptables -S ndsNET 2>/dev/null", timeout=10)
            if accept_rule in net:
                log.debug("ndsNET auth-bit accept rule already present")
                return
            out = self.ssh("iptables -t mangle -S ndsOUT 2>/dev/null", timeout=10)
            rules = [
                line.strip()
                for line in out.split("\n")
                if line.strip().startswith("-A ndsOUT") and "0x30000" in line
            ]
            if ip or mac:
                rules = [
                    r for r in rules
                    if (ip is None or f"-s {ip}" in r)
                    and (mac is None or f"--mac-source {mac}" in r)
                ]
            if not rules:
                log.debug("ndsOUT has no 0x30000-marked auth rules (fixed or NDS < 5.0.2)")
                return
            self.ssh(f"iptables -I ndsNET 1 {accept_rule}", timeout=10)
            log.info(
                "Inserted ndsNET auth-bit accept rule (%d 0x30000-marked client rule(s) present)",
                len(rules),
            )
        except Exception as e:
            log.warning(f"Could not fix nodogsplash auth marks: {e}")

    def disable_ipv6_on_lan(self):
        """Disable IPv6 on the LAN interface to prevent captive portal bypass.

        Nodogsplash only manages IPv4 iptables. If IPv6 Router Advertisements
        are active, WiFi clients get global IPv6 addresses and Android validates
        connectivity over IPv6, completely bypassing the captive portal.
        """
        try:
            self.ssh(
                "uci set dhcp.lan.ra='disabled' && "
                "uci set dhcp.lan.dhcpv6='disabled' && "
                "uci set network.lan.ip6assign='0' && "
                "uci commit dhcp && uci commit network",
                timeout=10,
            )
            # Flush any existing global IPv6 addresses from br-lan
            self.ssh(
                "ip -6 addr show br-lan scope global | grep inet6 | "
                "awk '{print $2}' | while read addr; do "
                "ip addr del \"$addr\" dev br-lan 2>/dev/null; done",
                timeout=10,
            )
            log.info("IPv6 disabled on LAN (RA, DHCPv6, ip6assign=0)")
        except Exception as e:
            log.warning(f"Could not disable IPv6 on LAN: {e}")

    def _use_ssh_for_api(self) -> bool:
        """Whether API calls must go through SSH (ndsRTR blocks port 2121 on LAN)."""
        return bool(self.jump_host) or bool(os.environ.get("TOLLGATE_VIRTUAL_LAB"))

    def api_status(self, path: str) -> int:
        url = self.backend_url(path)
        if self._use_ssh_for_api():
            try:
                out = self.ssh(f"curl -s -o /dev/null -w '%{{http_code}}' '{url}'", timeout=15)
                return int(out.strip()) if out.strip().isdigit() else 0
            except Exception:
                return 0
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             f"http://{self.host}:{BACKEND_PORT}{path}"],
            capture_output=True, text=True, timeout=15,
        )
        code = r.stdout.strip()
        return int(code) if code.isdigit() else 0

    def api_body(self, path: str) -> str:
        url = self.backend_url(path)
        if self._use_ssh_for_api():
            try:
                return self.ssh(f"wget -qO- '{url}'", timeout=15)
            except Exception:
                return ""
        r = subprocess.run(
            ["curl", "-s", f"http://{self.host}:{BACKEND_PORT}{path}"],
            capture_output=True, text=True, timeout=15,
        )
        return r.stdout.strip()

    def backend_curl_xff(self, path: str, ip: str | None = None, method: str | None = None,
                         headers: dict | None = None, data: str | None = None) -> str:
        ip = ip or self.phone_ip
        header_args = ""
        if ip:
            header_args += f" --header='X-Forwarded-For: {ip}'"
        if headers:
            for k, v in headers.items():
                header_args += f" --header='{k}: {v}'"
        if data:
            return self.ssh(f"wget -qO- {header_args} --post-data='{data}' '{path}' 2>/dev/null || true")
        return self.ssh(f"wget -qO- {header_args} '{path}' 2>/dev/null || true")

    def pay_direct(self, token: str, ip: str | None = None) -> dict:
        ip = ip or self.phone_ip

        client_ip = os.environ.get("TOLLGATE_CLIENT_IP", "")
        is_virtual_lab = bool(os.environ.get("TOLLGATE_VIRTUAL_LAB"))

        # Virtual lab: route payment through the Debian VM (10.99.99.100)
        # so the backend sees the correct source IP → MAC in ARP/DHCP.
        # Running curl on the router itself (localhost) fails MAC lookup.
        if is_virtual_lab and client_ip:
            ssh_user = os.environ.get("TOLLGATE_CLIENT_USER", "debian")
            try:
                result = subprocess.run(
                    ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                     f"{ssh_user}@{client_ip}",
                     f"curl -s --max-time 20 -d @- "
                     f"-H 'Content-Type: text/plain' "
                     f"'http://{self.host}:{BACKEND_PORT}/'"],
                    input=token, capture_output=True, text=True, timeout=30,
                )
                resp = result.stdout.strip()
                if resp:
                    try:
                        return json.loads(resp)
                    except json.JSONDecodeError:
                        return {"raw": resp[:500]}
            except Exception:
                pass

        # Local QEMU provider: same issue (localhost MAC lookup), route via client IP
        if client_ip and os.environ.get("TOLLGATE_VM_PROVIDER") == "local":
            try:
                result = subprocess.run(
                    ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                     f"root@{client_ip}",
                     f"curl -s --max-time 20 -d @- "
                     f"-H 'Content-Type: text/plain' "
                     f"'http://10.99.99.1:{BACKEND_PORT}/'"],
                    input=token, capture_output=True, text=True, timeout=30,
                )
                resp = result.stdout.strip()
                if resp:
                    try:
                        return json.loads(resp)
                    except json.JSONDecodeError:
                        return {"raw": resp[:500]}
            except Exception:
                pass

        cmd = (
            f"curl -s --max-time 20 -d @- "
            f"-H 'Content-Type: text/plain' "
            f"-H 'X-Forwarded-For: {ip}' "
            f"'{self.backend_url('/')}'"
        )
        result = self.ssh_stdin(cmd, token, timeout=60)
        resp = result.stdout.strip()
        try:
            return json.loads(resp)
        except json.JSONDecodeError:
            return {"raw": resp}

    def pay_direct_mac(self, token: str, mac: str | None = None, ip: str | None = None) -> dict:
        mac = mac or self.phone_mac
        ip = ip or self.phone_ip
        escaped = token.replace("'", "'\\''")
        resp = self.ssh(
            f"printf '%s' '{escaped}' > /tmp/tg-pay-token.txt && "
            f"curl -s --max-time 20 -d @/tmp/tg-pay-token.txt "
            f"-H 'Content-Type: text/plain' "
            f"-H 'X-Forwarded-For: {ip}' "
            f"'{self.backend_url('/')}'; "
            f"rm -f /tmp/tg-pay-token.txt",
            timeout=60,
        )
        try:
            return json.loads(resp)
        except json.JSONDecodeError:
            return {"raw": resp}

    def pay_via_header(self, token: str, mac: str | None = None) -> str:
        mac = mac or self.phone_mac
        return self.ssh(
            f"wget -qO- --header='X-Cashu: {token}' "
            f"'{self.backend_url(f'/pay?mac={mac}')}'"
        )

    def get_client_ip_from_nds(self, mac: str | None = None) -> str:
        """Look up the IP that NDS registered for a client MAC.

        NDS may see a different IP than the static one configured in env vars
        (e.g., DHCP-assigned .120 vs static .100 on a dual-IP VM). This queries
        ndsctl clients and returns the IP NDS actually associated with the MAC.
        """
        mac = mac or self.phone_mac
        if not mac:
            return ""
        out = self.ssh("ndsctl clients 2>&1", timeout=10)
        lines = out.split("\n")
        mac_clean = mac.replace(":", "").upper()
        for i, line in enumerate(lines):
            if mac in line or mac_clean in line.replace(":", "").upper():
                for j in range(i, min(i + 20, len(lines))):
                    m = re.search(r"ip=(\S+)", lines[j])
                    if m:
                        return m.group(1)
        return ""

    def get_nds_state(self, mac: str | None = None) -> str:
        mac = mac or self.phone_mac
        out = self.ssh("ndsctl clients 2>&1", timeout=10)
        lines = out.split("\n")
        for i, line in enumerate(lines):
            if mac in line or mac.replace(":", "").upper() in line.replace(":", "").upper():
                for j in range(i, min(i + 20, len(lines))):
                    m = re.search(r"state=(\S+)", lines[j])
                    if m:
                        return m.group(1)
        return ""

    def wait_for_auth(self, timeout: int = 30, mac: str | None = None) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if self.get_nds_state(mac) == "Authenticated":
                self.fix_nodogsplash_auth_marks()
                return True
            # Fallback: if backend delayed auth didn't fire, trigger manually
            if time.time() - start > 10:
                self.ssh(f"ndsctl auth {mac or self.phone_mac} 2>&1", timeout=5)
            time.sleep(1)
        authed = self.get_nds_state(mac) == "Authenticated"
        if authed:
            self.fix_nodogsplash_auth_marks()
        return authed

    def ensure_dhcp_lease(self, ip: str | None = None, mac: str | None = None) -> None:
        """Ensure the client IP/MAC pair exists in /tmp/dhcp.leases.

        The Rust backend's DhcpLeasesResolver reads /tmp/dhcp.leases to map
        client IPs to MAC addresses for session tracking. In the cloud lab,
        the Debian container may use a static IP that never appears in DHCP
        leases. This method injects a synthetic lease entry so MAC resolution
        works.

        No-op if the lease already exists.
        """
        ip = ip or self.phone_ip
        mac = mac or self.phone_mac
        if not ip or not mac:
            return
        try:
            leases = self.ssh("cat /tmp/dhcp.leases 2>/dev/null || echo ''", timeout=10)
            if ip in leases and mac in leases:
                return
            self.ssh(f"sed -i '/{ip}/d' /tmp/dhcp.leases 2>/dev/null || true", timeout=10)
            timestamp = int(time.time())
            self.ssh(
                f"echo '{timestamp} {mac} {ip} * ' >> /tmp/dhcp.leases",
                timeout=10,
            )
            log.info("Injected DHCP lease: %s -> %s", ip, mac)
        except Exception as e:
            log.warning("Could not inject DHCP lease for %s/%s: %s", ip, mac, e)

    def get_session(self, ip: str | None = None) -> dict:
        ip = ip or self.phone_ip
        resp = self.backend_curl_xff(self.backend_url("/balance"), ip)
        try:
            return json.loads(resp)
        except json.JSONDecodeError:
            return {"raw": resp}

    def get_remaining_seconds(self, ip: str | None = None) -> int:
        session = self.get_session(ip)
        remaining_ms = session.get("remaining", 0)
        return remaining_ms // 1000 if remaining_ms and remaining_ms > 0 else 0

    def wait_for_session_expiry(self, mac: str | None = None, poll_interval: float = 1, max_wait: int = 120) -> int:
        mac = mac or self.phone_mac
        start = time.time()
        while time.time() - start < max_wait:
            if self.get_nds_state(mac) != "Authenticated":
                return int(time.time() - start)
            time.sleep(poll_interval)
        raise TimeoutError(f"Session did not expire within {max_wait}s")

    def reset_state(self, mac: str | None = None, adb=None):
        if not mac and not self.phone_mac and adb:
            detected = adb.wifi_mac()
            if detected:
                self.phone_mac = detected
                log.info(f"reset_state auto-detected MAC: {detected}")
        mac = mac or self.phone_mac
        if adb:
            adb.shell("am force-stop com.android.captiveportallogin")
        if self.backend.has_sessions_json:
            self.ssh("echo '{}' > /etc/tollgate/sessions.json")
        self.restart_backend()
        time.sleep(3)
        self.ssh(f"ndsctl deauth {mac} 2>&1 || true")
        self.ssh("echo '' > /tmp/tollgate-portal.log")
        self.ssh("echo '' > /www/pending-token.txt")

    def apply_pricing(self, step_size: int | None = None, metric: str = "milliseconds"):
        if step_size is None:
            from lib.constants import DEFAULT_STEP_SIZE_MS
            step_size = DEFAULT_STEP_SIZE_MS
        self.ssh(
            f"sed -i 's/\"step_size\":[[:space:]]*[0-9]*/\"step_size\": {step_size}/' "
            f"/etc/tollgate/config.json"
        )
        self.ssh(
            f"sed -i 's/\"metric\":[[:space:]]*\"[^\"]*\"/\"metric\": \"{metric}\"/' "
            f"/etc/tollgate/config.json"
        )
        self.restart_backend()
        self._wait_for_backend()

    def restore_pricing(self):
        self.ssh("cp /etc/tollgate/config.json.test-backup /etc/tollgate/config.json")
        self.restart_backend()
        self._wait_for_backend()

    def _wait_for_backend(self, timeout: int = 15):
        start = time.time()
        while time.time() - start < timeout:
            code = self.api_status("/")
            if code == 200:
                return
            time.sleep(1)
        log.warning(f"Backend not healthy after {timeout}s")

    def wait_for_cli_socket(self, timeout: int = 30, interval: int = 1) -> bool:
        """Poll for CLI socket readiness after backend restart.

        Returns True if /var/run/tollgate.sock exists within timeout seconds.
        Returns False if timeout expires.
        """
        start = time.time()
        while time.time() - start < timeout:
            try:
                result = self.ssh("test -S /var/run/tollgate.sock && echo READY", timeout=5)
                if "READY" in result:
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False

    def restart_backend(self, timeout: int = 30):
        """Restart the backend service and wait for readiness."""
        self.ssh("service tollgate-wrt restart 2>/dev/null; service tollgate restart 2>/dev/null; true", timeout=15)
        time.sleep(2)
        if self.api_status("/") != 200:
            self.ssh("killall tollgate 2>/dev/null; sleep 1; setsid /tmp/tollgate > /tmp/tollgate.log 2>&1 < /dev/null &", timeout=10)
            time.sleep(3)

        if self.backend.is_rust_family:
            url = self.backend_url("/")
            start = time.time()
            while time.time() - start < timeout:
                try:
                    out = self.ssh(
                        f"curl -s -o /dev/null -w '%{{http_code}}' '{url}'",
                        timeout=10,
                    )
                    if out.strip() == "200":
                        return
                except Exception:
                    pass
                time.sleep(1)
            raise RuntimeError("Rust backend did not become healthy after restart")
        else:
            if not self.wait_for_cli_socket(timeout=timeout):
                raise RuntimeError("Backend CLI socket did not become ready after restart")

    def get_portal_log(self) -> str:
        return self.ssh("cat /tmp/tollgate-portal.log 2>/dev/null")

    def clear_portal_log(self):
        self.ssh("echo '' > /tmp/tollgate-portal.log")

    def enable_debug_portal(self):
        self.ssh("mkdir -p /etc/tollgate && touch /etc/tollgate/debug-portal")

    def disable_debug_portal(self):
        self.ssh("rm -f /etc/tollgate/debug-portal")

    def ensure_test_mint(self):
        cfg_raw = self.ssh("cat /etc/tollgate/config.json")
        if not cfg_raw or not cfg_raw.strip():
            log.warning("Config empty or unreadable, skipping test mint setup")
            return
        try:
            cfg = json.loads(cfg_raw)
        except json.JSONDecodeError:
            log.warning("Config not valid JSON, skipping test mint setup: %s", cfg_raw[:100])
            return
        if any(m.get("url") == TEST_MINT_URL for m in cfg.get("accepted_mints", [])):
            return
        cfg.setdefault("accepted_mints", []).append({
            "url": TEST_MINT_URL,
            "min_balance": 0,
            "balance_tolerance_percent": 0,
            "payout_interval_seconds": 60,
            "min_payout_amount": 0,
            "price_per_step": 1,
            "price_unit": "sat",
            "purchase_min_steps": 0,
        })
        tmp = "/tmp/config-testmint.json"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        self.scp_to(tmp, "/etc/tollgate/config.json")
        os.remove(tmp)
        self.restart_backend()
        log.info(f"Added {TEST_MINT_URL} to accepted mints, restarted backend")

    def replace_mints(self, mint_urls: list[str] | None = None):
        """Replace all accepted mints with only the specified URLs.
        
        Args:
            mint_urls: List of mint URLs to use. Defaults to [TEST_MINT_URL].
        """
        if mint_urls is None:
            mint_urls = [TEST_MINT_URL]

        # Read current config
        cfg_raw = self.ssh("cat /etc/tollgate/config.json")
        if not cfg_raw or not cfg_raw.strip():
            log.warning("Config empty or unreadable, skipping mint replacement")
            return
        try:
            cfg = json.loads(cfg_raw)
        except json.JSONDecodeError:
            log.warning("Config not valid JSON, skipping mint replacement: %s", cfg_raw[:100])
            return
        
        # Build new accepted_mints list
        new_mints = []
        for url in mint_urls:
            new_mints.append({
                "url": url,
                "min_balance": 0,
                "balance_tolerance_percent": 0,
                "payout_interval_seconds": 60,
                "min_payout_amount": 0,
                "price_per_step": 1,
                "price_unit": "sat",
                "purchase_min_steps": 0,
            })
        
        cfg["accepted_mints"] = new_mints
        
        current_urls = sorted([m.get("url", "") for m in json.loads(cfg_raw).get("accepted_mints", [])])
        if current_urls == sorted(mint_urls):
            log.info("Mints already correct, skipping restart")
            return

        self.write_remote_json("/etc/tollgate/config.json", cfg)

        try:
            self.restart_backend()
        except RuntimeError:
            log.warning("Backend restart failed after mint replacement — continuing")

        mint_str = ", ".join(mint_urls)
        log.info(f"Replaced accepted mints with: {mint_str}, restarted backend")

    def cli_command(self, command: str, args: list[str] | None = None, timeout: int = 10) -> dict:
        if not self.backend.has_cli_socket:
            return {"success": False, "error": "no CLI socket (rust backend)"}
        cmd_parts = ["tollgate", "--json", command]
        if args:
            cmd_parts.extend(args)
        raw = self.ssh(
            " ".join(cmd_parts),
            timeout=timeout,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    def get_tollgate_version(self) -> dict:
        return self.cli_command("version")

    def get_wallet_info(self) -> dict:
        return self.cli_command("wallet", args=["info"])

    def get_wallet_balance(self) -> dict:
        return self.cli_command("wallet", args=["balance"])

    def get_tollgate_status(self) -> dict:
        return self.cli_command("status")

    def get_tollgate_logs(self, filter_expr: str = "tollgate", lines: int = 200) -> str:
        return self.ssh(f"logread -l {lines} -e {filter_expr} 2>/dev/null")

    def collect_logs(self, results_dir: str, adb=None, bundle: str | None = None):
        raw = os.path.join(results_dir, "raw")
        if bundle:
            raw = os.path.join(raw, "failures", bundle)
        os.makedirs(raw, exist_ok=True)
        log_cmds = [
            ("portal.log", "cat /tmp/tollgate-portal.log"),
            ("backend.log", "logread -l 200 -e tollgate 2>/dev/null"),
            ("ndsctl-status.txt", "timeout 5 ndsctl status 2>/dev/null || true"),
            ("ndsctl-clients.txt", "timeout 5 ndsctl clients 2>/dev/null || true"),
            ("iptables-nds.txt", "iptables -L ndsRTR -n -v 2>/dev/null || true"),
            ("iptables-nds-out.txt", "iptables -L ndsOUT -n -v 2>/dev/null || true"),
            ("tollgate-config.json", "cat /etc/tollgate/config.json 2>/dev/null || true"),
            ("process-list.txt", "ps | grep -E 'tollgate|nodog' 2>/dev/null || true"),
            ("dhcp-leases.txt", "cat /tmp/dhcp.leases 2>/dev/null || true"),
            ("ipv6-addrs.txt", "ip -6 addr show br-lan scope global 2>/dev/null || echo 'none'"),
        ]
        if self.backend.has_sessions_json:
            log_cmds.append(("tollgate-sessions.json", "cat /etc/tollgate/sessions.json 2>/dev/null || echo '{}'"))
        for name, cmd in log_cmds:
            try:
                with open(os.path.join(raw, name), "w") as f:
                    f.write(self.ssh(cmd, timeout=10))
            except Exception:
                pass
        if adb:
            try:
                with open(os.path.join(raw, "logcat.txt"), "w") as f:
                    f.write(adb.shell("logcat -d -t 200"))
            except Exception:
                pass

    # -- Migration helpers (promoted from scenario tests) --

    def ssh_bool(self, cmd: str, timeout: int = 30) -> bool:
        """Run a remote shell predicate and return True only when it exits 0."""
        return self.ssh(f"( {cmd} ) >/dev/null 2>&1 && echo YES || echo NO", timeout=timeout).strip() == "YES"

    def file_mode(self, path: str) -> str:
        """Return the remote file's symbolic mode string, e.g. ``-rw-------``."""
        quoted = shlex.quote(path)
        return self.ssh(f"ls -l {quoted} 2>/dev/null | awk '{{print $1}}'").strip()

    def file_octal_mode(self, path: str) -> str:
        """Return the remote file's octal permission bits when stat supports it."""
        quoted = shlex.quote(path)
        return self.ssh(
            f"stat -c '%a' {quoted} 2>/dev/null || stat -f '%Lp' {quoted} 2>/dev/null || true"
        ).strip()

    def uci_get(self, path: str) -> str:
        return self.ssh(f"uci -q get {path} 2>/dev/null || true").strip()

    def uci_set(self, path: str, value: str) -> None:
        self.ssh(f"uci set {path}={shlex.quote(value)}")

    def uci_commit(self, *configs: str) -> None:
        if configs:
            self.ssh("uci commit " + " ".join(configs))
        else:
            self.ssh("uci commit")

    def block_mint(self, mint_url: str | None = None) -> None:
        """Block mint hostname via /etc/hosts (same as Makefile block-mint)."""
        url = mint_url or os.environ.get("TOLLGATE_TEST_MINT_URL", TEST_MINT_URL)
        from urllib.parse import urlparse
        host = urlparse(url).hostname or url
        quoted_host = shlex.quote(host)
        quoted_entry = shlex.quote(f"0.0.0.0 {host}")
        self.ssh(
            f"grep -qF -- {quoted_host} /etc/hosts || printf '%s\n' {quoted_entry} >> /etc/hosts"
        )
        log.info("Blocked mint host %s via /etc/hosts", host)

    def unblock_mint(self, mint_url: str | None = None) -> None:
        url = mint_url or os.environ.get("TOLLGATE_TEST_MINT_URL", TEST_MINT_URL)
        from urllib.parse import urlparse
        host = urlparse(url).hostname or url
        quoted_host = shlex.quote(host)
        self.ssh(
            f"tmp=/tmp/hosts.$$; grep -vF -- {quoted_host} /etc/hosts > $tmp || true; mv $tmp /etc/hosts"
        )
        log.info("Unblocked mint host %s", host)

    def get_hosts_entries(self) -> list[str]:
        return self.ssh("cat /etc/hosts").splitlines()

    def upstream_connect(self, ssid: str, password: str | None = None) -> dict[str, object]:
        args = ["connect", ssid]
        if password:
            args.append(password)
        return self.cli_command("upstream", args=args)

    def upstream_remove(self, ssid: str) -> dict[str, object]:
        return self.cli_command("upstream", args=["remove", ssid])

    def upstream_list(self) -> dict[str, object]:
        return self.cli_command("upstream", args=["list"])
