"""U-RUN-billing (KOR-46, ADR-083 section 2): `runtime` items name the person whose instruction the runtime follows
from - a session VM's share the session's acting user at the tick (a child: the acting user of its
`subagent.request`), another VM the person its source names - only towards a hub with the section `prompt_queue`."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_guest.schema import SchemaSet
from agent_runner import registry
from agent_runner.billing import meter

from ..prompts.kit import run, setup
from ..reconcile.rig import Rig, pid
from ..sessions.kit import TASK, WG
from .test_meter import ACTIVE, T0, Recorder

SCHEMAS = SchemaSet(Path(__file__).resolve().parents[4] / "contracts")
ALICE, BOB = pid("alice"), pid("bob")
QUEUE: dict[str, Any] = {"prompt_queue": {"via_agent": [], "withdrawn": []}}


async def acting(env: Any, sid: str, actor: str | None) -> None:
    async with env.pool.connection() as conn:
        await conn.execute("UPDATE runner.session SET turn_actor = %s WHERE id = %s", (actor, sid))


async def child_of(env: Any, child: str, actor: str) -> None:
    async with env.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO runner.operation (id, session_id, task_id, wg_id, type, state, params, child_session_id,"
            " actor_id) VALUES (gen_random_uuid(), %s, %s, %s, 'subagent.request', 'succeeded', '{}', %s, %s)",
            (child, TASK, WG, child, actor),
        )


def test_session_vm_shares_name_the_acting_user_at_the_tick(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        main = await pk.add_session(1, state="working")
        child = await pk.add_session(2, state="working", depth=1)
        unknown = await pk.add_session(3, state="idle")
        await acting(env, main, ALICE)
        await acting(env, child, ALICE)  # never used for a child
        await child_of(env, child, BOB)
        recorder = Recorder()
        async with env.pool.connection() as conn, conn.transaction():
            await meter.measure(env.context, conn, recorder, T0, triggered=True)
            await meter.measure(env.context, conn, recorder, T0 + timedelta(seconds=3), triggered=True)
        assert {item["session_id"]: item["triggered_by"] for item in recorder.items} == {
            main: ALICE,
            child: BOB,
            unknown: None,
        }
        assert all(SCHEMAS.errors("outbox/objects.json#/$defs/body_runtime", item) == [] for item in recorder.items)
        # the stamp is read at every tick: the next turn's person bears the next interval
        await acting(env, main, BOB)
        async with env.pool.connection() as conn, conn.transaction():
            await meter.measure(env.context, conn, recorder, T0 + timedelta(seconds=6), triggered=True)
            await meter.measure(env.context, conn, recorder, T0 + timedelta(seconds=9))
        later = [item for item in recorder.items[3:] if item["session_id"] == main]
        assert [item.get("triggered_by", "absent") for item in later] == [BOB, "absent"]

    run(rig, body)


def test_other_vms_name_the_person_of_their_source(rig: Rig, monkeypatch: Any) -> None:
    sessions = ("01930000-0000-7000-8000-000000000001", "01930000-0000-7000-8000-000000000002")

    async def work_vms(_context: Any, _conn: Any) -> list[meter.MeteredVm]:
        return [
            meter.MeteredVm("wvm-1", "work", "large", sessions[:1], (BOB,)),
            meter.MeteredVm("wvm-2", "work", "large", sessions[1:]),
        ]

    monkeypatch.setattr(registry, "discover", lambda module, attribute: [("workvm", work_vms)])

    async def body(env: Any) -> None:
        recorder = Recorder()
        async with env.pool.connection() as conn, conn.transaction():
            await meter.measure(env.context, conn, recorder, T0, triggered=True)
            await meter.measure(env.context, conn, recorder, T0 + timedelta(seconds=1), triggered=True)
        assert [(item["vm_id"], item["triggered_by"]) for item in recorder.items] == [("wvm-1", BOB), ("wvm-2", None)]

    run(rig, body)


def test_the_task_sends_triggered_by_only_to_a_hub_with_the_queue(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(1, state="working")
        await acting(env, sid, ALICE)
        session_env = pk.kit.session_env
        service = SimpleNamespace(env=session_env)
        times = iter([1_700_000_000.0, 1_700_000_010.0, 1_700_000_020.0])
        session_env.wall = lambda: next(times)
        session_env.desired = {**ACTIVE, **QUEUE}
        assert await meter.tick(env.context, service) == 0
        assert await meter.tick(env.context, service) == 1
        session_env.desired = ACTIVE
        assert await meter.tick(env.context, service) == 1
        items = [body for kind, body in await pk.kit.outbox() if kind == "runtime"]
        assert [item.get("triggered_by", "absent") for item in items] == [ALICE, "absent"]

    run(rig, body)
