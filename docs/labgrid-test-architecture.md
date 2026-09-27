# Labgrid-Driven Test Architecture — DRY, Parameterized, Multi-Device

## The Vision

One test file per user story. Every device (phone, VM, emulator, ESP32)
runs the same story through a device-agnostic interface provided by
labgrid. Adding a new device = adding a labgrid resource + a thin adapter,
not writing new tests.

```
tests/stories/test_user_pays_and_gets_internet.py
  ├── runs against: Android phone (via labgrid ADB)
  ├── runs against: Debian VM (via labgrid SSH)
  ├── runs against: Arch/omarchy VM (via labgrid SSH)
  ├── runs against: Android emulator on ai-legion (via labgrid ADB)
  └── runs against: ESP32+QEMU chain (via labgrid network)
```

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                 USER STORY TESTS                     │
│  test_user_joins_and_sees_portal                    │
│  test_user_pays_and_gets_internet                   │
│  test_user_session_expires                          │
│  test_user_sees_degraded_mode                       │
│  test_tabs_no_copy_bleed                            │
└──────────────────────┬──────────────────────────────┘
                       │ uses
┌──────────────────────▼──────────────────────────────┐
│           DEVICE ABSTRACTION LAYER                   │
│  ClientDevice protocol:                             │
│    join_wifi(ssid, psk) → bool                      │
│    open_url(url) → bool                             │
│    get_ip() → str                                   │
│    has_internet() → bool                            │
│    screenshot(path) → bool                          │
│    type_text(text) → bool                           │
│    tap(x, y) → bool                                 │
└──────────────────────┬──────────────────────────────┘
                       │ implemented by
┌──────────┬───────────┼───────────┬─────────────────┐
│ ADB      │ SSH       │ SSH       │ ADB             │ Network
│ Device   │ Device    │ Device    │ Emulator        │ Device
│ (phone)  │ (Debian)  │ (omarchy) │ (ai-legion)     │ (ESP32)
└──────────┴───────────┴───────────┴─────────────────┘
       │            │           │          │              │
       └────────────┴───────────┴──────────┴──────────────┘
                                  │
                    ┌─────────────▼─────────────┐
                    │      LABGRID LAYER         │
                    │  places/resources/drivers  │
                    │  (already running for      │
                    │   ESP32 + ADB)             │
                    └───────────────────────────┘
```

## Labgrid Resources to Add

### Already in labgrid (or configured):
- ESP32 S3 board (USB-JTAG serial, `F4:12:FA:CF:03:84`)
- Android phone (ADB, `ZY326DPC7R`) — needs a labgrid place

### To add:
```yaml
# labgrid places for additional test devices
targets:
  debian-vm:
    resources:
      NetworkSSHDevice:
        address: "10.99.99.100"
        username: "debian"
    drivers:
      SSHDriver: {}

  omarchy-vm:
    resources:
      NetworkSSHDevice:
        address: "10.99.99.101"
        username: "root"
    drivers:
      SSHDriver: {}

  android-emulator:
    resources:
      AndroidEmulator:
        avd_name: "test_avd"
        host: "ai-legion"
    drivers:
      AndroidADBDriver: {}

  nr7101-router:
    resources:
      NetworkSSHDevice:
        address: "192.168.13.124"
        username: "root"
    drivers:
      SSHDriver: {}
```

## Device Adapter Protocol

```python
class ClientDevice(Protocol):
    """Any device that can act as a captive-portal client."""
    
    def join_wifi(self, ssid: str, psk: str = "") -> bool:
        """Connect to a WiFi network. Returns True on success."""
        ...
    
    def open_url(self, url: str) -> bool:
        """Open a URL in a browser. Returns True if the page loads."""
        ...
    
    def get_ip(self) -> str:
        """Return the device's IP address on the current network."""
        ...
    
    def has_internet(self, host: str = "8.8.8.8") -> bool:
        """Check if the device can reach the internet."""
        ...
    
    def screenshot(self, path: str) -> bool:
        """Capture a screenshot to the given path."""
        ...
    
    def submit_token(self, token: str) -> bool:
        """Submit a Cashu token through the captive portal."""
        ...
```

### ADB Adapter (Android phone/emulator):
```python
class ADBClientDevice(ClientDevice):
    def __init__(self, labgrid_target):
        self.adb = labgrid_target.get_driver(AndroidADBDriver)
    
    def join_wifi(self, ssid, psk=""):
        # cmd wifi connect-network (Android 10+)
        ...
    
    def open_url(self, url):
        # am start -a VIEW -d url
        ...
```

### SSH Adapter (Debian/omarchy VM):
```python
class SSHClientDevice(ClientDevice):
    def __init__(self, labgrid_target):
        self.ssh = labgrid_target.get_driver(SSHDriver)
    
    def join_wifi(self, ssid, psk=""):
        # nmcli device wifi connect (or wpa_supplicant for omarchy)
        ...
    
    def open_url(self, url):
        # curl or headless browser
        ...
```

## Parameterized Test Example

```python
import pytest

@pytest.mark.parametrize("device_place", [
    "android-phone",
    "debian-vm", 
    "android-emulator",
])
def test_user_pays_and_gets_internet(request, device_place):
    """User Story: A person connects to TollGate WiFi, pays a Cashu
    token at the captive portal, and gets internet access."""
    
    device = get_client_device(request, device_place)
    
    # Step 1: Join the TollGate WiFi
    assert device.join_wifi("TollGate-0805"), "WiFi join failed"
    assert device.get_ip().startswith("192.168."), "No IP assigned"
    
    # Step 2: Captive portal appears
    assert device.is_captive_portal_detected(), "Portal not detected"
    device.screenshot(f"{device_place}-portal.png")
    
    # Step 3: Submit a valid token
    token = mint_test_token(amount=4)
    assert device.submit_token(token), "Token submission failed"
    
    # Step 4: Internet is granted
    assert device.wait_for_internet(timeout=30), "No internet after payment"
    device.screenshot(f"{device_place}-internet.png")
    
    # Step 5: External IP is visible
    ext_ip = device.get_external_ip()
    assert ext_ip, "No external IP"
```

One test file. Three devices. Same assertions. Drift in any device's
behavior is caught immediately.

## Router-Under-Test Abstraction

Routers also need an abstraction (for asserting gate state, sessions, etc.):

```python
class RouterUnderTest(Protocol):
    def get_nds_state(self, client_mac: str) -> str: ...
    def get_session(self, client_ip: str) -> dict: ...
    def get_portal_url(self) -> str: ...
    def get_backend_url(self) -> str: ...
    def get_discovery_event(self) -> dict: ...
```

Implemented by:
- `SSHRouterDUT` (any OpenWrt router via labgrid SSH)
- `ESP32RouterDUT` (tollgate-core via serial/HTTP)
- `EmulatorRouterDUT` (QEMU OpenWrt via labgrid)

## Evidence Integration

The evidence recorder already captures video + screenshots. Extend it to
be device-agnostic:

```python
@pytest.fixture
def evidence(request, device):
    rec = EvidenceRecorder.for_device(device, request.node.name)
    rec.start_video()
    yield rec
    rec.stop_and_validate()  # includes zai-vision validation
```

## Migration Path

### Phase 1 (now): Labgrid places for existing devices
- Add the Android phone as a labgrid ADB resource
- Add the NR7101 as a labgrid SSH resource
- Keep existing test files working (no behavior change)

### Phase 2 (next sprint): Device abstraction + parameterized stories
- Implement `ClientDevice` protocol for ADB + SSH adapters
- Write 3-4 core user stories as parameterized tests
- Run against phone + Debian VM

### Phase 3 (ongoing): Add devices + stories
- Android emulator on ai-legion (labgrid ADB)
- omarchy VM (labgrid SSH)
- ESP32 chain (labgrid serial/network)
- New stories: session expiry, degraded mode, multi-device

### Phase 4 (CI integration): 
- Contract JSON as the expected-behavior registry
- Test matrix published per PR
- Drift = red cell = blocked PR

## Why Labgrid Specifically

1. **Already proven** — ESP32 flashing + ADB phone are working via labgrid
2. **Place-based acquisition** — tests don't care WHERE the device is, only
   that a place is available
3. **Coordinator mode** — multiple agents can share a device farm without
   stepping on each other (the hardware mutex problem we've already hit)
4. **Driver abstraction** — ADB, SSH, serial, network all look the same
   to the test code
5. **Remote places** — devices on other machines (ai-legion VMs) are
   first-class citizens via the labgrid exporter
