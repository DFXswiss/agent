"""U-RUN-prompts: a prompt of a support person (ADR-096): verified with the key of its root certification against a
`write` release of the workgroup, kept with the person as its acting author."""

from __future__ import annotations

from typing import Any

from agent_runner.crypto.log import vectors as v

from ..reconcile.rig import Rig, pid, standard_world
from ..sessions.kit import ORG
from .kit import add_prompt, pull, run, setup


def test_a_prompt_of_a_support_person_is_kept_with_the_person_as_the_acting_author(rig: Rig) -> None:
    world = standard_world()
    log = world.orgs[ORG].log
    log.add("support_grant", "alice", wg=world.orgs[ORG].default_wg, access="write")
    world.platform.add("user_certify", user=pid("sam"), pub=v.pub("sam"))
    world.platform.add("role_grant", subject=pid("sam"), role="support")

    async def body(env: Any) -> None:
        pk = await setup(env)
        await pk.add_session()
        event_id = add_prompt(env, 1, "p", author="sam", lh=len(log.entries) - 1)
        assert await pull(env) == 1
        rows = await env.rows("SELECT author_id::text FROM runner.prompt_event WHERE event_id = %s", event_id)
        assert rows == [(pid("sam"),)]
        assert await pk.audit_codes() == []

    run(rig, body, world)
