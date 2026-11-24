"""U-RUN-model (KOR-46, ADR-083 section 2): towards a hub with the section `prompt_queue` a `usage` item names the
person whose instruction the request follows from (the session's acting user when the record is handled); an older
hub gets no `triggered_by`."""

from __future__ import annotations

from typing import Any

from ..model.kit import FLASH, Env
from ..reconcile.rig import pid
from ..sessions.kit import session_id
from .test_usage import TOKENS, consume, record, run, started

QUEUE: dict[str, Any] = {"prompt_queue": {"via_agent": [], "withdrawn": []}}
ALICE = pid("alice")


def test_usage_names_the_acting_user_of_the_turn(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, reader = await started(env, "platform")
        found.session_env.desired = QUEUE
        sid = session_id()
        found.guest.emit(sid, "usage", record(FLASH, **TOKENS))
        await consume(found, reader)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET turn_actor = %s WHERE id = %s", (ALICE, sid))
        found.guest.emit(sid, "usage", record(FLASH, **TOKENS))
        await consume(found, reader)
        found.session_env.desired = None
        found.guest.emit(sid, "usage", record(FLASH, **TOKENS))
        await consume(found, reader)
        items = await found.of_kind("usage")
        assert [item.get("triggered_by", "absent") for item in items] == [None, ALICE, "absent"]
        await env.sync.pusher.drain()  # the hub checks the items against contracts/outbox/usage.json

    run(store_dsn, body)
