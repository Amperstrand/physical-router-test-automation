"""Unit tests for lib/labgrid_venue.py — guarded place binding + env export.

All coordinator interaction is faked via an injected runner: no hardware,
no labgrid package, no network.
"""
from __future__ import annotations

import subprocess

import pytest

from lib.lab_inventory import (
    CoordinatorEntry,
    LabInventory,
    RouterEntry,
    SwitchEntry,
)
from lib.labgrid_venue import (
    LabgridBench,
    LabgridBenchError,
    apply_venue_env,
    bench_for_router,
    venue_env,
)


def _inventory(router: RouterEntry, coordinator: str = "coord.example:20408") -> LabInventory:
    return LabInventory(
        switch=SwitchEntry(host="switch.example"),
        routers={router.name: router},
        coordinator=CoordinatorEntry(address=coordinator),
    )


def _router(**overrides) -> RouterEntry:
    base = dict(
        name="ap-lan2",
        poe_port="lan2",
        address="192.0.2.51",
        place="ap-lan2",
        keyfile="~/keys/bench_ed25519",
        jump_host="root@switch.example",
    )
    base.update(overrides)
    return RouterEntry(**base)


class FakeRunner:
    def __init__(self, failures: dict[str, str] | None = None):
        self.calls: list[list[str]] = []
        self.failures = failures or {}

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        verb = cmd[5] if len(cmd) > 5 else ""
        if verb in self.failures:
            return subprocess.CompletedProcess(
                cmd, returncode=1, stdout="", stderr=self.failures[verb]
            )
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")


# --------------------------------------------------------------------------- #
# bench_for_router guards — refusal happens BEFORE any coordinator call
# --------------------------------------------------------------------------- #


def test_protected_router_refused():
    inv = _inventory(_router(protected=True))
    with pytest.raises(LabgridBenchError, match="protected"):
        bench_for_router(inv, "ap-lan2")


def test_router_without_place_refused():
    inv = _inventory(_router(place=""))
    with pytest.raises(LabgridBenchError, match="no labgrid place"):
        bench_for_router(inv, "ap-lan2")


def test_router_without_address_refused():
    inv = _inventory(_router(address=""))
    with pytest.raises(LabgridBenchError, match="no address"):
        bench_for_router(inv, "ap-lan2")


def test_missing_coordinator_refused():
    inv = _inventory(_router(), coordinator="")
    with pytest.raises(LabgridBenchError, match="coordinator"):
        bench_for_router(inv, "ap-lan2")


def test_happy_binding_uses_inventory_place(monkeypatch):
    # Hermetic (module docstring contract): the binding must not require the
    # labgrid-client binary on the test host or in CI. Only the place/coordinator
    # wiring is asserted, so point the binary resolver at the bare name.
    monkeypatch.setenv("TOLLGATE_LABGRID_CLIENT", "labgrid-client")
    inv = _inventory(_router(place="ap-lan9"))
    binding = bench_for_router(inv, "ap-lan2")
    assert binding.bench.place == "ap-lan9"
    assert binding.bench.coordinator == "coord.example:20408"
    assert binding.entry.address == "192.0.2.51"


# --------------------------------------------------------------------------- #
# LabgridBench client plumbing
# --------------------------------------------------------------------------- #


def _bench(runner: FakeRunner) -> LabgridBench:
    bench = LabgridBench("coord.example:20408", "ap-lan2", runner=runner)
    bench._bin = "labgrid-client"
    return bench


def test_acquire_sends_place_and_coordinator():
    runner = FakeRunner()
    _bench(runner).acquire()
    assert runner.calls == [
        ["labgrid-client", "-x", "coord.example:20408", "-p", "ap-lan2", "acquire"]
    ]


def test_release_without_acquire_is_noop():
    runner = FakeRunner()
    _bench(runner).release()
    assert runner.calls == []


def test_release_after_acquire_sends_release_once():
    runner = FakeRunner()
    bench = _bench(runner)
    bench.acquire()
    bench.release()
    bench.release()
    assert [c[-1] for c in runner.calls] == ["acquire", "release"]


def test_power_states_map_to_on_off():
    runner = FakeRunner()
    bench = _bench(runner)
    bench.power(True)
    bench.power(False)
    assert [c[-1] for c in runner.calls] == ["on", "off"]


def test_client_failure_surfaces_stderr():
    runner = FakeRunner(failures={"acquire": "place is already acquired"})
    with pytest.raises(LabgridBenchError, match="place is already acquired"):
        _bench(runner).acquire()


def test_release_clears_acquired_even_on_failure():
    runner = FakeRunner(failures={"release": "coordinator gone"})
    bench = _bench(runner)
    bench.acquire()
    with pytest.raises(LabgridBenchError):
        bench.release()
    # A second release must not hit the (broken) coordinator again.
    bench.release()
    assert len(runner.calls) == 2


# --------------------------------------------------------------------------- #
# env export feeding the existing Router construction
# --------------------------------------------------------------------------- #


def test_venue_env_sets_host_key_and_jump():
    env = venue_env(_router())
    assert env["TOLLGATE_SSH_HOST"] == "192.0.2.51"
    assert env["TOLLGATE_SSH_KEY"].endswith("bench_ed25519")
    assert "~" not in env["TOLLGATE_SSH_KEY"]
    assert env["TOLLGATE_SSH_JUMP_HOST"] == "root@switch.example"


def test_venue_env_omits_optional_fields_when_empty():
    env = venue_env(_router(keyfile="", jump_host=""))
    assert "TOLLGATE_SSH_KEY" not in env
    assert "TOLLGATE_SSH_JUMP_HOST" not in env


def test_apply_venue_env_writes_target_mapping():
    target: dict[str, str] = {}
    apply_venue_env(_router(), target)
    assert target["TOLLGATE_SSH_HOST"] == "192.0.2.51"
