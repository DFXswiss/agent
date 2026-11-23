"""U-RUN-prompts retention and routing (KOR-03, AUD-01 B-19): the hub may route a final prompt once more; after the
retention removed its stored event the routing answers the final state - no inbox refresh, no `prompt_unverified`
audit, no second delivery."""

from __future__ import annotations

from typing import Any

from agent_runner.prompts import retention

from ..reconcile.rig import Rig
from .kit import add_prompt, pull, run, setup
from .test_delivery import accept


def test_a_pruned_final_prompt_routed_again_stays_final(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        first, second = add_prompt(env, 1, "p1"), add_prompt(env, 2, "p2")
        await pull(env)
        for seq, event_id in ((1, first), (2, second)):
            await pk.route(sid, seq, event_id)
            await pk.deliverer.advance(sid)
            await accept(pk, sid, event_id)
        async with env.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE runner.prompt_delivery SET updated_at = now() - interval '2 days';"
                " UPDATE runner.prompt_event SET received_at = now() - interval '2 days'"
            )
            # the earliest accepted prompt of the task stays (restart hint)
            assert await retention.prune(conn) == 1
        audits = len(await env.audits())
        sent = list(pk.guest.sent())
        refreshed: list[bool] = []

        async def refresh() -> None:
            refreshed.append(True)

        pk.deliverer.env.refresh_profiles = refresh
        await pk.route(sid, 2, second)
        await pk.deliverer.advance(sid)
        assert await pk.rows(sid) == {first: "accepted", second: "accepted"}
        assert pk.guest.sent() == sent
        assert len(await env.audits()) == audits
        assert refreshed == []

    run(rig, body)
