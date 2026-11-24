"""U-RUN-prompts: a prompt of a user's agent (ADR-082, HOT-14): verified with the agent's own key and kept with the
agent's user as its acting author."""

from __future__ import annotations

from typing import Any

from ..reconcile.rig import Rig, pid, standard_world
from ..sessions.kit import ORG
from .kit import add_prompt, pull, run, setup


def test_a_prompt_of_the_agent_is_kept_with_its_user_as_the_acting_author(rig: Rig) -> None:
    world = standard_world()
    world.orgs[ORG].log.agent_add("alice", "bot", "alice", 1)

    async def body(env: Any) -> None:
        pk = await setup(env)
        await pk.add_session()
        event_id = add_prompt(env, 1, "p", author="bot")
        assert await pull(env) == 1
        rows = await env.rows("SELECT author_id::text FROM runner.prompt_event WHERE event_id = %s", event_id)
        assert rows == [(pid("alice"),)]
        assert await pk.audit_codes() == []

    run(rig, body, world)
