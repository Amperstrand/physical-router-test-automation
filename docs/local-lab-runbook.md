# Local Lab Runbook — Rebuild and Operate

How to bring the single-router local virtual lab back up from scratch or from
a snapshot, and the pitfalls that cost real debugging time. Topology and
commands assume the lab built by `scripts/virtual-lab.py`:

| Role | Address | Notes |
|---|---|---|
| OpenWrt router VM | `10.99.99.1` | QEMU, overlay `~/tollgate-virtual-lab/overlays/tollgate-poc.qcow2` |
| Debian 12 client VM | `10.99.99.100` | MAC `de:54:4e:91:49:da`, overlay `~/tollgate-virtual-lab/overlays/debian-client.qcow2` |
| Host bridge | `10.99.99.2` | Also serves the local CDK mint on `:8383` |
| Captive portal | `http://10.99.99.1:2050` | nodogsplash |
| Backend | `http://10.99.99.1:2121` | `tollgate-wrt` |

The lab password lives in `credentials/virtual-lab-credentials.json` and
`.env` (`TOLLGATE_SSH_PASSWORD`); both VMs use it for `root`.

## Fast path — snapshot restore (minutes)

If a verified-good internal snapshot exists on **both** overlays:

```bash
python3 scripts/virtual-lab.py stop-poc --host localhost

# List snapshots on both disks (the `snapshot` subcommand only knows the POC disk)
qemu-img snapshot -l ~/tollgate-virtual-lab/overlays/tollgate-poc.qcow2
qemu-img snapshot -l ~/tollgate-virtual-lab/overlays/debian-client.qcow2

# Roll both back to the same verified-good snapshot
qemu-img snapshot -a <name> ~/tollgate-virtual-lab/overlays/tollgate-poc.qcow2
qemu-img snapshot -a <name> ~/tollgate-virtual-lab/overlays/debian-client.qcow2

python3 scripts/virtual-lab.py start-poc --host localhost
```

With overlays already present, `start-poc` detects the provisioned boot and
skips serial-console provisioning, so this path takes minutes, not an hour.

Smoke checks after boot:

```bash
sshpass -p "$PW" ssh -o StrictHostKeyChecking=no root@10.99.99.1 'echo router-ok'
sshpass -p "$PW" ssh -o StrictHostKeyChecking=no root@10.99.99.100 'echo client-ok'
curl -s -o /dev/null -w '%{http_code}\n' http://10.99.99.1:2050/   # expect 200
sshpass -p "$PW" ssh -o StrictHostKeyChecking=no root@10.99.99.1 \
  'wget -qO- --timeout=3 http://127.0.0.1:2121/'                   # expect kind:10021 NAD event
```

## Cold path — full rebuild (~60 min)

```bash
python3 scripts/virtual-lab.py doctor --host localhost
python3 scripts/virtual-lab.py install-deps --host localhost        # first time only
python3 scripts/virtual-lab.py prepare-image --host localhost
python3 scripts/virtual-lab.py prepare-debian --host localhost
python3 scripts/virtual-lab.py start-poc --host localhost           # boots + provisions both VMs
python3 scripts/virtual-lab.py provision-debian --host localhost    # Chromium + Playwright in client VM
```

Gotchas specific to the cold path:

- **PEP 668**: Debian 12 refuses system-level `pip3 install` with
  `externally-managed-environment`. `provision-debian` passes
  `--break-system-packages` (Debian's supported escape hatch). If you install
  anything else into the client VM the same way, pass the same flag — or stage
  wheels host-side and copy them in (VM egress is unreliable, see pitfalls).
- **NDS pre-auth**: unauthenticated client traffic to the router is blocked
  *by design*. To provision or curl router-side endpoints from the client VM,
  authorize it first (`ndsctl auth de:54:4e:91:49:da` on the router) and
  `ndsctl deauth` it again afterwards so portal tests start from a clean state.
- **Password unification**: the same root password must be in `.env`
  (`TOLLGATE_SSH_PASSWORD` / `TOLLGATE_LUCI_PASSWORD`), in
  `credentials/virtual-lab-credentials.json`, and inside both VMs. If SSH
  suddenly fails after a rebuild, check these three match before debugging
  anything deeper.

## Running tests

```bash
./scripts/run-local-tests.sh tests/api/test_payment_regression.py
```

The script starts/stops the local CDK mint (`:8383`) itself and, when the
client VM answers and no `--client` flag is passed, defaults
`--client=container` so the portal e2e runs instead of silently skipping.

## Operational pitfalls

Each of these cost real time at least once:

1. **Never run `tollgate-wrt` with unknown flags.** It daemonizes silently,
   keeps running, and steals `:2121` from the service-managed instance.
   Version checks: `sha256sum /usr/bin/tollgate-wrt`, not `--version`.
2. **Deploying a binary**: stop the service → `scp -O` → start the service.
   Copying over the running binary fails with `Text file busy` (ETXTBSY).
   Afterwards verify *exactly one* daemon: `pgrep -f tollgate-wrt | wc -l`.
3. **Payments must come from the client VM** (10.99.99.100), as the **raw**
   token in the request body with `Content-Type: text/plain` — never
   JSON-wrapped. The backend rejects JSON-wrapped tokens.
4. **`rm /etc/tollgate/wallet.db` when switching mints.** A wallet.db carried
   over from another mint produces spend/swap errors that look like backend
   bugs.
5. **`ndsctl deauth <client-mac>` before payment tests.** Stale NDS auth
   state from a previous test makes portal flows pass or fail unpredictably.
6. **Mint lifecycle belongs to the runner.** Let `run-local-tests.sh`
   (its `start_mint`/EXIT-trap stop) own the `:8383` mint. A hand-started
   mint with a stale sqlite produces `Token Already Spent`-style failures;
   if the mint DB is stale, move it aside and let the runner start fresh.
7. **The local lab has ONE mint (`:8383`).** Cloud-lab fixtures — Nutshell V2
   on `:8384` and friends — do not exist here; mint-topology tests beyond a
   single mint skip or fail by design. (Tests that would hang the cashu CLI
   against absent mints skip via a reachability pre-check.)
8. **Disk precondition: ≥5 GB free.** qcow2 overlays grow and never shrink
   on their own. VM egress beyond the router is unreliable — deploy binaries
   via `scp` from the host, and stage pip wheels host-side before
   reprovisioning the client VM rather than downloading inside it.

## Snapshot discipline

- Snapshot **both** overlays, not just the POC disk —
  `scripts/virtual-lab.py snapshot ...` only manages
  `overlays/tollgate-poc.qcow2`; snapshot `overlays/debian-client.qcow2`
  manually so router and client roll back together.
- Take snapshots only at a **verified-good** state: portal `:2050` returns
  200, backend `:2121` serves the NAD event, and a payment smoke test has
  just passed. A snapshot of a half-broken lab reproduces that lab forever.
- Use **internal snapshots** (`qemu-img snapshot -c`) — they are delta-sized
  and need no extra files. Both VMs must be stopped (`stop-poc`) first.
