# Local QEMU lab (Ansible)

This replaces `scripts/provision-local-lab.sh` with an idempotent Ansible role
that provisions the bare-metal QEMU TollGate lab.

## Usage

```bash
ansible-playbook -i ansible/inventory.ini ansible/local-lab.yml
```

Overrides (extra vars): `lab_dir`, `openwrt_overlay`, `debian_overlay`,
`seed_iso`, `cdk_mintd_bin`, `tollgate_bin`, `host_ip`, `openwrt_ip`,
`debian_ip`, `bridge_name`.

## What it does (ported from the shell script)

1. Preflight checks (KVM, binaries, images) + qemu/deps install.
2. `vm-runner` wrapper (process-name disguise).
3. Bridge `tg-poc-br` + QEMU bridge helper + NAT + `ip_forward=1`.
4. CDK fakewallet mint (`cdk-mintd.service`) on `host_ip:8383`.
5. OpenWrt VM (`openwrt-vm.service`) + wait SSH.
6. OpenWrt config: key auth, binary deploy, procd init, mint config, restart.
7. Debian client VM (`debian-vm.service`) + best-effort SSH wait.

Prerequisites (unchanged from the script): OpenWrt overlay, Debian overlay,
NoCloud seed ISO, `cdk-mintd` binary, and (optional) a `tollgate-wrt` binary.
