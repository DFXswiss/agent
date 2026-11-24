"""U-RUN-pool: clone provisioning, warm pool fill, take and purge (05-runner.md sections 3.2, 3.3, 4.5; ADR-009).

Lima is the recorded-output fake extended with real guest control processes (`guests.py`): a started instance
answers `GET /v1/health` and `POST /v1/assign` through a real Unix socket with the token the runner handed over as
Lima parameter. The store is a real PostgreSQL database.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_runner.errors import RunnerError
from agent_runner.guestlink.client import GuestClient, Health
from agent_runner.guestlink.errors import GuestUnauthorized
from agent_runner.vm import pool as pool_module
from agent_runner.vm import records
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.pool import SELFTEST_SCRIPT, SELFTEST_SET, VmSettings, format_time, scrub_param
from agent_runner.vm.records import GoldenRef, VmRow

from .conftest import recorded
from .guests import GOLDEN, NOW, ORG, PREFIX, SETTINGS, WG_A, WG_B, Harness

GOLDEN_REF = GoldenRef(version=GOLDEN, lima_name=f"{PREFIX}golden-{GOLDEN}")


def test_format_time_and_settings_from_config() -> None:
    assert format_time(datetime(2026, 11, 21, 12, 0, 1, 234567, tzinfo=UTC)) == "2026-11-21T12:00:01.234Z"

    class Config:
        vm_cpus, vm_memory_mib, vm_disk_gib, pool_size, memory_reserve_mib, vm_idle_seconds = 2, 4096, 30, 3, 1024, 60
        sessions_per_vm, max_vms = 0, 7

    assert VmSettings.from_config(Config()) == VmSettings(  # type: ignore[arg-type]
        cpus=2, memory_mib=4096, disk_gib=30, pool_size=3, memory_reserve_mib=1024, vm_idle_seconds=60,
        sessions_per_vm=0, max_vms=7,
    )  # fmt: skip


def test_selftest_provisioning_is_a_valid_yq_append() -> None:
    assert SELFTEST_SET.startswith(".provision += [")
    item = json.loads(SELFTEST_SET.removeprefix(".provision += [").removesuffix("]"))
    assert item == {"mode": "system", "script": SELFTEST_SCRIPT}
    assert SELFTEST_SCRIPT.startswith("#!/bin/sh\n")
    assert "agent_guest.sandbox.selftest" in SELFTEST_SCRIPT
    assert "After=agent-guest-api.service" in SELFTEST_SCRIPT


def test_scrub_param(tmp_path: Path) -> None:
    path = tmp_path / "lima.yaml"
    path.write_text('param:\n  AGENT_GUEST_CONTROL_TOKEN: "secret-value"\nother: 1\n', encoding="utf-8")
    path.chmod(0o644)
    assert scrub_param(str(tmp_path)) is True
    assert path.read_text(encoding="utf-8") == 'param:\n  AGENT_GUEST_CONTROL_TOKEN: ""\nother: 1\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    # Already blank (and the comment line of the template) stays as it is.
    path.write_text(
        '#   AGENT_GUEST_CONTROL_TOKEN="<token>"\nparam:\n  AGENT_GUEST_CONTROL_TOKEN: ""\n', encoding="utf-8"
    )
    before = path.read_text(encoding="utf-8")
    assert scrub_param(str(tmp_path)) is False
    assert path.read_text(encoding="utf-8") == before


# -- provisioning -----------------------------------------------------------------------------------------------


def test_fill_one_pool_vm(harness: Harness) -> None:
    harness.add_golden()

    async def body(manager: VmManager) -> Any:
        return await manager.pool.fill_one(GOLDEN_REF)

    row = harness.run(body)
    assert row.state == "pool"
    assert row.wg_id is None
    assert row.id.startswith("vm-")
    assert row.lima_name.startswith(f"{PREFIX}arp-")
    # The Lima name and the VM ID are independent random values.
    assert row.lima_name.removeprefix(f"{PREFIX}arp-") != row.id.removeprefix("vm-")
    clone = next(argv for argv, _ in harness.fake.calls if argv[0] == "clone")
    assert clone[:2] == ["clone", "--tty=false"]
    assert clone[-2:] == [GOLDEN_REF.lima_name, row.lima_name]
    assert [clone[i] for i in (3, 5, 7)] == [".cpus = 4", '.memory = "8GiB"', SELFTEST_SET]
    start = next(argv for argv, _ in harness.fake.calls if argv[0] == "start")
    token = harness.tokens_dir.joinpath(f"{row.id}.token").read_text(encoding="ascii")
    assert start == ["start", "-y", "--set", f'.param.AGENT_GUEST_CONTROL_TOKEN="{token}"', row.lima_name]
    assert len(token) == 43
    assert stat.S_IMODE(harness.tokens_dir.joinpath(f"{row.id}.token").stat().st_mode) == 0o600
    assert stat.S_IMODE(harness.tokens_dir.stat().st_mode) == 0o700
    assert row.control_token_hash == records.token_hash(token)
    # The parameter is gone from the instance configuration after the first start.
    lima_yaml = (harness.fake.directory(row.lima_name) / "lima.yaml").read_text(encoding="utf-8")
    assert token not in lima_yaml
    assert 'AGENT_GUEST_CONTROL_TOKEN: ""' in lima_yaml
    assert harness.events[-1] == {
        "vm_id": row.id,
        "wg_id": None,
        "state": "pool",
        "golden_version": GOLDEN,
        "cpus": 4,
        "memory_mib": 8192,
        "disk_gib": 20,
        "at": "2026-11-21T12:00:00.000Z",
    }
    assert ("vm.pool_filled", {"vm_id": row.id, "lima_name": row.lima_name}) in harness.logs


def test_clone_failure_retries_with_a_new_name(harness: Harness) -> None:
    harness.add_golden()
    harness.fake.fail["start"] = (1, "", "FATA[0001] boom\n")

    async def body(manager: VmManager) -> Any:
        row = await manager.pool.fill_one(GOLDEN_REF)
        return row, manager.env.start_failures

    row, failures = harness.run(body)
    clones = [argv[-1] for argv, _ in harness.fake.calls if argv[0] == "clone"]
    deletes = [argv[-1] for argv, _ in harness.fake.calls if argv[0] == "delete"]
    assert len(clones) == 2
    assert clones[0] != clones[1]
    assert deletes == [clones[0]]
    assert row.lima_name == clones[1]
    assert failures == 1
    failed = [fields for name, fields in harness.logs if name == "vm.start_failed"]
    assert failed == [{"vm_id": row.id, "lima_name": clones[0], "attempt": 1, "code": "lima_failed"}]
    assert ("vm.lima_detail", {"lima_name": clones[0], "operation": "start"}) in harness.logs


def test_second_failure_is_vm_start_failed(harness: Harness) -> None:
    harness.add_golden()
    harness.fake.dead.update({"x"})

    def fail_clone(argv: list[str]) -> None:
        if argv[0] == "clone":
            harness.fake.fail["clone"] = recorded("clone-missing.txt")

    harness.fake.hook = fail_clone

    async def body(manager: VmManager) -> Any:
        with pytest.raises(RunnerError) as caught:
            await manager.pool.fill_one(GOLDEN_REF)
        return caught.value.code, manager.env.start_failures, manager.pool.filling

    assert harness.run(body) == ("vm_start_failed", 2, 0)
    assert harness.vms() == []
    assert list(harness.tokens_dir.glob("*.token")) == []
    assert [name for name, _ in harness.logs].count("vm.start_failed") == 2
    # A missing-instance failure of `clone` has no Lima detail to log... but the recorded one has stderr.
    assert [name for name, _ in harness.logs].count("vm.lima_detail") == 2


def test_guest_that_never_gets_healthy(harness: Harness) -> None:
    harness.add_golden()
    harness.fake.dead.update(f"{PREFIX}arp-{suffix}" for suffix in ("00000001", "00000002"))
    hexes = iter(["0000000a", "00000001", "00000002"])

    async def body(manager: VmManager) -> Any:
        manager.env.random_hex = lambda _n: next(hexes)
        with pytest.raises(RunnerError) as caught:
            await manager.pool.fill_one(GOLDEN_REF)
        return caught.value.code

    assert harness.run(body) == "vm_start_failed"
    codes = [fields["code"] for name, fields in harness.logs if name == "vm.start_failed"]
    assert codes == ["vm_health_timeout", "vm_health_timeout"]
    assert harness.fake.instances.keys() == {GOLDEN_REF.lima_name}
    # Health was polled every 100 ms for 30 s.
    assert harness.clock.sleeps.count(0.1) >= 2 * 299


def test_files_error_counts_as_start_failure(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    harness.add_golden()
    calls = iter([True, False])

    def scrub(_directory: str) -> bool:
        if next(calls):
            raise OSError("disk")
        return True

    monkeypatch.setattr(pool_module, "scrub_param", scrub)

    async def body(manager: VmManager) -> Any:
        return await manager.pool.fill_one(GOLDEN_REF)

    row = harness.run(body)
    assert [fields["code"] for name, fields in harness.logs if name == "vm.start_failed"] == ["vm_files"]
    assert row.state == "pool"


def test_selftest_failure_and_timeout(harness: Harness) -> None:
    harness.add_golden()
    answers = iter(
        [
            (0, json.dumps({"ok": False, "checks": []}), ""),
            (0, "not json", ""),
        ]
    )

    def shell(argv: list[str]) -> None:
        if argv[0] == "shell":
            harness.fake.selftest = next(answers)

    harness.fake.hook = shell

    async def body(manager: VmManager) -> Any:
        with pytest.raises(RunnerError) as caught:
            await manager.pool.fill_one(GOLDEN_REF)
        return caught.value.code

    assert harness.run(body) == "vm_start_failed"
    names = [fields for name, fields in harness.logs if name == "vm.start_failed"]
    assert [fields["code"] for fields in names] == ["vm_selftest_failed", "vm_selftest_failed"]
    assert harness.vms() == []


def test_selftest_waits_for_the_result(harness: Harness) -> None:
    harness.add_golden()
    pending = [(1, "", "cat: no such file\n")] * 3

    def shell(argv: list[str]) -> None:
        if argv[0] == "shell":
            harness.fake.selftest = pending.pop() if pending else (0, json.dumps({"ok": True}), "")

    harness.fake.hook = shell

    async def body(manager: VmManager) -> Any:
        return await manager.pool.fill_one(GOLDEN_REF)

    assert harness.run(body).state == "pool"
    assert harness.clock.sleeps.count(0.5) == 3


def test_selftest_result_never_appears(harness: Harness) -> None:
    harness.add_golden()
    harness.fake.selftest = (1, "", "cat: no such file\n")

    async def body(manager: VmManager) -> Any:
        with pytest.raises(RunnerError):
            await manager.pool.fill_one(GOLDEN_REF)

    harness.run(body)
    assert [fields.get("code") for name, fields in harness.logs if name == "vm.start_failed"] == [
        "vm_selftest_failed",
        "vm_selftest_failed",
    ]


def test_store_failure_after_provisioning_removes_the_instance(harness: Harness) -> None:
    harness.add_golden()
    hexes = iter(["00000001", "00000002", "00000001", "00000003"])

    async def body(manager: VmManager) -> Any:
        manager.env.random_hex = lambda _n: next(hexes)
        await manager.pool.fill_one(GOLDEN_REF)
        # Same VM ID again: the token file exists already, so the fill fails after the instance was created.
        with pytest.raises(RunnerError) as caught:
            await manager.pool.fill_one(GOLDEN_REF)
        return caught.value.code

    assert harness.run(body) == "vm_token_exists"
    assert sorted(harness.fake.instances) == [f"{PREFIX}arp-00000002", GOLDEN_REF.lima_name]
    assert [row["id"] for row in harness.vms()] == ["vm-00000001"]
    assert [path.name for path in harness.tokens_dir.glob("*.token")] == ["vm-00000001.token"]


# -- refill, take, purge ----------------------------------------------------------------------------------------


def test_refill_fills_up_to_pool_size_two_in_parallel(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = VmSettings(**{**SETTINGS.__dict__, "pool_size": 3})
    in_flight: list[int] = []

    async def body(manager: VmManager) -> Any:
        original = manager.provisioner.create

        async def create(*args: Any, **kwargs: Any) -> str:
            in_flight.append(manager.pool.filling)
            return await original(*args, **kwargs)

        manager.provisioner.create = create  # type: ignore[method-assign]
        added = await manager.refill()
        again = await manager.refill()
        return added, again, await manager.capacity()

    added, again, capacity = harness.run(body)
    assert (added, again) == (3, 0)
    assert max(in_flight) == 2
    assert capacity.pool_ready == 3
    assert capacity.slots_free == 1
    assert [row["state"] for row in harness.vms()] == ["pool"] * 3


def test_refill_only_into_free_slots(harness: Harness) -> None:
    harness.add_golden()
    harness.physical_mib = 32768 + 8192

    async def body(manager: VmManager) -> Any:
        return await manager.refill()

    assert harness.run(body) == 1
    harness.physical_mib = 1024
    assert harness.run(body) == 0


def test_refill_stops_after_a_failed_round(harness: Harness) -> None:
    harness.add_golden()
    harness.fake.selftest = (0, json.dumps({"ok": False}), "")

    async def body(manager: VmManager) -> Any:
        return await manager.refill()

    assert harness.run(body) == 0
    assert harness.vms() == []


def test_refill_reraises_cancellation(harness: Harness) -> None:
    harness.add_golden()

    async def body(manager: VmManager) -> Any:
        async def cancelled(_golden: GoldenRef, *, reserved: bool = False) -> VmRow:
            raise asyncio.CancelledError

        manager.pool.fill_one = cancelled  # type: ignore[method-assign,assignment]
        with pytest.raises(asyncio.CancelledError):
            await manager.pool.refill(GOLDEN_REF)

    harness.run(body)


def test_refill_without_golden_or_disabled(harness: Harness) -> None:
    async def body(manager: VmManager) -> Any:
        return await manager.refill(), await manager.pool.missing(None), await manager.pool.refill(None)

    assert harness.run(body) == (0, 0, 0)
    assert ("vm.pool_no_golden", {}) in harness.logs
    harness.add_golden()
    harness.settings = VmSettings(**{**SETTINGS.__dict__, "pool_size": 0})
    assert harness.run(body)[0] == 0
    assert harness.vms() == []


def test_take_assigns_the_oldest_pool_vm(harness: Harness) -> None:
    harness.add_golden()

    async def body(manager: VmManager) -> Any:
        first = await manager.pool.fill_one(GOLDEN_REF)
        harness.now = NOW + timedelta(seconds=1)
        await manager.pool.fill_one(GOLDEN_REF)
        taken = await manager.pool.take(GOLDEN_REF, ORG, WG_A)
        assert taken is not None
        async with await manager.client(taken) as client:
            health = await client.health()
        return first, taken, health, manager._refill_wanted.is_set()

    first, taken, health, woken = harness.run(body)
    assert taken.id == first.id
    assert (taken.state, taken.wg_id, taken.org_id) == ("running", WG_A, ORG)
    assert taken.assigned_at is not None
    assert (health.state, health.wg_id, health.org_id) == ("assigned", WG_A, ORG)
    assert [event["state"] for event in harness.events if event["vm_id"] == first.id] == [
        "pool",
        "assigning",
        "running",
    ]
    assert woken is True


def test_take_skips_a_broken_pool_vm(harness: Harness) -> None:
    harness.add_golden()

    async def body(manager: VmManager) -> Any:
        first = await manager.pool.fill_one(GOLDEN_REF)
        second = await manager.pool.fill_one(GOLDEN_REF)
        # The first pool VM's guest went away while it waited in the pool.
        harness.fake.farm.halt(first.lima_name)  # type: ignore[union-attr]
        taken = await manager.pool.take(GOLDEN_REF, ORG, WG_A)
        empty = await manager.pool.take(GOLDEN_REF, ORG, WG_B)
        return first, second, taken, empty

    first, second, taken, empty = harness.run(body)
    assert taken.id == second.id
    assert empty is None
    states = {row["id"]: row["state"] for row in harness.vms()}
    assert states == {first.id: "deleted", second.id: "running"}
    assert ("vm.assign_failed", {"vm_id": first.id, "code": "guest_unreachable"}) in harness.logs
    assert first.lima_name not in harness.fake.instances
    assert not harness.tokens_dir.joinpath(f"{first.id}.token").exists()


def test_take_ignores_pool_vms_of_another_golden(harness: Harness) -> None:
    harness.add_golden()

    async def body(manager: VmManager) -> Any:
        await manager.pool.fill_one(GOLDEN_REF)
        return await manager.pool.take(GoldenRef(version="g2", lima_name="x"), ORG, WG_A)

    assert harness.run(body) is None


def test_purge_deletes_pool_vms_of_a_retired_golden(harness: Harness) -> None:
    harness.add_golden()

    async def fill(manager: VmManager) -> Any:
        return await manager.refill()

    assert harness.run(fill) == 2
    old = {row["id"] for row in harness.live()}
    harness.sql("UPDATE runner.golden SET state = 'retired'")
    harness.add_golden("g2", minute=5)
    assert harness.run(fill) == 2
    rows = harness.vms()
    assert {row["id"] for row in rows if row["state"] == "deleted"} == old
    assert {row["golden_version"] for row in rows if row["state"] == "pool"} == {"g2"}
    harness.sql("UPDATE runner.golden SET state = 'retired'")

    async def purge(manager: VmManager) -> Any:
        return await manager.pool.purge(None)

    assert harness.run(purge) == 2
    assert harness.live() == []


# -- VmEnv helpers ----------------------------------------------------------------------------------------------


def test_wait_health_outcomes(harness: Harness) -> None:
    class Fake:
        def __init__(self, answers: list[Any]) -> None:
            self.answers = answers

        async def health(self, *, timeout: float | None = None) -> Health:
            value = self.answers.pop(0)
            if isinstance(value, Exception):
                raise value
            assert isinstance(value, Health)
            return value

    def health(state: str, wg: str | None = None) -> Health:
        return Health(state=state, guest_version="0", claude_version="0", landlock_abi=4, boot_id="b", org_id=None,
                      wg_id=wg, sessions=())  # fmt: skip

    async def body(manager: VmManager) -> Any:
        env = manager.env
        accept_unassigned = pool_module._unassigned
        results = []
        for answers in ([health("broken")], [health("assigned", WG_A)]):
            with pytest.raises(RunnerError) as caught:
                await env.wait_health(Fake(answers), accept_unassigned)  # type: ignore[arg-type]
            results.append(caught.value.code)
        ok = await env.wait_health(Fake([GuestUnauthorized("health"), health("unassigned")]), accept_unassigned)  # type: ignore[arg-type]
        return results, ok.state

    assert harness.run(body) == (["vm_broken", "vm_health_unexpected"], "unassigned")


def test_instance_dir_and_client(harness: Harness) -> None:
    harness.add_golden()

    async def body(manager: VmManager) -> Any:
        env = manager.env
        with pytest.raises(RunnerError) as missing:
            await env.instance_dir(f"{PREFIX}arp-ffffffff")
        row = await manager.pool.fill_one(GOLDEN_REF)
        env.dirs.clear()
        path = await env.socket_path(row.lima_name)
        client = await env.client(row)
        await client.aclose()
        return missing.value.code, path, isinstance(client, GuestClient)

    code, path, is_client = harness.run(body)
    assert code == "vm_instance_missing"
    assert path.endswith(os.path.join("sock", "agent-guest.sock"))
    assert is_client


def test_destroy_of_a_missing_instance_is_fine(harness: Harness) -> None:
    async def body(manager: VmManager) -> Any:
        await manager.env.destroy(f"{PREFIX}arp-ffffffff")

    harness.run(body)


def test_store_insert_failure_removes_instance_and_token(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    harness.add_golden()

    async def insert(*_args: Any, **_kwargs: Any) -> Any:
        raise RunnerError("store_unavailable")

    monkeypatch.setattr(records, "insert", insert)

    async def body(manager: VmManager) -> Any:
        with pytest.raises(RunnerError) as caught:
            await manager.pool.fill_one(GOLDEN_REF)
        return caught.value.code

    assert harness.run(body) == "store_unavailable"
    assert sorted(harness.fake.instances) == [GOLDEN_REF.lima_name]
    assert list(harness.tokens_dir.glob("*.token")) == []


def test_parameter_that_cannot_be_blanked_fails_the_start(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    harness.add_golden()
    monkeypatch.setattr(pool_module, "scrub_param", lambda _directory: False)

    async def body(manager: VmManager) -> Any:
        with pytest.raises(RunnerError):
            await manager.pool.fill_one(GOLDEN_REF)

    harness.run(body)
    assert [f["code"] for n, f in harness.logs if n == "vm.start_failed"] == ["vm_param_scrub", "vm_param_scrub"]


def test_take_interrupted_and_state_changed(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    harness.add_golden()

    async def body(manager: VmManager) -> Any:
        first = await manager.pool.fill_one(GOLDEN_REF)
        harness.now = NOW + timedelta(seconds=1)
        second = await manager.pool.fill_one(GOLDEN_REF)
        original = records.transition
        calls = {"n": 0}

        async def transition(db: Any, vm_id: str, expect: Any, **fields: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # the first VM was moved by somebody else meanwhile
            if calls["n"] == 2:
                raise asyncio.CancelledError
            return await original(db, vm_id, expect, **fields)

        monkeypatch.setattr(records, "transition", transition)
        with pytest.raises(asyncio.CancelledError):
            await manager.pool.take(GOLDEN_REF, ORG, WG_A)
        return first, second

    first, second = harness.run(body)
    states = {row["id"]: row["state"] for row in harness.vms()}
    assert states == {first.id: "deleted", second.id: "deleted"}
    assert ("vm.assign_failed", {"vm_id": first.id, "code": "vm_state_changed"}) in harness.logs


def test_purge_skips_a_pool_vm_taken_meanwhile(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    harness.add_golden()

    async def body(manager: VmManager) -> Any:
        await manager.pool.fill_one(GOLDEN_REF)

        async def transition(*_a: Any, **_k: Any) -> Any:
            return None

        monkeypatch.setattr(records, "transition", transition)
        return await manager.pool.purge(None)

    assert harness.run(body) == 0
    assert [row["state"] for row in harness.vms()] == ["pool"]
