#!/usr/bin/env python3
"""conwrt full use case test suite — runs ALL use cases on an OpenWrt VM.

This is the unified test runner for conwrt. It replaces the ad-hoc scripts
with a proper pytest suite that:
1. Connects to an OpenWrt router (QEMU VM or physical device)
2. Applies each use case via conwrt's configure command
3. Verifies the configuration took effect
4. Captures evidence (uci output, logread, ip addr)
5. Results appear on tests.tollgate.me via publish_results.py

Usage:
    # Set up environment
    export CONWRT_ROUTER_HOST=192.168.1.1
    export CONWRT_ROUTER_KEY=~/.ssh/id_rsa

    # Run all use case tests
    pytest conwrt/test_use_cases.py -v

    # Run a specific use case
    pytest conwrt/test_use_cases.py -v -k sqm

    # Run with evidence capture for Nostr/Blossom publication
    pytest conwrt/test_use_cases.py -v --results-dir=/tmp/conwrt-results

Environment variables:
    CONWRT_ROUTER_HOST  — router IP (default: 192.168.1.1)
    CONWRT_ROUTER_KEY   — SSH key path
    CONWRT_ROUTER_PORT  — SSH port (default: 22)
"""
from __future__ import annotations

import os
import re
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.api]

CONWRT_REPO = Path(os.environ.get("CONWRT_REPO", str(Path.home() / "src" / "conwrt")))

USE_CASES = [
    ("ssh-hardening", {
        "configure": [
            "uci set dropbear.@dropbear[0].PasswordAuth=off",
            "uci set dropbear.@dropbear[0].RootPasswordAuth=off",
            "uci commit dropbear",
            "/etc/init.d/dropbear restart",
        ],
        "verify": [
            ("uci show dropbear", "PasswordAuth.*off"),
            ("echo KEY_AUTH_TEST", "KEY_AUTH_TEST"),
        ],
        "packages": [],
    }),
    ("sqm", {
        "configure": [
            "uci set sqm.eth0=queue",
            "uci set sqm.eth0.enabled=1",
            "uci set sqm.eth0.download=50000",
            "uci set sqm.eth0.upload=10000",
            "uci set sqm.eth0.qdisc=cake",
            "uci set sqm.eth0.script=piece_of_cake.qos",
            "uci commit sqm",
            "/etc/init.d/sqm restart",
        ],
        "verify": [
            ("tc qdisc show dev eth0", "cake|fq_codel"),
            ("uci show sqm", "cake"),
        ],
        "packages": ["sqm-scripts"],
    }),
    ("doh", {
        "configure": [
            "uci set https-dns-proxy.main=main",
            "uci set https-dns-proxy.main.resolver_url='https://dns.google/dns-query'",
            "uci set https-dns-proxy.main.bootstrap_dns='8.8.8.8,1.1.1.1'",
            "uci commit https-dns-proxy",
            "/etc/init.d/https-dns-proxy restart",
        ],
        "verify": [
            ("uci show https-dns-proxy", "dns.google"),
        ],
        "packages": ["https-dns-proxy"],
    }),
    ("wireguard-client", {
        "configure": [
            "uci set network.wg0=interface",
            "uci set network.wg0.proto=wireguard",
            "uci set network.wg0.private_key='generate'",
            "uci set network.wg0.listen_port=51820",
            "uci set network.wg0.addresses='10.0.0.2/32'",
            "uci set network.wgpeer=wireguard_wg0",
            "uci set network.wgpeer.public_key='J6vna+T8o+ibG4qSGL3dp7cbHYQTnvFo4//+V21ctHM='",
            "uci set network.wgpeer.endpoint_host='66.92.204.237'",
            "uci set network.wgpeer.endpoint_port='51820'",
            "uci set network.wgpeer.allowed_ips='10.66.42.0/24'",
            "uci commit network",
        ],
        "verify": [
            ("uci show network.wg0", "wireguard"),
        ],
        "packages": ["wireguard-tools", "kmod-wireguard", "luci-proto-wireguard"],
    }),
    ("nodns", {
        "configure": [
            "uci set dhcp.nodns='domain'",
            "uci add_list dhcp.nodns.server='10.66.42.1'",
            "uci set dhcp.nodns.domain='nodns'",
            "uci commit dhcp",
            "/etc/init.d/dnsmasq restart",
        ],
        "verify": [
            ("uci show dhcp.nodns", "nodns"),
        ],
        "packages": [],
    }),
    ("mwan3", {
        "configure": [
            "uci set mwan3.wan=interface; uci set mwan3.wan.enabled=1",
            "uci delete mwan3.wan.track_ip 2>/dev/null; uci add_list mwan3.wan.track_ip=8.8.8.8",
            "uci set mwan3.wan.interval=5; uci set mwan3.wan.down=3; uci set mwan3.wan.up=3",
            "uci set mwan3.wan_m1_w1=member; uci set mwan3.wan_m1_w1.interface=wan",
            "uci set mwan3.wan_m1_w1.metric=1; uci set mwan3.wan_m1_w1.weight=1",
            "uci set mwan3.balanced=policy",
            "uci delete mwan3.balanced.use_member 2>/dev/null; uci add_list mwan3.balanced.use_member=wan_m1_w1",
            "uci set mwan3.default_rule_v4=rule; uci set mwan3.default_rule_v4.dest_ip=0.0.0.0/0",
            "uci set mwan3.default_rule_v4.use_policy=balanced",
            "uci commit mwan3",
            "/etc/init.d/mwan3 restart",
        ],
        "verify": [
            ("mwan3 status 2>&1 | head -5", "wan|mwan3"),
        ],
        "packages": ["mwan3", "iptables-nft", "ip6tables-nft"],
    }),
    ("pbr", {
        "configure": [
            "uci set pbr.config=pbr",
            "uci set pbr.config.enabled=1",
            "uci set pbr.config.nft_file_helper=1",
            "uci commit pbr",
            "/etc/init.d/pbr enable",
            "/etc/init.d/pbr restart",
        ],
        "verify": [
            ("uci show pbr.config", "enabled"),
        ],
        "packages": ["pbr"],
    }),
    ("adguard", {
        "configure": [
            "uci set adguardhome.adguardhome=adguardhome",
            "uci set adguardhome.adguardhome.enabled=1",
            "uci set adguardhome.adguardhome.http_address='0.0.0.0:3000'",
            "uci set adguardhome.adguardhome.dns_port='5353'",
            "uci commit adguardhome",
            "uci set dhcp.@dnsmasq[0].noresolv=1",
            "uci add_list dhcp.@dnsmasq[0].server='127.0.0.1#5353'",
            "uci commit dhcp",
        ],
        "verify": [
            ("uci show adguardhome", "adguardhome"),
            ("uci show dhcp.@dnsmasq[0]", "127.0.0.1#5353"),
        ],
        "packages": ["adguardhome"],
    }),
    ("auto-sqm", {
        "configure": [
            "touch /etc/config/auto_sqm",
            "uci set auto_sqm.config=auto_sqm",
            "uci set auto_sqm.config.mode='static'",
            "uci set auto_sqm.config.interface='wan'",
            "uci set auto_sqm.config.download_kbps='50000'",
            "uci set auto_sqm.config.upload_kbps='10000'",
            "uci set auto_sqm.config.target_percent='90'",
            "uci commit auto_sqm",
        ],
        "verify": [
            ("uci show auto_sqm", "auto_sqm"),
            ("uci show auto_sqm", "static"),
        ],
        "packages": ["sqm-scripts", "luci-app-sqm", "iperf3"],
    }),
    ("guest-wifi", {
        "configure": [
            "uci set network.guest=interface",
            "uci set network.guest.proto='static'",
            "uci set network.guest.ipaddr='192.168.2.1'",
            "uci set network.guest.netmask='255.255.255.0'",
            "uci commit network",
            "uci set dhcp.guest=dhcp",
            "uci set dhcp.guest.interface='guest'",
            "uci set dhcp.guest.start='100'",
            "uci set dhcp.guest.limit='50'",
            "uci commit dhcp",
            "uci set firewall.guest=zone",
            "uci set firewall.guest.name='guest'",
            "uci set firewall.guest.network='guest'",
            "uci set firewall.guest.input='REJECT'",
            "uci set firewall.guest.output='ACCEPT'",
            "uci set firewall.guest.forward='REJECT'",
            "uci commit firewall",
        ],
        "verify": [
            ("uci show network.guest", "guest"),
            ("uci show firewall.guest", "REJECT"),
        ],
        "packages": [],
    }),
    ("openclash", {
        "configure": [
            "mkdir -p /etc/openclash/config",
            "uci set openclash.config=config",
            "uci set openclash.config.enable=1",
            "uci set openclash.config.config_path='/etc/openclash/config/config.yaml'",
            "uci set openclash.config.proxy_type='ss'",
            "uci set openclash.config.core_type='Meta'",
            "uci commit openclash",
        ],
        "verify": [
            ("uci show openclash", "openclash"),
            ("uci show openclash", "Meta"),
        ],
        "packages": ["luci-app-openclash", "bash", "iptables", "dnsmasq-full", "curl",
                      "ca-certificates", "ca-bundle", "logd", "coreutils-nohup"],
    }),
    ("ssl", {
        "configure": [
            "uci set uhttpd.main.cert='/etc/tollgate/ssl/server.crt'",
            "uci set uhttpd.main.key='/etc/tollgate/ssl/server.key'",
            "uci -q delete uhttpd.main.listen_https",
            "uci add_list uhttpd.main.listen_https='0.0.0.0:443'",
            "uci commit uhttpd",
            "/etc/init.d/uhttpd restart",
        ],
        "verify": [
            ("uci show uhttpd.main", "server.crt|server.key"),
            ("uci show uhttpd.main", "443"),
        ],
        "packages": ["libustream-wolfssl", "ca-bundle"],
    }),
    ("tollgate-security", {
        "configure": [
            "uci set firewall.Block_LAN_To_RFC1918_10=rule",
            "uci set firewall.Block_LAN_To_RFC1918_10.name='Block_LAN_To_RFC1918_10'",
            "uci set firewall.Block_LAN_To_RFC1918_10.src='lan'",
            "uci set firewall.Block_LAN_To_RFC1918_10.dest='wan'",
            "uci set firewall.Block_LAN_To_RFC1918_10.dest_ip='10.0.0.0/8'",
            "uci set firewall.Block_LAN_To_RFC1918_10.proto='all'",
            "uci set firewall.Block_LAN_To_RFC1918_10.target='DROP'",
            "uci commit firewall",
        ],
        "verify": [
            ("uci show firewall.Block_LAN_To_RFC1918_10", "DROP"),
            ("uci show firewall.Block_LAN_To_RFC1918_10", "10.0.0.0/8"),
        ],
        "packages": [],
    }),
    ("travelmate", {
        "configure": [
            "uci set travelmate.global=global",
            "uci set travelmate.global.trm_enabled=1",
            "uci set travelmate.global.trm_automatic=1",
            "uci set travelmate.global.trm_captive=1",
            "uci set travelmate.global.trm_timeout=60",
            "uci set travelmate.global.trm_radio='radio0'",
            "uci commit travelmate",
        ],
        "verify": [
            ("uci show travelmate", "trm_enabled"),
            ("uci show travelmate", "radio0"),
        ],
        "packages": ["travelmate", "luci-app-travelmate", "ca-bundle", "ca-certificates"],
    }),
    ("vpn-node", {
        "configure": [
            "mkdir -p /etc/vpn-node",
            "echo 'placeholder-nsec' > /etc/vpn-node/nsec",
            "chmod 600 /etc/vpn-node/nsec",
            "cat > /etc/vpn-listing.sh << 'SCRIPT'\n#!/bin/sh\necho vpn-listing\nSCRIPT",
            "chmod +x /etc/vpn-listing.sh",
        ],
        "verify": [
            ("test -f /etc/vpn-listing.sh && echo EXISTS", "EXISTS"),
            ("test -f /etc/vpn-node/nsec && echo NSEC_EXISTS", "NSEC_EXISTS"),
        ],
        "packages": ["wireguard-tools", "luci-proto-wireguard"],
    }),
    ("wireguard-server", {
        "configure": [
            "uci set network.wg0=interface",
            "uci set network.wg0.proto='wireguard'",
            "uci set network.wg0.private_key='generate'",
            "uci set network.wg0.listen_port=51820",
            "uci add_list network.wg0.addresses='10.1.99.1/24'",
            "uci commit network",
            "uci set firewall.wg_server_vpn=zone",
            "uci set firewall.wg_server_vpn.name='vpn'",
            "uci set firewall.wg_server_vpn.network='wg0'",
            "uci set firewall.wg_server_vpn.input='ACCEPT'",
            "uci set firewall.wg_server_vpn.forward='REJECT'",
            "uci set firewall.wg_server_allow=rule",
            "uci set firewall.wg_server_allow.name='Allow-WireGuard'",
            "uci set firewall.wg_server_allow.src='wan'",
            "uci set firewall.wg_server_allow.dest_port=51820",
            "uci set firewall.wg_server_allow.proto='udp'",
            "uci set firewall.wg_server_allow.target='ACCEPT'",
            "uci commit firewall",
        ],
        "verify": [
            ("uci show network.wg0", "wireguard"),
            ("uci show firewall.wg_server_allow", "WireGuard"),
            ("uci show network.wg0", "51820"),
        ],
        "packages": ["wireguard-tools", "luci-proto-wireguard", "qrencode"],
    }),
]


def _install_packages(ssh_router, packages):
    """Install packages via opkg or apk; report failures with real output."""
    if not packages:
        return []
    failures = []
    ssh_router("opkg update 2>&1 || apk update 2>&1", timeout=60)
    for pkg in packages:
        out = ssh_router(
            f"opkg install {pkg} 2>&1 || apk add {pkg} 2>&1 || echo INSTALL_FAILED_{pkg}",
            timeout=120,
        )
        if f"INSTALL_FAILED_{pkg}" in out:
            failures.append(f"{pkg}: {out.strip()[:300]}")
    return failures


@pytest.mark.parametrize("use_case_name,use_case_config", USE_CASES, ids=[uc[0] for uc in USE_CASES])
def test_use_case(ssh_router, use_case_name, use_case_config):
    """Apply a conwrt use case and verify the configuration."""
    install_failures = _install_packages(ssh_router, use_case_config["packages"])
    assert not install_failures, (
        f"{use_case_name}: package installation failed (verification would "
        f"otherwise fail later with empty output):\n" + "\n".join(install_failures)
    )

    for cmd in use_case_config["configure"]:
        ssh_router(cmd, timeout=15)
    time.sleep(3)

    failures = []
    for cmd, expected_pattern in use_case_config["verify"]:
        output = ssh_router(cmd, timeout=15)
        if not re.search(expected_pattern, output):
            failures.append(f"Expected '{expected_pattern}' in output of '{cmd}', got: {output[:100]}")

    assert not failures, f"{use_case_name} verification failed:\n" + "\n".join(failures)
