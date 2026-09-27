# 0.6.0 Validation Campaign Harness (QEMU venue)

Scripts from the 2026-09-17 unattended validation campaign against the QEMU
OpenWrt 24.10 lab (tracker: issue #113). Design: run scripts, check exit
codes, analyze evidence after — no interactive babysitting.

## Topology (and why the shims exist)

```
                    ┌────────────────────────── ai-legion (lab host) ──────────────────────────┐
                    │                                                                          │
 internet ← wlo1 ←──┤ (NAT/masq 10.99.99.0/24)                                                 │
                    │   ↑ policy rule: iif tg-poc-br → table 2000 (default wlo1)                │
                    │   │                        ↑ return path: to <client> → table 2200       │
                    │  tg-poc-br (10.99.99.2) ── Macvlan-free: clients are NETNS + veth        │
                    │   │            │            [tgclient1 .51 / tgclient2 .52]              │
                    │ tg-poc-tap    veth-cN-host                                                               │
                    │   ↓ (QEMU NIC)                                                                           │
                    └───┼──────────────────────────────────────────────────────────────────────┘
                        ↓
                  OpenWrt router VM 10.99.99.1 (br-lan only, default route via 10.99.99.2)
                  tollgate-wrt :2121 · NDS :2050 · uhttpd portal :2051
```

The lab host simultaneously plays gateway (router's upstream) and test-clients.
That triple-role creates three failure modes a physical deployment doesn't
have; each has a shim, each is checked by `lab-preflight.sh`:

1. **Hairpin martian drops** — router-bounced client packets return with
   src = the host's own IP. Shim: `accept_local=1` + `rp_filter=0` on
   `tg-poc-br`.
2. **Routing loop** — per-destination `via-router` routes would send the
   bounced packet straight back to the router. Shim: policy rule
   `pref 100 iif tg-poc-br lookup 2000` (router-bounced traffic always
   egresses wlo1), and per-client return rules `to <client-ip> lookup 2200`
   so replies traverse the tollgate too (otherwise router conntrack sees
   only one direction and drops TCP as INVALID — UDP gets asymmetric free
   passage, which made this bug look protocol-dependent).
3. **Conntrack NAT-binding poisoning** — if the client shares the host
   kernel, the flow's first NAT traversal happens on the wrong path and the
   masquerade binds to a useless address. Shim: clients are **network
   namespaces** (`tg-lib.sh: ensure_client`) bridged onto the LAN — own
   conntrack, own L3, closest software analog of plugging in a laptop.
4. **Single-interface router** — NDS gates only `iif br-lan`; production
   replies arrive via a WAN zone and bypass ndsNET. Shim:
   `tg-lib.sh: lab_shim` (accept established traffic from non-lan sources =
   production wan-zone semantics; client→internet stays gated).

Also: the QEMU guest clock drifts (check skew in preflight; sync via
`date -s @<epoch>` through SSH — busybox `ip`/`stat` limitations mean prefer
`iptables-save`, `ls -l`, and `ndsctl json` for router state).

## Usage

```bash
# on ai-legion (scripts assume ~/ssh key to root@10.99.99.1):
bash scripts/0.6.0-validation/lab-preflight.sh   # LAB READY or fix instructions
bash scripts/0.6.0-validation/phase-a-payment.sh  # each phase: exit 0 = green
```

- `mint-tokens.mjs` runs on the **Mac** (needs `@cashu/cashu-ts` + admin
  keys); it normalizes proof amounts to numbers (cashu-ts emits strings —
  the daemon's Go decoder requires uint64). Output `tokens.json` goes to
  ai-legion `/tmp/phase-a/tokens.json`.
- Phases A–F are self-contained (preflight → checks → `TRIAGE.txt` bundle on
  any failure → restore lab state). `tg-lib.sh` holds shared helpers.
- Evidence lands in `/tmp/phase-*/` on ai-legion; `phase-g-summary.sh`
  renders the cross-phase table.

## Lab-to-physical fidelity notes

- QEMU venue proves L3+: payment protocol, NDS enforcement, sessions,
  portal serving, mint-outage behavior. Radio/WiFi, visual regression,
  install matrix, and soak remain physical-venue work (#106–#112).
- Recommendations for the next lab revision (not done in-campaign): serial
  console as out-of-band recovery (SSH is in-band through the network under
  test), boot the actual CI ipk under emulated aarch64, and a local
  fakewallet mint inside the lab so external mint latency blips can't poison
  health-tracker state during unrelated tests (observed: one 30s mint blip
  → degraded cascade).
