"""Shared constants for the GCP cloud lab."""

from __future__ import annotations

import os

DEFAULT_ZONE = "us-east1-b"
DEFAULT_MACHINE_TYPE = "n2-standard-2"
DEFAULT_DISK_SIZE_GB = 50
VM_NAME = "tollgate-test-runner"
# DEPRECATED: GCP cloud lab — SHC is the default provider (~10x cheaper) and
# no runner snapshot is currently baked. Guarded by ensure_runner_snapshot().
SNAPSHOT_NAME = os.environ.get("TOLLGATE_GCP_SNAPSHOT", "tollgate-runner-v20")
FIREWALL_RULE_SSH = "tollgate-allow-ssh"
VIRT_LAB_PASSWORD = "tollgate"
OPENWRT_IP = "10.99.99.1"
SELLER_OPENWRT_IP = "10.99.99.11"
SELLER_OPENWRT_MAC = "52:54:00:12:34:57"
DEBIAN_IP = "10.99.99.100"
DEBIAN_MAC = "de:54:4e:91:49:da"
SSH_KEY = os.environ.get("TOLLGATE_GCP_SSH_KEY", os.path.expanduser("~/.ssh/google_compute_engine"))
TEST_DIR = os.environ.get("GITHUB_WORKSPACE", "/opt/tollgate-test")
VIRT_LAB_WORKDIR = "$HOME/tollgate-virtual-lab"
CLOUD_ARCH = "x86_64"
SUITE_REPO = "OpenTollGate/physical-router-test-automation"
SUITE_REPO_URL = "https://github.com/OpenTollGate/physical-router-test-automation.git"
RESULTS_ROOT = "/tmp/tollgate-results"
WORKER_LOG = "/var/log/tollgate-run.log"
STALE_VM_HOURS = 2

# Management bridge — separate from test network so SSH survives network changes
MGMT_BRIDGE = "mgmt-br"
MGMT_HOST_IP = "10.99.97.2"
MGMT_SUBNET = "10.99.97.0/24"
MGMT_ALPHA_IP = "10.99.97.1"
MGMT_DEBIAN_IP = "10.99.97.100"
MGMT_BETA_IP = "10.99.97.11"
MGMT_TAP_ALPHA = "mgmt-tap"
MGMT_TAP_DEBIAN = "mgmt-tap2"
MGMT_TAP_BETA = "mgmt-tap3"
MGMT_ALPHA_MAC = "52:54:00:c0:01:01"
MGMT_DEBIAN_MAC = "52:54:00:c0:02:64"
MGMT_BETA_MAC = "52:54:00:c0:03:0b"

# Two-router upstream bridge
UPSTREAM_BRIDGE = "tg-upstream-br"
UPSTREAM_TAP_ALPHA = "tg-upst-tap-a"
UPSTREAM_TAP_BETA = "tg-upst-tap-b"
BETA_WAN_IP = "10.99.98.1"
ALPHA_WAN_MAC = "52:54:00:aa:bb:02"
BETA_WAN_MAC = "52:54:00:aa:bb:01"

# Local mint configuration
LOCAL_MINT_HOST = "10.99.99.2"
CDK_MINT_PORT = 8383
NUTSHELL_V2_MINT_PORT = 8384
NUTSHELL_V1_MINT_PORT = 8385
CDK_VERSION = "0.16.0"
CDK_MINT_DIR = "/opt/cdk-mintd"

# Toxiproxy — chaos engineering proxy for controllable mint connectivity.
# CDK V2 mint runs on CDK_MINT_INTERNAL_PORT; Toxiproxy listens on
# CDK_MINT_PORT and forwards, allowing tests to toggle connectivity.
TOXIPROXY_MGMT_PORT = 8474
TOXIPROXY_VERSION = "2.11.0"
TOXIPROXY_DIR = "/opt/toxiproxy"
CDK_MINT_INTERNAL_PORT = 18383

# IP-based URLs — used for health checks before /etc/hosts is configured
CDK_MINT_URL = f"http://{LOCAL_MINT_HOST}:{CDK_MINT_PORT}"
NUTSHELL_V2_MINT_URL = f"http://{LOCAL_MINT_HOST}:{NUTSHELL_V2_MINT_PORT}"
NUTSHELL_V1_MINT_URL = f"http://{LOCAL_MINT_HOST}:{NUTSHELL_V1_MINT_PORT}"

# LAN mint URLs — IP-based (OpenWrt dnsmasq does not resolve /etc/hosts entries)
V1_TESTNUT_NUTSHELL_LAN = f"http://{LOCAL_MINT_HOST}:{NUTSHELL_V1_MINT_PORT}"
V2_TESTNUT_CDK_LAN = f"http://{LOCAL_MINT_HOST}:{CDK_MINT_PORT}"
V2_TESTNUT_NUTSHELL_LAN = f"http://{LOCAL_MINT_HOST}:{NUTSHELL_V2_MINT_PORT}"

# Beta isolated LAN bridge — separate from tg-poc-br so Beta has its own subnet
BETA_BRIDGE = "tg-beta-br"
BETA_TAP = "tg-beta-tap"
BETA_LAN_IP = "10.99.96.11"
BETA_LAN_HOST_IP = "10.99.96.2"
BETA_LAN_SUBNET = "10.99.96.0/24"

# Backwards-compatible alias
NUTSHELL_V1_MINT_LAN = V1_TESTNUT_NUTSHELL_LAN

MAX_CHAIN_ROUTERS = 5

# ── Multi-hop chain topology (N >= 2 routers) ──────────────
# Each router[i] has:
#   NIC0 (br-lan): on chain_bridge(i), IP chain_lan_ip(i), serves DHCP
#   NIC1 (eth1/WAN): on chain_bridge(i+1), gets DHCP from router[i+1] (only if i < N-1)
#   NIC2 (mgmt): on mgmt-br, IP chain_mgmt_ip(i)
# The topmost router[N-1] has Host NAT on its br-lan bridge for internet access.
# Router[0] reuses the existing tg-poc-br (10.99.99.0/24).
# Router[i>=1] uses 10.99.{50+i}.0/24 to avoid conflicts with existing subnets.


def chain_bridge(router_index: int) -> str:
    if router_index == 0:
        return "tg-poc-br"
    return f"tg-hop-{router_index}-br"


def chain_subnet_prefix(router_index: int) -> int:
    if router_index == 0:
        return 99
    return 50 + router_index


def chain_lan_ip(router_index: int) -> str:
    return f"10.99.{chain_subnet_prefix(router_index)}.1"


def chain_host_ip(router_index: int) -> str:
    return f"10.99.{chain_subnet_prefix(router_index)}.2"


def chain_subnet(router_index: int) -> str:
    return f"10.99.{chain_subnet_prefix(router_index)}.0/24"


def chain_lan_tap(router_index: int) -> str:
    return f"tg-hop-{router_index}-lan"


def chain_wan_tap(router_index: int) -> str:
    return f"tg-hop-{router_index}-wan"


def chain_mgmt_tap(router_index: int) -> str:
    return f"mgmt-tap-{router_index}"


def chain_mgmt_ip(router_index: int) -> str:
    return f"10.99.97.{11 + router_index}"


def chain_lan_mac(router_index: int) -> str:
    return f"52:54:00:dd:{router_index:02x}:00"


def chain_wan_mac(router_index: int) -> str:
    return f"52:54:00:dd:{router_index:02x}:01"


def chain_mgmt_mac(router_index: int) -> str:
    return f"52:54:00:dd:{router_index:02x}:02"


def chain_disk_name(router_index: int) -> str:
    return f"tollgate-chain-{router_index}.qcow2"
