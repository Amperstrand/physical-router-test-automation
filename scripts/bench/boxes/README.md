# Box records — what physically lives at an address

One file per box: `<name>.identity`, plain `key=value`, `#` comments. Written by

```sh
scripts/bench/device-identity.sh claim --name <box> --iface <iface> --src <addr>
```

and read by the preflight that must run before ANY destructive step:

```sh
scripts/bench/device-identity.sh verify --name <box> || exit $?
```

- `box` — the record's name (must match the file name)
- `iface` — the **host** interface to use, the near end of the cable
- `src_addr` — the **host source address** on that interface (BindAddress for ssh, --interface for probes)
- `router_ip` — the box's address as seen from that interface
- `lan_mac` — the box's own br-lan MAC, lowercase colon form
- `hostname` — optional: `uci get system.@system[0].hostname`, checked by `--check-hostname`
- `device_code` — optional: a short code for the model (e.g. `mt3000`, `cudy-wr3000`)
- `claimed_at` / `claimed_by` / `claimed_via` — provenance: when, from which host, and how the MAC was read

The device pin, in one line: `scripts/bench/device-identity.sh verify --name <box>`

Rules the guard enforces:

* A record without `iface`, `src_addr`, `router_ip` or `lan_mac` is **refused** (exit 6).
* **Two records claiming the same `box` are refused** (exit 6) — never guess which one is the bench.
  Keep exactly one per box; rename or delete stale copies.
* `claim` will not overwrite a record for the same box that names a different MAC without `--force`.

Example: [`example.identity.txt`](example.identity.txt) — deliberately NOT named `*.identity`, so it
can sit next to a real record without ever being read as one (the guard only ever reads
`*.identity`, and two matching records for one box would be refused as ambiguous anyway). The real
records are machine-local (the host interface and source address are host-specific) and the
directory is overridable with `DI_BOXES_DIR`; keep the ones you commit free of anything you would
not publish.

Full rationale, methods and exit codes: [`docs/bench-device-identity.md`](../../docs/bench-device-identity.md).
