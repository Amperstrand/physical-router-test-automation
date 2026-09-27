#!/bin/sh
# SSH wrapper for the GS1900-8HP rig — routes every connection through the
# management switch (root@192.168.13.2) as SSH jump host.
#
# Why this exists: the DUT subnets (192.168.10N.0/24, one VLAN per switch port)
# are only reachable via the switch, and labgrid's SSHDriver forces
# `-F none` (it ignores ~/.ssh/config), so ProxyJump must be injected here.
# Wired into the labgrid env via `tools: ssh:` in rig-alpha.yaml.
exec /usr/bin/ssh -J root@192.168.13.2 -o StrictHostKeyChecking=accept-new "$@"
