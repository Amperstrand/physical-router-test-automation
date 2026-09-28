# Bench device identity — why the rig pins the box before it touches it

**Lane:** `scripts/bench/device-identity.sh` (`claim` / `verify` / `show`)
**Records:** `scripts/bench/boxes/<name>.identity`
**Offline suite:** `tests/bench-device-identity/run-tests.sh` (no router, no network)
**Status:** the guard is fail-closed and covered by 28 no-hardware tests, including the
non-vacuity pair and a mutation control. It has NOT yet been exercised on live hardware in this
form — see ["What it does not prove"](#what-it-does-not-prove).

## The accident this exists for (measured 2026-09-28)

The bench host had **two routers both answering on `192.168.1.1`**:

- **GL-MT3000** (the bench) — reached over NIC `enp0s31f6`, from host source address `192.168.1.200`
- **Cudy WR3000** — reached over a USB dongle, at an address on that dongle

The first two WAN-less offline-install attempts **silently addressed the wrong device**, and the
Cudy may have been flashed as a side effect. Nothing in those runs was wrong-looking: the address
answered, the port was open, the install "worked". The lane that finally produced a trustworthy
result did it with an ad-hoc fix — `ssh -o BindAddress=192.168.1.200` and pinning the target by
source address. This guard is that fix, made explicit and fail-closed.

The failure class is *not* "the router was broken": it is **an address is not an identity**. Two
boxes can share one address, whichever one the kernel picks wins, and a destructive step then lands
on a box nobody meant to touch.

## The one-line preflight

Before ANY destructive step — flash, `apk add`, install, `sysupgrade`, reboot, a paid E2E:

```sh
scripts/bench/device-identity.sh verify --name bench-mt3000 || exit $?
```

Exit `0` means exactly one thing: *the box answering at this address, over this interface, from
this host source address, IS the box that was claimed*. **Any non-zero exit is a refusal and the
destructive step must not run.** From inside another script:

```sh
"$(dirname "$0")/../bench/device-identity.sh" verify --name "$BENCH_BOX" || exit $?
```

`scripts/mt3000-bench/bench-deploy-apk.sh` calls it automatically when `BENCH_BOX` (or
`BENCH_DEVICE_IDENTITY`) is set, and refuses with exit `11` if the guard refuses; `--require-identity`
makes an unset pin a refusal too.

## Claiming a box

```sh
# over the wired NIC, pinning the host source address as well
scripts/bench/device-identity.sh claim \
    --name bench-mt3000 --iface enp0s31f6 --src 192.168.1.200 \
    --hostname GL-MT3000 --device-code mt3000
```

`claim` refuses to write a record unless it could (a) use the interface it was told to use, (b) prove
that the kernel reaches the address **out of that interface from that source address**, and (c) read
the box's LAN (br-lan) MAC over the bound path. It also refuses (exit `8`) to overwrite an existing
record for the same box that names a *different* MAC — that is how a rig gets pinned to the wrong box
in the first place — until you pass `--force`.

## The record

Plain `key=value`, `#` comments, one file per box in `scripts/bench/boxes/`
(`DI_BOXES_DIR` overrides the directory, `--file PATH` a single record). See
[`scripts/bench/boxes/README.md`](../scripts/bench/boxes/README.md).

```
box=bench-mt3000
iface=enp0s31f6
src_addr=192.168.1.200
router_ip=192.168.1.1
lan_mac=94:83:c4:aa:11:22
hostname=GL-MT3000          # optional
device_code=mt3000          # optional
claimed_at=… claimed_by=… claimed_via=…
```

A record missing `iface`, `src_addr`, `router_ip` or `lan_mac` is **incomplete, and incomplete is a
refusal** (exit `6`): a pin you cannot honour is worse than no pin, because it looks like one.

## How the MAC is read — and what each method proves

- **`neigh`** (default) — `ip route get <ip> from <src>` → `curl --interface <src> http://<ip>:<port>/` → `ip -4 neigh show dev <iface>`.
  Proves the box that **this host source address** hits at that IP, on that interface, has this LAN MAC — i.e. exactly the wrong-device accident. No credentials needed.
- **`ssh`** — `ssh -o BindAddress=<src> … cat /sys/class/net/br-lan/address`.
  Proves the box answering over the **bound** path reports its own br-lan MAC, and that credentials reach that box. Needs the lab credential.

`neigh` deliberately does **not** use ICMP: this bench drops ping (see
`scripts/mt3000-bench/README.md`), so a ping-based reachability check is a false negative while an
unbound TCP check is a false positive. The touch is a TCP connect **from the claimed source
address** — that is what makes the kernel resolve the L2 address of the claimed path.

A stale neighbour entry is never trusted: the guard re-reads over the bound path, and a
`FAILED`/`INCOMPLETE` entry is not a MAC (both directions are covered by tests).

`--check-hostname` (with `--method ssh`) adds a second attribute: `uci get
system.@system[0].hostname` must equal the record's `hostname=` (exit `9` otherwise).

## Exit codes

All of these exit non-zero, i.e. they fail closed:

- `0` — verified: the box that answers IS the claimed box
- `2` — usage / bad arguments
- `3` — **LAN MAC mismatch** — a different box answers on that address
- `4` — address unreachable over the claimed interface/source
- `5` — the claimed interface/source cannot be used (source not on the iface, or the route goes out a **different** interface)
- `6` — identity record missing / incomplete / **ambiguous** (two records claim the same box)
- `7` — the LAN MAC could not be read
- `8` — `claim`: an existing record for this box disagrees; not overwritten (use `--force`)
- `9` — optional attribute mismatch (hostname / device_code)
- `10` — internal: a required tool is missing / state could not be established

Every refusal prints the same block to stderr: the claimed box, interface, host source, router
address, **expected** and **observed** LAN MAC, the method, and an explicit
`DO NOT FLASH, INSTALL, REBOOT OR sysupgrade` line. `--json` adds one machine-readable line for
evidence:

```json
{"status":"WRONG-DEVICE","exit":3,…,"expected_mac":"94:83:c4:aa:11:22","observed_mac":"04:ab:18:de:ad:01","method":"neigh"}
```

## What it does not prove

* It guards **this** box for **this** lane. A script that never calls it is unguarded — the guard
  makes the right thing easy, it cannot make an unmodified script safe. Wire the one-liner into the
  destructive entry points you use.
* MAC equality is *identity*, not *content*: it says nothing about which build, config or wallet the
  box carries. Pair it with the artifact-identity gate (`bench-deploy-apk.sh --verify-only`).
* `neigh` needs a successful TCP touch; on a box with no open port at all it fails closed (exit 4/7)
  rather than guessing. Use `--method ssh` there.
* A box that was re-flashed onto a *new* br-lan MAC will legitimately fail verification: that is the
  guard working. Re-`claim` (with `--force`) and say so out loud.
* It is not a lock. Two lanes can still drive the same box; that is `bench-lock.sh`'s job.

## The offline suite

```sh
tests/bench-device-identity/run-tests.sh          # 28 tests, 0 skips, no router, no network
```

The "rig" is a throw-away directory and `ip`, `curl` and `ssh` are PATH doubles
(`tests/bench-device-identity/harness/bin/`), so the **production guard text runs unmodified**. The
doubles refuse anything unbound (an `ssh` without `BindAddress=`, a `curl` without `--interface`)
— so a case cannot pass by making an unbindable check.

Covered: matching identity → pass; **wrong MAC → fail closed naming both MACs and the interface**;
unreachable → fail; missing / ambiguous / incomplete record → fail; address on a different interface
→ fail; unreadable MAC → fail; `claim` refusing to write (no MAC / different interface / not
reachable / conflicting record); stale neighbour entries in both directions; the source binding in
the invocation log; the JSON verdict; and the controls:

* **non-vacuity** — against the same fake rig, the unguarded flow really does flash the wrong box
  (that is the accident, reproduced) while the guarded flow refuses and flashes nothing;
* **positive control** — the guarded flow still flashes the *right* box;
* **mutation control** — with the MAC comparison mutated out of a copy of the guard, the same rig
  flashes the wrong box, proving the comparison (not the harness) is what stops it.
