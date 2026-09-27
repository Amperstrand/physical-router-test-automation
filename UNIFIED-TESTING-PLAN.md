# Unified Testing Framework Plan

## Goal

One test suite, one router abstraction, one film recorder — running against
any combination of phone client and router venue. `make test` just works,
regardless of whether you have a physical phone, a Cuttlefish emulator, or
just a Debian container.

## Architecture

```
pytest tests/
    ├── phone/          # requires a phone client (adb | cuttlefish | container)
    ├── browser/        # requires a browser (Playwright)
    ├── api/            # requires only router SSH
    └── unit/           # no infrastructure needed

Fixtures (conftest.py):
    router  → Router(host, jump_host, backend)     # SSH to any OpenWrt
    adb     → ADBDevice | CuttlefishClient | ContainerClient  # phone client
    wifi    → WiFi(adb, router, ssid)              # connect/disconnect
    cashu   → CashuFixture(mint_url)               # mint test tokens
    portal  → PortalConfig(type)                   # builtin | net4sats

Plugin (lib/film_recorder.py):
    --film  → screenrecord during phone tests → compose evidence film
```

## Client Matrix

| Client | Setup | Phone Tests | Browser Tests | Captive Portal UX | WiFi SSID |
|--------|-------|-------------|---------------|-------------------|-----------|
| `adb` (physical) | USB phone | ✅ | ✅ | ✅ real | ✅ real |
| `cuttlefish` | ai-legion CVD | ✅ | ✅ | ✅ real Android | ✅ hwsim |
| `container` | QEMU Debian | ⚠️ adapted | ✅ | ❌ no portal detection | ❌ wired |
| `mac` | local WiFi | ⚠️ desktop | ✅ | ⚠️ macOS portal | ✅ real |

## Router Matrix

| Router | OpenWrt | Setup | NDS | Notes |
|--------|---------|-------|-----|-------|
| Physical (GL-MT3000) | 24.10+ | `TOLLGATE_SSH_HOST=<ip>` | ✅ | Production-adjacent |
| QEMU virtual-lab | 24.10 | `virtual-lab.py start-poc` | ✅ | Fast iteration |
| Cuttlefish AP VM | 22.03→25.12 | managed by cvd | ⚠️ 22.03 workaround | Phone UX testing |
| SHC cloud | 24.10 | `cloud-lab.py submit` | ✅ | CI/fire-and-forget |

## Execution Paths

### 1. Developer iteration (fastest, no phone)
```bash
python3 scripts/virtual-lab.py start-poc --host localhost
pytest tests/api/ --client container --backend go
```

### 2. Phone UX testing (Cuttlefish)
```bash
bash scripts/connect-cuttlefish.sh ai-legion
pytest tests/phone/ --client cuttlefish --backend go --film
```

### 3. Physical router validation
```bash
export TOLLGATE_SSH_HOST=192.168.1.1
pytest tests/phone/ --client adb --backend go --film
```

### 4. CI (SHC cloud)
```bash
python3 scripts/cloud-lab.py submit --pr 42 --publish
```

### 5. Full film production
```bash
pytest tests/phone/ --client cuttlefish --backend go --film --film-narrate
```

## Implementation Status

### Done
- [x] CuttlefishClient (`lib/clients/cuttlefish.py`) — SSH-wrapped adb
- [x] Film recorder plugin (`lib/film_recorder.py`) — --film option
- [x] TESTING.md — architecture documentation
- [x] OpenWrt 25.12 compatibility assessment
- [x] connect-cuttlefish.sh — phone connectivity script

### Next (priority order)
1. **Upgrade Cuttlefish AP VM to OpenWrt 25.12** — eliminates NDS workaround
2. **Fix QEMU virtual-lab SSH** — the OpenWrt VM's dropbear rejects connections
3. **Run phone tests end-to-end** — verify the full framework works
4. **Add narration generation to --film-narrate** — edge-tts integration
5. **Integrate director.py chapter composition** — for polished films
6. **CI integration** — run tests automatically on PRs

## Migration from demo/ Lab

The demo/ directory (gitignored) contains the original Cuttlefish lab work.
Key artifacts to migrate:

| demo/ artifact | Target location | Status |
|----------------|-----------------|--------|
| `lab-remote.sh` | → `scripts/lab-remote.sh` | Migrate |
| `e2e-smoke.mjs` | → `tests/phone/test_smoke.py` (pytest port) | Rewrite |
| `director.py` | → `lib/film_director.py` | Migrate |
| `voice_guard.py` | → `lib/voice_guard.py` | Migrate |
| `cdp-driver.js` | → keep in demo/ (phone-specific) | Stay |
| `tg-pipeline.sh` | → replaced by `make film` | Obsolete |
| `net4sats-captive-server.py` | → obsolete (real daemon on AP VM) | Delete |

The demo/ directory remains as historical evidence but should not be the
canonical testing infrastructure.
