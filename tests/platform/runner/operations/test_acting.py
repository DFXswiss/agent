"""U-RUN-operations: the acting user of a turn (HOT-14, ADR-068; `credentials/acting.py`,
`credentials/session_state.py`), through the real journal handlers in the guest's order: `session.state
prompt_accepted` names the verified author of the prompt, `operation.request` is stamped with the turn's acting user,
`idle` closes the turn; prompts of two people in one turn, an unknown prompt and a prompt of another session leave
the turn without an acting user; a child session inherits the acting user of its `subagent.request`."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from psycopg.types.json import Jsonb

from agent_runner.credentials import acting
from agent_runner.credentials import session_state as acting_state
from agent_runner.crypto.ids import uuid7
from agent_runner.guestlink.client import JournalRecord
from agent_runner.operations.journal import HANDLERS as OPERATION_HANDLERS
from agent_runner.prompts import queue
from agent_runner.registry import ROOT_PACKAGE
from agent_runner.sessions.consume import VmJournal
from agent_runner.sessions.extensions import state_observers
from agent_runner.sessions.journal import HANDLERS as SESSION_HANDLERS

from ..credentials.kit import ALICE, BOB
from ..reconcile.rig import Rig
from ..sessions.kit import ORG, TASK, WG, session_id
from .kit import REQUESTED_AT, OKit, run, setup


def test_the_observer_is_registered_before_the_delivery() -> None:
    names = [observer.name for observer in state_observers(ROOT_PACKAGE)]
    # the acting user is read while the prompt's delivery row is still open; the delivery's observer closes it
    assert names.index("credentials.acting") < names.index("prompts.delivery")
    assert acting_state.OBSERVERS[0].handle is acting_state.observe


class Turns:
    """A main session of the operations kit whose guest journal runs through the session and operation handlers and
    the acting-user observer."""

    def __init__(self, kit: OKit, sid: str) -> None:
        self.kit = kit
        self.sid = sid
        self.scope_seq = 100

    async def prompt(self, author: str, *, session: str | None = None) -> str:
        """A verified prompt of `author` stored and delivered to `session` (default: this one); its event ID."""
        event_id = uuid7()
        self.scope_seq += 1
        header = {"id": event_id, "org": ORG, "scope_id": WG, "task": TASK}
        async with self.kit.env.pool.connection() as conn:
            assert await queue.keep(
                conn, header=header, scope_seq=self.scope_seq, author_sig="sig", sealed={"x": 1}, author_id=author
            )
            await conn.execute(
                "INSERT INTO runner.prompt_delivery (event_id, session_id, task_id, scope_seq, state)"
                " VALUES (%s, %s, %s, %s, 'typed')",
                (event_id, session or self.sid, TASK, self.scope_seq),
            )
            cur = await conn.execute("SELECT author_id::text FROM runner.prompt_event WHERE event_id = %s", (event_id,))
            assert await cur.fetchone() == (author,)
        return event_id

    async def journal(self, kind: str, body: dict[str, Any], sid: str | None = None) -> None:
        kit = self.kit
        kit.seq += 1
        row = await kit.vm()
        reader = VmJournal(
            kit.kit.session_env,
            row,
            handlers={**SESSION_HANDLERS, **OPERATION_HANDLERS},
            observers=acting_state.OBSERVERS,
        )
        record = JournalRecord(seq=kit.seq, ts=REQUESTED_AT, session_id=sid or self.sid, kind=kind, body=body)
        await reader.handle([record])

    async def accepted(self, prompt_id: str) -> None:
        await self.journal("session.state", {"state": "prompt_accepted", "prompt_id": prompt_id, "at": REQUESTED_AT})

    async def idle(self) -> None:
        await self.journal("session.state", {"state": "idle", "at": REQUESTED_AT})

    async def request(self, op_type: str = "github.comment", sid: str | None = None) -> str | None:
        """Journal one operation request; the acting user it was stamped with."""
        op_id = uuid7()
        body = {
            "pending_id": uuid7(),
            "operation_id": op_id,
            "type": op_type,
            "params": {"repo": "octo-org/hello", "number": 1, "body": "x"},
            "requested_at": REQUESTED_AT,
            "local_check": "passed",
        }
        await self.journal("operation.request", body, sid)
        return (await self.kit.op(op_id)).actor_id

    async def turn(self) -> tuple[str | None, bool]:
        async with self.kit.env.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT turn_actor::text, turn_open FROM runner.session WHERE id = %s", (self.sid,)
            )
            row = await cur.fetchone()
        assert row is not None
        return row[0], row[1]


def test_each_turn_acts_for_the_author_of_its_prompt(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit = await setup(env)
        turns = Turns(kit, await kit.session(actor=None))
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET turn_open = false WHERE id = %s", (turns.sid,))
        assert await turns.request() is None  # no prompt accepted yet: nobody
        await turns.accepted(await turns.prompt(ALICE))
        assert await turns.turn() == (ALICE, True)
        assert await turns.request() == ALICE
        await turns.idle()
        assert await turns.turn() == (ALICE, False)
        assert await turns.request() == ALICE  # still journaled for the turn that ended
        # bob writes in alice's task: the next turn acts for bob
        await turns.accepted(await turns.prompt(BOB))
        assert await turns.request() == BOB
        # the same author once more in the open turn keeps it
        await turns.accepted(await turns.prompt(BOB))
        assert await turns.turn() == (BOB, True)
        # alice's prompt joins bob's open turn: nobody acts any more until the next turn
        await turns.accepted(await turns.prompt(ALICE))
        assert await turns.turn() == (None, True)
        assert await turns.request() is None
        await turns.accepted(await turns.prompt(BOB))
        assert await turns.turn() == (None, True)
        await turns.idle()
        await turns.accepted(await turns.prompt(ALICE))
        assert await turns.request() == ALICE

    run(rig, body)


def test_a_prompt_without_known_author_leaves_the_turn_without_acting_user(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit = await setup(env)
        turns = Turns(kit, await kit.session(actor=None))
        other = await kit.session(2, actor=None)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET turn_open = false")
        # a prompt delivered to another session
        await turns.accepted(await turns.prompt(ALICE, session=other))
        assert await turns.turn() == (None, True)
        await turns.idle()
        # an ID that names no stored prompt, and one that is no UUID
        await turns.accepted(uuid7())
        assert await turns.turn() == (None, True)
        await turns.idle()
        await turns.accepted("not-a-uuid")
        assert await turns.turn() == (None, True)
        await turns.idle()
        # a replayed (or forged) acceptance of a prompt that was accepted before never brings back its author
        replayed = await turns.prompt(ALICE)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.prompt_delivery SET state = 'accepted' WHERE event_id = %s", (replayed,))
        await turns.accepted(replayed)
        assert await turns.turn() == (None, True)
        assert await turns.request() is None
        # other states change nothing
        await turns.journal("session.state", {"state": "working", "at": REQUESTED_AT})
        assert await turns.turn() == (None, True)

    run(rig, body)


def test_a_child_session_acts_for_the_person_who_instructed_its_parent(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit = await setup(env)
        parent = await kit.session(actor=BOB)
        child = await kit.session(3, depth=1, actor=None)
        turns = Turns(kit, parent)
        assert await turns.request(sid=child) is None  # no subagent request names it yet
        async with env.pool.connection() as conn:
            requested = await turns.request("subagent.request")
            assert requested == BOB
            await conn.execute(
                "UPDATE runner.operation SET child_session_id = %s WHERE type = 'subagent.request'", (child,)
            )
        assert await turns.request(sid=child) == BOB
        # the child's own prompt (its purpose, the operation ID) never changes it
        await turns.journal(
            "session.state", {"state": "prompt_accepted", "prompt_id": uuid7(), "at": REQUESTED_AT}, child
        )
        assert await turns.request(sid=child) == BOB

    run(rig, body)


def test_the_stamp_of_an_unknown_session_is_nobody(rig: Rig) -> None:
    async def body(env: Any) -> None:
        async with env.pool.connection() as conn:
            assert await acting.stamp(conn, SimpleNamespace(id=session_id(42), depth=0)) is None

    run(rig, body)


def test_the_migration_takes_the_verified_author_of_prompts_stored_before(rig: Rig) -> None:
    import agent_runner.store  # noqa: PLC0415

    migration = Path(agent_runner.store.__file__).parent / "migrations" / "0016_acting_user.sql"
    backfill = next(part for part in migration.read_text().split(";") if "UPDATE runner.prompt_event" in part)

    async def body(env: Any) -> None:
        rows = [(uuid7(), {"header": {"author": BOB}}), (uuid7(), {"header": {"author": "bob"}}), (uuid7(), {})]
        async with env.pool.connection() as conn:
            for n, (event_id, sealed) in enumerate(rows):
                await conn.execute(
                    "INSERT INTO runner.prompt_event (event_id, org_id, wg_id, task_id, scope_seq, author_sig, sealed)"
                    " VALUES (%s, %s, %s, %s, %s, 'sig', %s)",
                    (event_id, ORG, WG, TASK, 900 + n, Jsonb(sealed)),
                )
            await conn.execute(backfill)
            cur = await conn.execute(
                "SELECT author_id::text FROM runner.prompt_event WHERE event_id = ANY(%s) ORDER BY scope_seq",
                ([event_id for event_id, _sealed in rows],),
            )
            assert [row[0] for row in await cur.fetchall()] == [BOB, None, None]

    run(rig, body)
