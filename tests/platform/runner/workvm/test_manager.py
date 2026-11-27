"""Work VM manager (docs/platform/spec/13-work-vm.md sections 2, 3 and 7) against a migrated store, a fake Lima, a
fake job helper and a fake guest."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import time
from dataclasses import replace
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_runner.errors import RunnerError
from agent_runner.github import snapshot
from agent_runner.guestlink.errors import GuestRefused, GuestUnreachable
from agent_runner.operations.extensions import OperationFailed
from agent_runner.store.migrate import migrate
from agent_runner.vm.lima import LimaError
from agent_runner.workvm import firewall, records
from agent_runner.workvm import manager as manager_module
from agent_runner.workvm.jobs import ShellResult, WorkVmError
from agent_runner.workvm.manager import NO_LEASE, NO_VM, CallFailed, WorkVms, sizes_fit

from .kit import CHILD, SESSION, START, TASK, TASK_B, VM_ID, Job, World, pack_tar, session


@pytest.fixture
def world(runner_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    assert migrate(runner_dsn).status == "applied"
    created = World(dsn=runner_dsn, tmp=tmp_path)

    async def guest_for(env: Any, local: Any) -> Any:
        if env == "no-guest":
            raise snapshot.NoGuest
        return created.guest()

    monkeypatch.setattr(snapshot, "guest_for", guest_for)
    return created


def rows(world: World) -> list[dict[str, Any]]:
    with psycopg.connect(world.dsn, autocommit=True, row_factory=dict_row) as conn:
        return list(conn.execute("SELECT id, state, slot, scope, size FROM runner.workvm ORDER BY slot, id"))


async def request(vms: WorkVms, params: dict[str, Any] | None = None, local: Any = None) -> dict[str, Any]:
    return await vms.request(local or session(), params or {})


# -- request / release -----------------------------------------------------------------------------------------


def test_request_clones_starts_and_returns_the_vm(world: World) -> None:
    result = world.run(request)
    assert result["created"] is True
    assert result["state"] == "running"
    assert (result["scope"], result["size"], result["cpus"], result["memory_mib"], result["disk_gib"]) == (
        "task",
        "standard",
        4,
        8192,
        50,
    )
    assert result["workvm_id"].startswith("wvm-")
    assert result["ready_ms"] == 0
    [row] = rows(world)
    assert (row["state"], row["slot"]) == ("running", 0)
    [sets] = world.sets.values()
    first, second = firewall.resolvers()
    assert sets == [
        ".cpus=4",
        '.memory="8GiB"',
        '.disk="50GiB"',
        ".ssh.localPort=47200",
        f'.dns=["{first}","{second}"]',
    ]
    assert any(line.startswith("runner.workvm created") for line in world.logs)


def test_session_scope_gets_its_own_vm_and_its_commands_use_it(world: World) -> None:
    async def body(vms: WorkVms) -> tuple[Any, Any, Any]:
        task_vm = await request(vms)
        own = await request(vms, {"scope": "session", "size": "large"}, session(CHILD))
        used = await records.for_session(vms.deps.db, task_id=TASK, session_id=CHILD)
        return task_vm, own, used

    task_vm, own, used = world.run(body)
    assert own["workvm_id"] != task_vm["workvm_id"]
    assert own["scope"] == "session"
    assert own["cpus"] == 8
    assert used is not None
    assert used.id == own["workvm_id"]
    assert [row["slot"] for row in rows(world)] == [0, 1]


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"size": "xlarge"}, "limit_exceeded"),
        ({"image": None}, "workvm_unavailable"),
        ({"boundary": False}, "workvm_unavailable"),
        ({"slots": 0}, "workvm_unavailable"),
    ],
)
def test_requests_that_cannot_be_served(world: World, change: dict[str, Any], code: str) -> None:
    params = {"size": change.pop("size")} if "size" in change else {}
    if "slots" in change:
        world.settings = manager_module.Settings(**{**world.settings.__dict__, "slots": 0})
        change.clear()
    for key, value in change.items():
        setattr(world, key, value)
    with pytest.raises(OperationFailed) as caught:
        world.run(lambda vms: request(vms, params))
    assert caught.value.code == code
    assert caught.value.retryable is False


@pytest.mark.parametrize("limit", ["max_per_task", "max_per_org", "max_per_owner"])
def test_limits_per_task_org_and_owner(world: World, limit: str) -> None:
    world.settings = manager_module.Settings(**{**world.settings.__dict__, limit: 1})

    async def body(vms: WorkVms) -> None:
        await request(vms, {}, session(task_id=TASK))
        same_task = limit == "max_per_task"
        await request(
            vms, {"scope": "session"} if same_task else {}, session(CHILD, task_id=TASK if same_task else TASK_B)
        )

    with pytest.raises(OperationFailed) as caught:
        world.run(body)
    assert caught.value.code == "limit_exceeded"


def test_a_request_waits_in_the_queue_until_capacity_is_free(world: World) -> None:
    world.free_mib = 0

    def free_after_two_polls(w: World) -> None:
        if len(w.sleeps) == 2:
            w.free_mib = 10_000

    world.on_sleep = free_after_two_polls
    result = world.run(request)
    assert result["created"] is True
    assert world.sleeps == [2.0, 2.0]
    assert result["ready_ms"] == 4000


def test_a_request_gives_up_after_the_queue_time(world: World) -> None:
    world.settings = manager_module.Settings(**{**world.settings.__dict__, "slots": 1})

    async def body(vms: WorkVms) -> None:
        await request(vms)
        await request(vms, {}, session(CHILD, task_id=TASK_B))

    with pytest.raises(OperationFailed) as caught:
        world.run(body)
    assert caught.value.code == "timeout"
    assert len(world.sleeps) == 5


def test_a_queued_request_waits_for_the_one_before_it(world: World) -> None:
    async def body(vms: WorkVms) -> int:
        vms.queue.append("earlier")

        def leave(w: World) -> None:
            if "earlier" in vms.queue:
                vms.queue.remove("earlier")

        world.on_sleep = leave
        await request(vms)
        return len(world.sleeps)

    assert world.run(body) == 1


def test_a_taken_slot_is_a_retryable_failure(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    async def taken(db: Any, vm: Any) -> Any:
        raise RunnerError("workvm_exists")

    monkeypatch.setattr(records, "insert", taken)
    with pytest.raises(OperationFailed) as caught:
        world.run(request)
    assert caught.value.code == "timeout"
    assert caught.value.retryable is True


@pytest.mark.parametrize("failing", ["clone", "start"])
def test_a_vm_that_does_not_start_is_removed(world: World, failing: str) -> None:
    world.fail.add(failing)
    with pytest.raises(OperationFailed) as caught:
        world.run(request)
    assert caught.value.code == "upstream_error"
    assert caught.value.retryable is True
    assert [row["state"] for row in rows(world)] == ["deleted"]
    assert world.instances == {}
    assert any("start_failed" in line for line in world.logs)


def test_a_vm_whose_helper_never_answers_is_removed(world: World) -> None:
    world.helper_fail["list"] = ShellResult(255, b"", False)
    world.fail.add("delete")
    with pytest.raises(OperationFailed):
        world.run(request)
    assert [row["state"] for row in rows(world)] == ["deleting"]  # the sweep deletes it again (KOR-43)
    assert any("delete_failed" in line for line in world.logs)
    assert world.sleeps[-1] == 0.5


def test_the_clone_resolves_through_cloudflare(world: World) -> None:
    world.run(request)
    [sets] = world.sets.values()
    first, second = firewall.resolvers()
    assert sets[-1] == f'.dns=["{first}","{second}"]'


def test_release_deletes_the_vm_of_the_scope(world: World) -> None:
    async def body(vms: WorkVms) -> tuple[Any, Any, Any]:
        nothing = await vms.release(session(), {})
        created = await request(vms)
        released = await vms.release(session(), {"scope": "task"})
        return nothing, created, released

    nothing, created, released = world.run(body)
    assert nothing == {"released": False}
    assert released == {"released": True, "workvm_id": created["workvm_id"]}
    assert [row["state"] for row in rows(world)] == ["deleted"]
    assert world.instances == {}
    session_release = world.run(lambda vms: vms.release(session(), {"scope": "session"}))
    assert session_release == {"released": False}


# -- lifecycle -------------------------------------------------------------------------------------------------


# -- tool calls ------------------------------------------------------------------------------------------------


def test_exec_mirrors_the_workspace_runs_and_returns_the_output(world: World) -> None:
    world.workspace = {"a.txt": b"one", "src/b.py": b"print(1)\n"}
    world.scripts["make test"] = (b"x" * 40000 + b"done\n", "exited", 0)

    async def body(vms: WorkVms) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        await request(vms)
        first = await vms.call(
            None,
            session(),
            "workvm_exec",
            {
                "command": "make test",
                "cwd": "/workspace/src",
                "env": [{"name": "CI", "value": "1"}],
                "stdin": "y\n",
                "timeout_seconds": 60,
            },
        )
        second = await vms.call(None, session(), "workvm_exec", {"command": "make test"})
        world.workspace = {"a.txt": b"two"}
        third = await vms.call(None, session(), "workvm_exec", {"command": "make test"})
        return first, second, third

    first, second, third = world.run(body)
    assert first["state"] == "exited"
    assert first["exit_code"] == 0
    assert first["command"] == "make test"
    assert first["output"].endswith("done\n")
    assert len(first["output"]) == 32768
    assert first["output_bytes"] == 40005
    assert first["output_start"] == 40005 - 32768
    assert first["synced"] == {"files": 2, "bytes": first["synced"]["bytes"], "deleted": 0}
    assert second["synced"]["files"] == 0
    assert second["synced"]["deleted"] == 0
    assert third["synced"]["files"] == 1
    assert third["synced"]["deleted"] == 1
    assert world.started[0] == {
        "command": "make test",
        "env": {"CI": "1"},
        "stdin": "y\n",
        "cwd": "/workspace/src",
        "timeout": "60",
    }
    assert world.started[1]["cwd"] == "/workspace"
    assert world.started[1]["timeout"] == "0"
    assert len(world.helper.applied) == 2  # nothing to send the second time


def guest_version(world: World, version: str) -> None:
    """The session's VM (`kit.VM_ID`) runs a guest of `version` (its golden record)."""
    with psycopg.connect(world.dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, state)"
            " VALUES ('g1', 'golden-g1', '1', %s, 'sha256:x', 'ready')",
            (version,),
        )
        conn.execute(
            "INSERT INTO runner.vm (id, lima_name, golden_version, state, control_token_hash, cpus, memory_mib)"
            " VALUES (%s, %s, 'g1', 'running', 'x', 1, 1024)",
            (VM_ID, VM_ID),
        )


def test_a_guest_before_the_known_export_gets_full_exports(world: World) -> None:
    guest_version(world, "0.7.0")
    world.workspace = {"a": b"1"}

    async def body(vms: WorkVms) -> None:
        await request(vms)
        await vms.call(None, session(), "workvm_exec", {"command": "true"})
        await vms.call(None, session(), "workvm_exec", {"command": "true"})

    world.run(body)
    assert world.known == [None, None]


def test_mirror_sends_only_changed_files_across_the_guest_link(world: World) -> None:
    """FIX-02: after the first mirror, the export names the mirrored files as known, so an unchanged workspace
    crosses the guest link as a manifest only and a changed file alone with its content."""
    guest_version(world, "0.8.0")
    world.workspace = {f"repo/f{index}.txt": b"x" * 16384 for index in range(20)}

    async def body(vms: WorkVms) -> list[dict[str, Any]]:
        await request(vms)
        first = await vms.call(None, session(), "workvm_exec", {"command": "true"})
        second = await vms.call(None, session(), "workvm_exec", {"command": "true"})
        world.workspace = {**world.workspace, "repo/f3.txt": b"changed"}
        third = await vms.call(None, session(), "workvm_exec", {"command": "true"})
        return [first["synced"], second["synced"], third["synced"]]

    synced = world.run(body)
    assert [item["files"] for item in synced] == [20, 0, 1]
    assert world.known == [None, 20, 20]
    full, unchanged, one = world.transferred
    assert full > 20 * 16384
    assert unchanged < full // 20  # the manifest, no content
    assert one < full // 20  # the manifest and the one changed file
    pushes = [line for line in world.logs if line.startswith("runner.workvm push ")]
    assert len(pushes) == 3
    assert f"session_id={SESSION}" in pushes[0]
    assert "files=0 bytes=0 deleted=0" in pushes[1]
    for line in pushes:
        assert {"transferred", "export_ms", "apply_ms", "total_ms"} <= {
            part.split("=", 1)[0] for part in line.split() if "=" in part
        }


@pytest.mark.parametrize("refusal", ["request_invalid", "size_exceeded"])
def test_mirror_through_a_guest_without_known_exports_everything(world: World, refusal: str) -> None:
    guest_version(world, "0.8.0")  # a golden record that claims more than the guest knows
    world.workspace = {"a": b"1", "b": b"2"}

    async def body(vms: WorkVms) -> list[int]:
        await request(vms)
        await vms.call(None, session(), "workvm_exec", {"command": "true"})
        world.old_guest = GuestRefused("files_export", 400 if refusal == "request_invalid" else 413, refusal)
        world.workspace = {"a": b"1", "b": b"3"}
        second = await vms.call(None, session(), "workvm_exec", {"command": "true"})
        return [second["synced"]["files"], second["synced"]["deleted"]]

    assert world.run(body) == [1, 0]
    assert world.known == [None, 2, None]  # refused with known, then the full export
    assert len(world.helper.applied) == 2


def test_an_export_that_leaves_out_a_needed_file_is_repeated_in_full(world: World) -> None:
    guest_version(world, "0.8.0")
    world.workspace = {"a": b"1", "b": b"2"}

    async def body(vms: WorkVms) -> dict[str, Any]:
        await request(vms)
        await vms.call(None, session(), "workvm_exec", {"command": "true"})
        world.workspace = {"a": b"1", "b": b"changed"}
        world.also_omit = ["b"]  # a guest that leaves out the changed file
        second = await vms.call(None, session(), "workvm_exec", {"command": "true"})
        return dict(second["synced"])

    assert world.run(body)["files"] == 1
    assert world.known == [None, 2, None]
    _root, pushed = world.helper.applied[-1]
    with tarfile.open(fileobj=io.BytesIO(pushed)) as archive:
        assert archive.extractfile("b").read() == b"changed"  # type: ignore[union-attr]


def test_push_export_refuses_an_export_without_the_needed_content(world: World, tmp_path: Path) -> None:
    from .kit import export_tar  # noqa: PLC0415

    exported = tmp_path / "partial.tar"
    exported.write_bytes(export_tar({"a": b"1"}, known=[], also_omit=["a"]))

    async def body(vms: WorkVms) -> str:
        await request(vms)
        vm = await vms._vm_of(session())
        assert vm is not None
        try:
            await vms.push_export(vm, session(), ".", exported)
        except WorkVmError as error:
            return error.code
        return "pushed"

    assert world.run(body) == "workvm_sync_failed"


def test_mirrors_of_one_session_run_one_after_the_other(world: World) -> None:
    """A repeated call while the first mirror still runs waits for it and then sends only what is left."""
    guest_version(world, "0.8.0")
    world.workspace = {"a": b"1", "b": b"2"}

    async def body(vms: WorkVms) -> list[int]:
        await request(vms)
        calls = [vms.call(None, session(), "workvm_sync", {"direction": "to_vm"}) for _ in range(2)]
        first, second = await asyncio.gather(*calls)
        return [first["files"], second["files"]]

    # whichever call takes the lock first mirrors both files; the other then finds nothing left to send
    assert sorted(world.run(body)) == [0, 2]
    assert world.known == [None, 2]
    assert len(world.helper.applied) == 1


def test_exec_without_sync_and_a_failing_start(world: World) -> None:
    async def body(vms: WorkVms) -> dict[str, Any]:
        await request(vms)
        result = await vms.call(None, session(), "workvm_exec", {"command": "true", "sync": "none"})
        world.helper_fail["start"] = ShellResult(1, b"", False)
        try:
            await vms.call(None, session(), "workvm_exec", {"command": "true", "sync": "none"})
        except CallFailed as failed:
            result["failed"] = failed.error
        return result

    result = world.run(body)
    assert "synced" not in result
    assert result["state"] == "running"
    assert result["failed"] == "unavailable"
    assert world.exports == []


def test_output_kill_and_status(world: World) -> None:
    world.scripts["serve"] = (b"line\n", "running", None)

    async def body(vms: WorkVms) -> tuple[Any, Any, Any, Any]:
        await request(vms)
        started = await vms.call(None, session(), "workvm_exec", {"command": "serve", "sync": "none"})
        job = started["job_id"]
        output = await vms.call(None, session(), "workvm_output", {"job_id": job, "offset": 2, "limit": 2})
        killed = await vms.call(None, session(), "workvm_kill", {"job_id": job, "signal": "KILL"})
        status = await vms.call(None, session(CHILD), "workvm_status", {})
        return started, output, killed, status

    started, output, killed, status = world.run(body)
    assert output["output"] == "ne"
    assert output["output_start"] == 2
    assert output["command"] == "serve"
    assert killed["state"] == "killed"
    assert killed["exit_code"] == 143
    assert status["workvm"]["state"] == "running"
    assert status["workvm"]["size"] == "standard"
    assert status["jobs"][0]["job_id"] == started["job_id"]
    assert status["jobs"][0]["started_at"] == "2023-11-14T22:13:20.000Z"


def test_unknown_jobs_and_invalid_answers(world: World) -> None:
    async def body(vms: WorkVms) -> list[str]:
        await request(vms)
        errors = []
        for arguments in ({"job_id": "j-000000000000"},):
            try:
                await vms.call(None, session(), "workvm_output", arguments)
            except CallFailed as failed:
                errors.append(failed.error)
        world.helper_fail["wait"] = ShellResult(0, b"garbage", False)
        try:
            await vms.call(None, session(), "workvm_output", {"job_id": "j-000000000000"})
        except CallFailed as failed:
            errors.append(failed.error)
        return errors

    assert world.run(body) == ["not_found", "unavailable"]


def test_sync_to_vm_of_a_path_and_from_vm(world: World) -> None:
    world.workspace = {"repos/o/r/a.py": b"old", "repos/o/r/keep.env": b"local", "other.txt": b"x"}

    async def body(vms: WorkVms) -> tuple[Any, Any, Any]:
        await request(vms)
        whole = await vms.call(None, session(), "workvm_sync", {"direction": "to_vm"})
        part = await vms.call(None, session(), "workvm_sync", {"direction": "to_vm", "path": "repos/o/r"})
        world.helper.pack = pack_tar({"a.py": b"new", "b.py": b"added"}, excluded=["keep.env"])
        back = await vms.call(None, session(), "workvm_sync", {"direction": "from_vm", "path": "repos/o/r"})
        return whole, part, back

    whole, part, back = world.run(body)
    assert whole == {
        "direction": "to_vm",
        "path": None,
        "files": 3,
        "bytes": whole["bytes"],
        "deleted": 0,
        "skipped": 0,
    }
    assert part["path"] == "repos/o/r"
    assert part["files"] == 2
    assert back["direction"] == "from_vm"
    assert back["path"] == "repos/o/r"
    assert (back["files"], back["deleted"]) == (2, 0)
    assert back["skipped"] >= 1
    [(directory, data, replace)] = world.imports
    assert directory == "repos/o/r"
    assert replace is True
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        contents = {m.name: archive.extractfile(m).read() for m in archive if m.isfile()}  # type: ignore[union-attr]
    assert contents == {"a.py": b"new", "b.py": b"added", "keep.env": b"local"}


def test_from_vm_into_a_new_directory_and_its_errors(world: World) -> None:
    async def body(vms: WorkVms) -> list[str]:
        await request(vms)
        world.helper.pack = pack_tar({"shot.png": b"\x89PNG"})
        world.export_error = GuestRefused("files", 404, "not_found")
        result = await vms.call(None, session(), "workvm_sync", {"direction": "from_vm", "path": "out"})
        assert result["files"] == 1
        errors = []
        for arguments, setup in (
            ({"direction": "from_vm"}, None),
            ({"direction": "from_vm", "path": "out"}, ("helper", ShellResult(1, b"", False))),
            ({"direction": "from_vm", "path": "out"}, ("truncated", ShellResult(0, b"", True))),
            ({"direction": "to_vm", "path": "bad"}, ("export", GuestRefused("files", 400, "path_invalid"))),
            ({"direction": "to_vm"}, ("export", GuestRefused("files", 400, "size_exceeded"))),
            ({"direction": "to_vm"}, ("export", GuestUnreachable("files"))),
            ({"direction": "from_vm", "path": "out"}, ("import", GuestRefused("files", 400, "path_invalid"))),
            ({"direction": "from_vm", "path": "out"}, ("import", GuestRefused("files", 413, "size_exceeded"))),
            ({"direction": "from_vm", "path": "out"}, ("import", GuestUnreachable("files"))),
        ):
            world.helper_fail.pop("pack", None)
            world.export_error = GuestRefused("files", 404, "not_found")
            world.import_error = None
            if setup is not None:
                kind, value = setup
                if kind in ("helper", "truncated"):
                    world.helper_fail["pack"] = value  # type: ignore[assignment]
                elif kind == "export":
                    world.export_error = value  # type: ignore[assignment]
                else:
                    world.import_error = value  # type: ignore[assignment]
            try:
                await vms.call(None, session(), "workvm_sync", arguments)
            except CallFailed as failed:
                errors.append(f"{failed.error}:{failed.message[:12]}")
        return errors

    assert world.run(body) == [
        "params_invalid:from_vm need",
        "unavailable:The work VM ",
        "unavailable:The work VM ",
        "params_invalid:The path is ",
        "unavailable:The files ar",
        "unavailable:The workspac",
        "params_invalid:The path is ",
        "unavailable:The files ar",
        "unavailable:The workspac",
    ]


def test_a_failing_apply_and_a_missing_guest(world: World) -> None:
    world.workspace = {"a": b"1"}

    async def body(vms: WorkVms) -> list[str]:
        await request(vms)
        errors = []
        world.helper_fail["apply"] = ShellResult(1, b"", False)
        try:
            await vms.call(None, session(), "workvm_sync", {"direction": "to_vm"})
        except CallFailed as failed:
            errors.append(failed.error)
        world.helper_fail.clear()
        for direction in ("to_vm", "from_vm"):
            world.helper.pack = pack_tar({})
            try:
                await vms.call("no-guest", session(), "workvm_sync", {"direction": direction, "path": "x"})
            except CallFailed as failed:
                errors.append(failed.error)
        return errors

    assert world.run(body) == ["unavailable", "unavailable", "unavailable"]


def test_a_job_missing_from_the_list_has_no_label(world: World) -> None:
    async def body(vms: WorkVms) -> str:
        created = await request(vms)
        vm = await records.get(vms.deps.db, created["workvm_id"])
        assert vm is not None
        world.helper.jobs["j-111111111111"] = Job(command="other")
        return await vms._label(vm, "j-000000000000")

    assert world.run(body) == ""


def test_a_second_request_returns_the_existing_vm(world: World) -> None:
    async def body(vms: WorkVms) -> tuple[dict[str, Any], dict[str, Any], str]:
        first = await request(vms)
        again = await request(vms, {"size": "large"})
        await records.update(vms.deps.db, first["workvm_id"], state="starting")
        try:
            await request(vms)
        except WorkVmError as error:
            return first, again, error.code
        return first, again, ""

    first, again, code = world.run(body)
    assert again["created"] is False
    assert again["workvm_id"] == first["workvm_id"]
    assert again["size"] == "standard"
    assert code == "workvm_gone"


def test_ensure_running_accepts_only_running_vms(world: World) -> None:
    async def body(vms: WorkVms) -> list[str]:
        created = await request(vms)
        vm = await records.get(vms.deps.db, created["workvm_id"])
        assert vm is not None
        assert (await vms.ensure_running(vm)).state == "running"
        codes = []
        for state in ("starting", "deleted"):
            await records.update(vms.deps.db, vm.id, state=state)
            try:
                await vms.ensure_running(vm)
            except WorkVmError as error:
                codes.append(error.code)
        return codes

    assert world.run(body) == ["workvm_gone", "workvm_gone"]


def test_idle_pass_tears_down_only_idle_vms_without_running_jobs(world: World) -> None:
    async def body(vms: WorkVms) -> list[list[str]]:
        created = await request(vms)
        first = await vms.idle_pass()
        world.now += timedelta(seconds=601)
        world.scripts["sleep 100"] = (b"", "running", None)
        await vms.call(None, session(), "workvm_exec", {"command": "sleep 100", "sync": "none", "wait_seconds": 0})
        world.now += timedelta(seconds=601)
        busy = await vms.idle_pass()
        world.helper.jobs.clear()
        world.helper_fail["list"] = ShellResult(1, b"", False)  # an unreadable job list counts as no job
        world.fail.add("delete")
        removed = await vms.idle_pass()
        assert (await records.get(vms.deps.db, created["workvm_id"])).state == "deleting"  # type: ignore[union-attr]
        world.fail.discard("delete")
        assert await vms.sweep() == 1  # KOR-43: a failed deletion is repeated by the sweep
        assert (await records.get(vms.deps.db, created["workvm_id"])).state == "deleted"  # type: ignore[union-attr]
        return [first, busy, removed]

    first, busy, removed = world.run(body)
    assert first == []
    assert busy == []
    assert len(removed) == 1
    assert any("delete_failed" in line for line in world.logs)
    deleted = [body for _, kind, body in world.events if kind == "workvm.state" and body["state"] == "deleted"]
    assert deleted[0]["reason"] == "idle"
    assert deleted[0]["runtime_ms"] == 1202000
    lost = [body for _, kind, body in world.events if kind == "workvm.run" and body["state"] == "lost"]
    assert len(lost) == 1
    assert lost[0]["command"] == "sleep 100"


def test_reconcile_deletes_every_left_over_vm(world: World) -> None:
    async def body(vms: WorkVms) -> None:
        await request(vms)
        await request(vms, {}, session(CHILD, task_id=TASK_B))
        world.instances["wp-t-wvm-orphan"] = "Running"
        world.instances["wp-t-other"] = "Running"
        fresh = WorkVms(vms.deps)
        await fresh.reconcile()

    world.run(body)
    assert [row["state"] for row in rows(world)] == ["deleted", "deleted"]
    assert set(world.instances) == {"wp-t-other"}
    assert any("orphan_deleted" in line for line in world.logs)
    reasons = [
        body["reason"] for _, kind, body in world.events if kind == "workvm.state" and body["state"] == "deleted"
    ]
    assert reasons == ["restart", "restart"]


async def _call(vms: WorkVms, *, tool: str, arguments: dict[str, Any]) -> Any:
    return await vms.call(None, session(), tool, arguments)


def test_calls_without_a_vm(world: World) -> None:
    status = world.run(lambda vms: vms.call(None, session(), "workvm_status", {}))
    assert status == {"workvm": None, "jobs": []}
    for tool, arguments in (("workvm_output", {"job_id": "j-000000000000"}), ("workvm_sync", {"direction": "to_vm"})):
        with pytest.raises(CallFailed) as caught:
            world.run(partial(_call, tool=tool, arguments=arguments))
        assert (caught.value.error, caught.value.message) == ("not_found", NO_VM)


def test_exec_obtains_a_vm_implicitly(world: World) -> None:
    result = world.run(lambda vms: vms.call(None, session(), "workvm_exec", {"command": "true", "sync": "none"}))
    assert result["state"] == "running"
    starting, created = [body for _, kind, body in world.events if kind == "workvm.state"]
    assert (starting["state"], starting["workvm_id"], starting["implicit"]) == ("starting", created["workvm_id"], True)
    assert created["implicit"] is True
    assert created["state"] == "created"
    assert created["reason"] is None
    world.boundary = False
    with pytest.raises(CallFailed) as caught:
        world.run(lambda vms: vms.call(None, session(CHILD, task_id=TASK_B), "workvm_exec", {"command": "true"}))
    assert caught.value.error == "unavailable"
    assert "No work VM could be obtained" in caught.value.message


def test_without_the_hub_lease_exec_and_browse_are_refused_and_the_other_tools_go_on(world: World) -> None:
    """ADR-087 section 4: no command with internet egress, and no implicit VM, after the failover time without hub."""
    held = [False]

    async def body(vms: WorkVms) -> tuple[list[str], dict[str, Any]]:
        vms.deps.hub_lease = lambda: held[0]
        refused: list[str] = []
        for tool, arguments in (("workvm_exec", {"command": "true"}), ("workvm_browse", {"url": "https://x.test"})):
            with pytest.raises(CallFailed) as caught:
                await vms.call(None, session(), tool, arguments)
            assert caught.value.message == NO_LEASE
            refused.append(caught.value.error)
        assert await records.live(vms.deps.db) == []  # no implicit VM was started
        status = await vms.call(None, session(), "workvm_status", {})
        held[0] = True
        await vms.call(None, session(), "workvm_exec", {"command": "true", "sync": "none"})
        return refused, status

    refused, status = world.run(body)
    assert refused == ["unavailable", "unavailable"]
    assert status == {"workvm": None, "jobs": []}
    assert sum("lease_lost" in line for line in world.logs) == 2
    assert len(rows(world)) == 1


def test_the_lease_is_held_by_default(world: World) -> None:
    assert world.run(lambda vms: asyncio.sleep(0, vms.deps.hub_lease())) is True


def test_reconcile_deletes_only_the_vms_left_over_from_before(world: World) -> None:
    async def body(vms: WorkVms) -> None:
        await request(vms)
        fresh = WorkVms(vms.deps)
        left = await fresh.left_over()
        await request(vms, {}, session(CHILD, task_id=TASK_B))  # obtained while the test runs were reconciled
        await fresh.reconcile(left)

    world.run(body)
    assert [row["state"] for row in rows(world)] == ["deleted", "running"]
    assert len(world.instances) == 1


def test_status_while_starting_and_with_unreadable_jobs(world: World) -> None:
    async def body(vms: WorkVms) -> tuple[Any, Any]:
        created = await request(vms)
        world.helper_fail["list"] = ShellResult(1, b"", False)
        unreadable = await vms.call(None, session(), "workvm_status", {})
        await records.update(vms.deps.db, created["workvm_id"], state="deleting")
        deleting = await vms.call(None, session(), "workvm_status", {})
        return unreadable, deleting

    unreadable, deleting = world.run(body)
    assert unreadable["jobs"] == []
    assert unreadable["workvm"]["state"] == "running"
    assert deleting["workvm"]["state"] == "stopping"
    assert deleting["jobs"] == []


def test_a_call_to_a_vm_that_is_going_away(world: World) -> None:
    async def body(vms: WorkVms) -> str:
        created = await request(vms)
        await records.update(vms.deps.db, created["workvm_id"], state="deleting")
        try:
            await vms.call(None, session(), "workvm_exec", {"command": "true", "sync": "none"})
        except CallFailed as failed:
            return failed.error
        return ""

    assert world.run(body) == "unavailable"


def test_runs_are_recorded_when_they_start_and_end(world: World) -> None:
    world.scripts["make"] = (b"ok\n", "running", None)

    async def body(vms: WorkVms) -> None:
        await request(vms)
        started = await vms.call(None, session(), "workvm_exec", {"command": "make", "sync": "none"})
        job = world.helper.jobs[started["job_id"]]
        await vms.call(None, session(), "workvm_output", {"job_id": started["job_id"], "wait_seconds": 0})
        job.state, job.exit_code = "exited", 2
        await vms.idle_pass()  # settles the open run
        await vms.call(None, session(), "workvm_output", {"job_id": started["job_id"]})  # already recorded

    world.run(body)
    runs = [(identity, body) for identity, kind, body in world.events if kind == "workvm.run"]
    assert [body["state"] for _, body in runs] == ["running", "exited"]
    identity, final = runs[-1]
    assert identity.session_id == SESSION
    assert identity.task_id == TASK
    assert final["exit_code"] == 2
    assert final["ended_at"] is not None
    assert final["output"] == "ok\n"
    assert final["kind"] == "exec"
    assert final["command"] == "make"


def test_deleting_a_vm_that_is_not_running_and_one_already_gone(world: World) -> None:
    async def body(vms: WorkVms) -> None:
        created = await request(vms)
        vm = await records.update(vms.deps.db, created["workvm_id"], state="starting")
        assert vm is not None
        await vms.delete(vm, reason="failed")
        await vms.delete(vm, reason="failed")  # already deleted: nothing happens

    world.run(body)
    assert [row["state"] for row in rows(world)] == ["deleted"]
    assert len([1 for _, kind, body in world.events if kind == "workvm.state" and body["state"] == "deleted"]) == 1


def test_a_failing_event_does_not_break_the_vm(world: World) -> None:
    world.emit_error = RuntimeError("outbox")
    result = world.run(request)
    assert result["created"] is True
    assert any("event_failed kind=workvm.state" in line for line in world.logs)


def test_teardown_records_finished_and_unreadable_runs(world: World) -> None:
    world.scripts["a"] = (b"", "running", None)
    world.scripts["b"] = (b"", "running", None)

    async def body(vms: WorkVms) -> None:
        await request(vms)
        first = await vms.call(None, session(), "workvm_exec", {"command": "a", "sync": "none"})
        await vms.call(None, session(), "workvm_exec", {"command": "b", "sync": "none"})
        world.helper.jobs[first["job_id"]].state = "exited"
        world.helper.jobs[first["job_id"]].exit_code = 0
        del world.helper.jobs[next(job for job in world.helper.jobs if job != first["job_id"])]
        await vms.release(session(), {})

    world.run(body)
    finals = [body["state"] for _, kind, body in world.events if kind == "workvm.run" and body["ended_at"]]
    assert sorted(finals) == ["exited", "lost"]


def test_session_end_tears_down_session_and_task_vms(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    active = {"n": 1}

    async def active_sessions(db: Any, task_id: str, *, except_id: str | None = None) -> int:
        return active["n"]

    monkeypatch.setattr(records, "active_sessions", active_sessions)

    async def body(vms: WorkVms) -> None:
        await request(vms)
        await request(vms, {"scope": "session"}, session(CHILD))
        await vms.session_ended(session(CHILD))  # its own VM goes, the task VM stays: the main session is active
        assert [row["state"] for row in rows(world)] == ["running", "deleted"]
        active["n"] = 0
        await vms.session_ended(session())
        await vms.delete(await records.get(vms.deps.db, rows(world)[0]["id"]), reason="released")  # type: ignore[arg-type]

    world.run(body)
    assert [row["state"] for row in rows(world)] == ["deleted", "deleted"]
    reasons = [
        body["reason"] for _, kind, body in world.events if kind == "workvm.state" and body["state"] == "deleted"
    ]
    assert reasons == ["session_ended", "session_ended"]


def test_browse_reads_a_page_and_brings_a_screenshot_into_the_workspace(world: World) -> None:
    answer = {
        "final_url": "https://example.com/",
        "status": 200,
        "title": "Example",
        "text": "Hello",
        "screenshot": "/var/lib/workvm/screenshots/1.png",
    }
    world.scripts[manager_module.BROWSE_COMMAND] = (b"log\n" + json.dumps(answer).encode() + b"\n", "exited", 0)

    async def body(vms: WorkVms) -> tuple[Any, Any, Any]:
        read = await vms.call(None, session(), "workvm_browse", {"url": "https://example.com/"})
        shot = await vms.call(
            None, session(), "workvm_browse", {"url": "https://example.com/", "action": "screenshot", "path": "s/a.png"}
        )
        default = await vms.call(
            None, session(), "workvm_browse", {"url": "https://example.com/", "action": "screenshot"}
        )
        return read, shot, default

    read, shot, default = world.run(body)
    assert (read["title"], read["status"], read["text"], read["screenshot"]) == ("Example", 200, "Hello", None)
    assert shot["screenshot"] == "s/a.png"
    assert default["screenshot"] == f".workvm/screenshots/{default['job_id']}.png"
    assert [directory for directory, _, _ in world.imports] == ["s", ".workvm/screenshots"]
    spec = world.started[0]
    assert json.loads(spec["env"]["WORKVM_BROWSE"]) == {"action": "read", "url": "https://example.com/"}
    kinds = {body["kind"] for _, kind, body in world.events if kind == "workvm.run"}
    assert kinds == {"browse"}


def test_browse_without_an_answer_and_a_failing_file_fetch(world: World) -> None:
    world.scripts[manager_module.BROWSE_COMMAND] = (b"crash\nnot json {\n", "exited", 1)

    async def body(vms: WorkVms) -> tuple[Any, str]:
        failed = await vms.call(None, session(), "workvm_browse", {"url": "https://example.com/"})
        world.scripts[manager_module.BROWSE_COMMAND] = (
            json.dumps({"screenshot": "/x.png", "status": 1000}).encode() + b"\n",
            "exited",
            0,
        )
        world.helper_fail["packfile"] = ShellResult(1, b"", False)
        try:
            await vms.call(None, session(), "workvm_browse", {"url": "https://example.com/", "action": "screenshot"})
        except CallFailed as error:
            return failed, error.error
        return failed, ""

    failed, error = world.run(body)
    assert failed["status"] is None
    assert failed["title"] == ""
    assert failed["text"] == ""
    assert error == "unavailable"


def test_browse_while_still_running_returns_the_output(world: World) -> None:
    world.scripts[manager_module.BROWSE_COMMAND] = (b"loading\n", "running", None)
    result = world.run(lambda vms: vms.call(None, session(), "workvm_browse", {"url": "https://example.com/"}))
    assert result["text"] == "loading\n"


def test_a_screenshot_into_the_workspace_root(world: World) -> None:
    world.scripts[manager_module.BROWSE_COMMAND] = (json.dumps({"screenshot": "/x.png"}).encode() + b"\n", "exited", 0)
    world.export_error = GuestRefused("files", 404, "not_found")
    world.run(
        lambda vms: vms.call(
            None, session(), "workvm_browse", {"url": "https://example.com/", "action": "screenshot", "path": "a.png"}
        )
    )
    assert world.imports[0][0] == "."


def test_sizes_fit() -> None:
    assert sizes_fit(["standard", "large", "xlarge"], "large") == ["standard", "large"]


def test_guest_texts() -> None:
    assert manager_module._guest_text("other").startswith("The workspace could not be accessed")
    assert json.loads(json.dumps(manager_module.SIZES)) == {
        "standard": [4, 8192, 50],
        "large": [8, 16384, 100],
        "xlarge": [16, 32768, 200],
    }
    assert SESSION != CHILD


def test_without_an_event_writer_nothing_is_written() -> None:
    nothing: Any = None
    deps = manager_module.Deps(
        db=nothing,
        settings=nothing,
        now=nothing,
        lima_for=nothing,
        shell_for=nothing,
        base_for=nothing,
        boundary_ok=nothing,
        free_memory_mib=nothing,
        log=print,
    )
    identity = manager_module.Identity("o", "w", "t", "s")
    emitted: Any = deps.emit(identity, "workvm.state", {})
    assert asyncio.run(emitted) is None


def test_a_session_of_another_task_never_reaches_the_task_vm(world: World) -> None:
    """Threat model "another task's session uses this VM": the VM is resolved from the caller's session only."""

    async def body(vms: WorkVms) -> tuple[Any, Any]:
        own = await request(vms)
        foreign = session(CHILD, task_id=TASK_B)
        status = await vms.call(None, foreign, "workvm_status", {})
        with pytest.raises(CallFailed) as caught:
            await vms.call(None, foreign, "workvm_output", {"job_id": "j-000000000000"})
        released = await vms.release(foreign, {})
        return own, (status, caught.value.error, released)

    own, (status, error, released) = world.run(body)
    assert status == {"workvm": None, "jobs": []}
    assert error == "not_found"
    assert released["released"] is False
    assert [row["state"] for row in rows(world)] == ["running"]
    assert own["created"] is True


def test_a_failing_boundary_check_tears_down_every_live_vm(world: World) -> None:
    async def body(vms: WorkVms) -> tuple[list[str], list[str], list[str]]:
        await request(vms)
        await request(vms, {}, session(CHILD, task_id=TASK_B))
        healthy = await vms.guard_pass()
        world.boundary = False
        failed = await vms.guard_pass()
        again = await vms.guard_pass()
        return healthy, failed, again

    healthy, failed, again = world.run(body)
    assert (healthy, len(failed), again) == ([], 2, [])
    assert [row["state"] for row in rows(world)] == ["deleted", "deleted"]
    reasons = [
        body["reason"] for _, kind, body in world.events if kind == "workvm.state" and body["state"] == "deleted"
    ]
    assert reasons == ["failed", "failed"]
    assert any("boundary_failed deleted=2" in line for line in world.logs)


def test_the_guard_does_nothing_without_slots(world: World) -> None:
    world.settings = manager_module.Settings(**{**world.settings.__dict__, "slots": 0})
    world.boundary = False
    assert world.run(lambda vms: vms.guard_pass()) == []


def test_a_failing_wait_for_capacity_leaves_the_queue(world: World) -> None:
    world.free_mib = 0

    def boom(_world: World) -> None:
        raise RuntimeError("cancelled")

    world.on_sleep = boom

    async def body(vms: WorkVms) -> list[str]:
        with pytest.raises(RuntimeError):
            await request(vms)
        return list(vms.queue)

    assert world.run(body) == []


def test_a_subagent_works_in_its_own_vm_never_in_the_task_vm(world: World) -> None:
    child = session(CHILD, parent_id=SESSION)

    async def body(vms: WorkVms) -> tuple[Any, Any, Any]:
        main = await request(vms)
        before = await vms.call(None, child, "workvm_status", {})
        result = await vms.call(None, child, "workvm_exec", {"command": "true", "sync": "none"})
        after = await vms.call(None, child, "workvm_status", {})
        return main, before, (result, after)

    main, before, (result, after) = world.run(body)
    assert before == {"workvm": None, "jobs": []}
    assert result["state"] == "running"
    assert after["workvm"]["workvm_id"] != main["workvm_id"]
    assert after["workvm"]["scope"] == "session"
    assert sorted(row["scope"] for row in rows(world)) == ["session", "task"]


@pytest.mark.parametrize(
    ("requested", "elapsed", "reserve", "expected"),
    [
        (30.0, 0.0, 3.0, 30.0),
        (30.0, 20.0, 3.0, 10.0),
        (0.0, 0.0, 3.0, 0.0),
        (30.0, 40.0, 3.0, 0.0),
        (30.0, 0.0, 8.0, 25.0),
    ],
)
def test_the_wait_fits_the_guest_budget(
    monkeypatch: pytest.MonkeyPatch, requested: float, elapsed: float, reserve: float, expected: float
) -> None:
    monkeypatch.setattr(time, "monotonic", lambda: 100.0 + elapsed)
    assert manager_module._remaining(requested, 100.0, reserve) == expected


def test_a_subagent_asking_for_the_task_vm_gets_its_own(world: World) -> None:
    child = session(CHILD, parent_id=SESSION)
    result = world.run(lambda vms: request(vms, {"scope": "task"}, child))
    assert result["scope"] == "session"
    assert [row["scope"] for row in rows(world)] == ["session"]


def test_deleting_a_vm_that_no_longer_runs_drops_its_open_runs(world: World) -> None:
    world.scripts["serve"] = (b"line\n", "running", None)

    async def body(vms: WorkVms) -> int:
        await vms.call(None, session(), "workvm_exec", {"command": "serve", "sync": "none"})
        [vm] = await records.live(vms.deps.db)
        await records.update(vms.deps.db, vm.id, state="stopping")
        await vms.delete(vm, reason="failed")
        return len(vms.runs)

    assert world.run(body) == 0
    assert [row["state"] for row in rows(world)] == ["deleted"]


# -- warm pool (spec 11 section 7) -----------------------------------------------------------------------------


def pooled(world: World, size: int = 1, slots: int = 2) -> None:
    world.settings = manager_module.Settings(**{**world.settings.__dict__, "pool_size": size, "slots": slots})


def pool_rows(world: World) -> list[dict[str, Any]]:
    with psycopg.connect(world.dsn, autocommit=True, row_factory=dict_row) as conn:
        return list(conn.execute("SELECT lima_name, slot, state, image FROM runner.workvm_pool ORDER BY slot"))


def test_the_pool_fills_and_a_request_takes_a_ready_entry_without_cloning(world: World) -> None:
    pooled(world, size=2, slots=3)

    async def body(vms: WorkVms) -> tuple[int, int, dict[str, Any], bool]:
        filled = await vms.pool_pass()
        wanted_more = vms.pool_wanted.is_set()
        filled += await vms.pool_pass()
        assert wanted_more
        assert not vms.pool_wanted.is_set()
        again = await vms.pool_pass()
        clones = sum(1 for operation, _ in world.calls if operation == "clone")
        result = await request(vms)
        assert sum(1 for operation, _ in world.calls if operation == "clone") == clones
        return filled, again, result, vms.pool_wanted.is_set()

    filled, again, result, wanted = world.run(body)
    assert (filled, again, wanted) == (2, 0, True)
    assert (result["created"], result["ready_ms"], result["size"]) == (True, 0, "standard")
    [row] = rows(world)
    assert row["state"] == "running"
    assert [entry["slot"] for entry in pool_rows(world)] == [1 - row["slot"]]
    assert all(sets[0] == ".cpus=4" for sets in world.sets.values())
    assert sum("pool_ready" in line for line in world.logs) == 2
    assert any("pooled=1" in line for line in world.logs)
    [created] = [body for _, kind, body in world.events if kind == "workvm.state"]
    assert created["workvm_id"] == result["workvm_id"]


def test_without_a_pool_nothing_is_kept_warm(world: World) -> None:
    assert world.run(lambda vms: vms.pool_pass()) == 0
    assert pool_rows(world) == []
    world.run(request)
    assert any("pooled=0" in line for line in world.logs)


def test_a_larger_size_is_cloned_and_the_pool_stays(world: World) -> None:
    pooled(world)

    async def body(vms: WorkVms) -> dict[str, Any]:
        await vms.pool_pass()
        return await request(vms, {"size": "large"})

    result = world.run(body)
    assert (result["size"], result["cpus"]) == ("large", 8)
    assert [entry["slot"] for entry in pool_rows(world)] == [0]
    assert [row["slot"] for row in rows(world)] == [1]


def test_an_unhealthy_entry_is_removed_and_the_request_goes_cold(world: World) -> None:
    pooled(world)

    def heal(w: World) -> None:
        w.helper_fail.clear()

    async def body(vms: WorkVms) -> dict[str, Any]:
        await vms.pool_pass()
        world.helper_fail["list"] = ShellResult(255, b"", False)
        world.on_sleep = heal
        return await request(vms)

    result = world.run(body)
    assert result["created"] is True
    assert sorted(row["state"] for row in rows(world)) == ["deleted", "running"]
    assert any("pool_unhealthy" in line for line in world.logs)
    assert pool_rows(world) == []


def test_a_taken_pool_entry_is_a_retryable_failure(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    pooled(world)

    async def taken(*_: Any) -> Any:
        raise RunnerError("workvm_exists")

    monkeypatch.setattr(records, "pool_assign", taken)
    with pytest.raises(OperationFailed) as caught:
        world.run(request)
    assert (caught.value.code, caught.value.retryable) == ("timeout", True)


def test_the_pool_keeps_the_ram_of_one_request_and_never_takes_from_a_waiting_one(world: World) -> None:
    pooled(world, size=2)

    async def body(vms: WorkVms) -> tuple[int, int, int]:
        world.free_mib = 2 * 8192 - 1
        low = await vms.pool_pass()
        world.free_mib = 1_000_000
        vms.queue.append("waiting")
        queued = await vms.pool_pass()
        vms.queue.clear()
        return low, queued, await vms.pool_pass() + await vms.pool_pass()

    assert world.run(body) == (0, 0, 2)
    assert len(pool_rows(world)) == 2


def test_a_pool_entry_that_does_not_start_is_dropped(world: World) -> None:
    pooled(world)
    world.fail.update({"start", "delete"})
    assert world.run(lambda vms: vms.pool_pass()) == 0
    assert pool_rows(world) == []
    assert any("pool_failed" in line for line in world.logs)
    assert any("pool_delete_failed" in line for line in world.logs)


def test_a_failed_pool_start_logs_the_last_lima_error(world: World) -> None:
    pooled(world)
    world.fail.add("start")
    world.lima_stderr = (
        'time="2026-11-24T14:18:00+01:00" level=info msg="Starting the instance"\n'
        'time="2026-11-24T14:18:01+01:00" level=fatal msg="open /private/var/agent-workvm/wvm00/lima/_config/'
        'networks.yaml: permission denied"\n'
    )
    assert world.run(lambda vms: vms.pool_pass()) == 0
    assert (
        'runner.workvm pool_failed slot=0 code=lima_failed lima="open /private/var/agent-workvm/wvm00/lima/_config/'
        'networks.yaml: permission denied"'
    ) in world.logs


@pytest.mark.parametrize(
    ("detail", "reason"),
    [
        ("", ""),
        ("plain failure\n\n", "plain failure"),
        ('level=error msg="first"\nlevel=warning msg="later"\n', "first"),
        ('time="t" level=error msg="real"\ntime="t" level=info msg="saw level=error"\n', "real"),
        ('level=fatal msg="quoted \\"name\\" and \\\\ path"', 'quoted "name" and \\ path'),
        ("level=fatal without message\x1b[0m", "level=fatal without message [0m"),
        ("x" * 300, "x" * (manager_module.LIMA_REASON_LIMIT - 3) + "..."),
    ],
)
def test_lima_reason(detail: str, reason: str) -> None:
    assert manager_module.lima_reason(detail) == reason


def test_failure_names_lima_only_for_lima_errors() -> None:
    assert manager_module.failure(LimaError("lima_failed", "start", detail="boom\n")) == 'code=lima_failed lima="boom"'
    assert manager_module.failure(LimaError("lima_failed", "start")) == "code=lima_failed"
    assert manager_module.failure(WorkVmError("workvm_unavailable", "x")) == "code=workvm_unavailable"


def test_entries_of_another_image_or_without_an_offer_go(world: World) -> None:
    pooled(world)

    async def body(vms: WorkVms) -> tuple[list[str], list[str]]:
        await vms.pool_pass()
        first = [entry.lima_name for entry in await records.pool(vms.deps.db)]
        await records.set_image(vms.deps.db, "wp-t-wimg-2", "ready", START + timedelta(days=1))
        await vms.pool_pass()
        second = [entry.image for entry in await records.pool(vms.deps.db)]
        world.boundary = False
        await vms.pool_pass()
        return first, second

    first, second = world.run(body)
    assert len(first) == 1
    assert first[0] not in world.instances
    assert second == ["wp-t-wimg-2"]
    assert pool_rows(world) == []


def test_a_waiting_request_takes_the_slot_of_a_pool_entry(world: World) -> None:
    pooled(world, slots=1)

    async def body(vms: WorkVms) -> dict[str, Any]:
        await vms.pool_pass()
        return await request(vms, {"size": "large"})

    result = world.run(body)
    assert result["size"] == "large"
    assert pool_rows(world) == []
    assert [row["slot"] for row in rows(world)] == [0]
    assert world.sleeps == []


def test_guard_and_reconcile_drop_the_pool(world: World) -> None:
    pooled(world)

    async def body(vms: WorkVms) -> tuple[int, int]:
        await vms.pool_pass()
        world.boundary = False
        await vms.guard_pass()
        after_guard = len(await records.pool(vms.deps.db))
        world.boundary = True
        await vms.pool_pass()
        await WorkVms(vms.deps).reconcile()
        return after_guard, len(await records.pool(vms.deps.db))

    assert world.run(body) == (0, 0)
    assert world.instances == {}


def test_an_empty_pool_falls_back_to_a_cold_start(world: World) -> None:
    pooled(world)
    result = world.run(request)
    assert result["created"] is True
    assert any("pooled=0" in line for line in world.logs)


def test_a_waiting_standard_request_takes_a_ready_entry_at_the_head(world: World) -> None:
    pooled(world)

    async def body(vms: WorkVms) -> dict[str, Any]:
        await vms.pool_pass()
        vms.queue.append("earlier")

        def leave(_w: World) -> None:
            if "earlier" in vms.queue:
                vms.queue.remove("earlier")

        world.on_sleep = leave
        return await request(vms)

    result = world.run(body)
    assert result["created"] is True
    assert world.sleeps == [2.0]
    assert pool_rows(world) == []
    assert any("pooled=1" in line for line in world.logs)


def test_a_waiting_request_never_drops_an_entry_that_is_still_starting(world: World) -> None:
    pooled(world, slots=1)
    world.settings = manager_module.Settings(**{**world.settings.__dict__, "queue_seconds": 2})

    async def body(vms: WorkVms) -> None:
        entry = records.Pooled("wp-t-wvm-0000000f", 0, "wp-t-wimg-1", 8192, "starting", START)
        await records.pool_add(vms.deps.db, entry)
        world.instances[entry.lima_name] = "Running"
        await request(vms, {"size": "large"})

    with pytest.raises(OperationFailed) as caught:
        world.run(body)
    assert caught.value.code == "timeout"
    assert [entry["state"] for entry in pool_rows(world)] == ["starting"]
    assert "wp-t-wvm-0000000f" in world.instances


def test_an_entry_removed_while_it_started_takes_its_vm_along(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    pooled(world)

    async def gone(*_: Any) -> bool:
        return False

    monkeypatch.setattr(records, "pool_ready", gone)
    assert world.run(lambda vms: vms.pool_pass()) == 0
    assert world.instances == {}
    assert not any("pool_ready" in line for line in world.logs)


def test_dropping_an_entry_that_was_assigned_keeps_its_vm(world: World) -> None:
    entry = records.Pooled("wp-t-wvm-00000010", 0, "wp-t-wimg-1", 8192, "ready", START)
    world.instances[entry.lima_name] = "Running"
    world.run(lambda vms: vms._drop(entry))
    assert entry.lima_name in world.instances


def test_a_request_the_pool_cannot_make_fit_leaves_the_pool(world: World) -> None:
    pooled(world, slots=2)
    world.settings = manager_module.Settings(**{**world.settings.__dict__, "queue_seconds": 2})

    async def body(vms: WorkVms) -> None:
        await vms.pool_pass()
        world.free_mib = 1024  # free RAM plus the pool's 8 GiB is still below a large VM's 16 GiB
        await request(vms, {"size": "large"})

    with pytest.raises(OperationFailed) as caught:
        world.run(body)
    assert caught.value.code == "timeout"
    assert [entry["state"] for entry in pool_rows(world)] == ["ready"]


def test_the_disk_quota_holds_requests_and_the_pool(world: World) -> None:
    world.settings = manager_module.Settings(
        **{**world.settings.__dict__, "slots": 3, "pool_size": 1, "disk_quota_gib": 120, "queue_seconds": 2}
    )

    async def body(vms: WorkVms) -> tuple[int, dict[str, Any]]:
        pool = await vms.pool_pass()  # two standard disks (100 GiB) fit the quota: one entry starts
        first = await request(vms, {"size": "large"})  # 100 GiB: the pool entry gives its disk back
        return pool, first

    pool, first = world.run(body)
    assert (pool, first["size"]) == (1, "large")
    assert pool_rows(world) == []

    async def more(vms: WorkVms) -> int:
        return await vms.pool_pass()  # 100 + 2 x 50 > 120: no entry

    assert world.run(more) == 0
    with pytest.raises(OperationFailed) as caught:
        world.run(lambda vms: request(vms, {}, session(CHILD, task_id=TASK_B)))  # 100 + 50 > 120: waits, times out
    assert caught.value.code == "timeout"


def test_without_a_disk_quota_the_disk_is_not_checked(world: World) -> None:
    world.settings = manager_module.Settings(**{**world.settings.__dict__, "disk_quota_gib": 0})
    assert world.run(request)["created"] is True


# -- a restart without the volume of the slot homes (HOT-25) ----------------------------------------------------------


def test_a_slot_lima_cannot_list_stops_neither_the_reconciliation_nor_the_sweep(world: World) -> None:
    async def body(vms: WorkVms) -> int:
        world.instances["wp-t-wvm-orphan1"] = "Stopped"
        world.slot_of["wp-t-wvm-orphan1"] = 1
        world.fail_list = {0}
        await WorkVms(vms.deps).reconcile()
        world.instances["wp-t-wvm-orphan2"] = "Stopped"
        world.slot_of["wp-t-wvm-orphan2"] = 1
        return await vms.sweep()

    assert world.run(body) == 1
    assert world.instances == {}
    assert sum(line.startswith("runner.workvm list_failed slot=0 ") for line in world.logs) == 2
    assert sum(line == "runner.workvm orphan_deleted slot=1" for line in world.logs) == 2


def test_an_orphan_lima_cannot_delete_does_not_stop_the_reconciliation(world: World) -> None:
    async def body(vms: WorkVms) -> None:
        world.instances["wp-t-wvm-orphan"] = "Stopped"
        world.slot_of["wp-t-wvm-orphan"] = 0
        world.fail = {"delete"}
        await WorkVms(vms.deps).reconcile()

    world.run(body)
    assert "wp-t-wvm-orphan" in world.instances
    assert any(line.startswith("runner.workvm orphan_delete_failed slot=0 ") for line in world.logs)


def test_a_work_vm_whose_slot_home_is_gone_counts_as_deleted(world: World) -> None:
    """The volume hung elsewhere after the restart: the real slot command finds no home and no instance, so the
    reconciliation deletes the VMs left over without an error and the sweep has nothing to do again."""
    from agent_runner.vm.lima import Lima  # noqa: PLC0415
    from agent_runner.workvm.shell import SlotCommand  # noqa: PLC0415

    async def body(vms: WorkVms) -> int:
        await request(vms)
        command = SlotCommand(("wpwvm00", "wpwvm01"), home_root=str(world.tmp / "gone"))
        vms.deps.lima_for = lambda slot: Lima(command.sync_runner(slot), prefix="wp-t-")
        await WorkVms(vms.deps).reconcile()
        return await vms.sweep()

    assert world.run(body) == 0
    assert [row["state"] for row in rows(world)] == ["deleted"]
    assert not any("delete_failed" in line for line in world.logs)


def test_no_work_vm_is_offered_while_the_volume_is_missing(world: World) -> None:
    async def missing() -> bool:
        return False

    async def body(vms: WorkVms) -> tuple[str | None, bool, str | None, bool]:
        assert await vms.check_volume() is True  # the default: in place
        vms.deps.volume_ok = missing
        gone = await vms.available(), await vms.check_volume()
        vms.deps.settings = replace(vms.deps.settings, slots=0)
        return (*gone, await vms.available(), await vms.check_volume())

    assert world.run(body) == (None, False, None, True)
    assert world.logs.count("runner.workvm volume_missing") == 1


# -- login VMs (KOR-73, ADR-100 section 3) ------------------------------------------------------------------------


def test_login_vms_need_no_session_count_for_no_limit_are_not_billed_and_go_with_the_login(world: World) -> None:
    from agent_runner.workvm import billing_vms  # noqa: PLC0415

    world.settings = manager_module.Settings(
        **{**world.settings.__dict__, "slots": 3, "max_per_org": 1, "max_per_owner": 1}
    )

    async def body(vms: WorkVms) -> tuple[Any, Any, list[Any], Any]:
        first = await vms.obtain_login("0193000a-0000-7000-8000-0000000000a1")
        second = await vms.obtain_login("0193000a-0000-7000-8000-0000000000a2")
        task_vm = await request(vms)  # the organization and owner limits of 1 still leave room for it
        async with vms.deps.db.connection() as conn:
            billed = await billing_vms.VMS(None, conn)
        await vms.delete(first, reason="login_ended")
        return first, second, billed, task_vm

    first, second, billed, task_vm = world.run(body)
    assert (first.scope, first.session_id, first.task_id, first.owner_id) == (
        "login",
        "0193000a-0000-7000-8000-0000000000a1",
        manager_module.NO_ID,
        None,
    )
    assert second.slot != first.slot
    assert [vm.vm_id for vm in billed] == [task_vm["workvm_id"]]
    states = {row["id"]: row["state"] for row in rows(world)}
    assert (states[first.id], states[second.id]) == ("deleted", "running")


def test_no_login_vm_without_a_ready_image(world: World) -> None:
    world.image = None
    with pytest.raises(OperationFailed) as caught:
        world.run(lambda vms: vms.obtain_login("0193000a-0000-7000-8000-0000000000a1"))
    assert caught.value.code == "workvm_unavailable"
