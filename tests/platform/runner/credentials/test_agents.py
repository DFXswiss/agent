"""U-RUN-credentials: credential records and `CK` wraps of the owner's agents (ADR-082 section 9.5): a record of the
owner's active agent verifies; a record or `CK` wrap of a former agent of the owner (removed or superseded) still
opens, but the device never seals a new record under an epoch only a former agent wrapped; anybody else's stays
refused."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from agent_runner.credentials import cache
from agent_runner.credentials.cache import RecordRefused
from agent_runner.crypto.log import vectors as v

from ..reconcile.rig import World, wrap_for
from .kit import ALICE, Env, github_body, kit_env, make_record, pid, world_with_users


def agent_world(step: Callable[[v.LogBuilder], object] = lambda log: None) -> World:
    """`world_with_users` with alice's agent `bot` (generation 1) in org `a`, then `step`."""
    world = world_with_users()
    log = world.orgs[pid("a/org")].log
    log.agent_add("alice", "bot", "alice", 1)
    step(log)
    return world


def test_a_record_of_the_owners_active_agent_verifies_with_the_agents_key(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        env.hub.records[(ALICE, "github")] = make_record(github_body(), author="bot")
        await env.add_ck()
        async with cache.opened(env.context, ALICE, "github") as opened:
            assert opened["login"] == "octo-user"

    async def main() -> None:
        async with kit_env(store_dsn, agent_world()) as env:
            await body(env)

    asyncio.run(main())


@pytest.mark.parametrize(
    "step",
    [
        lambda log: log.add("remove", "alice", subject=pid("bot")),
        lambda log: log.agent_add("alice", "bot2", "alice", 2),
    ],
    ids=["removed", "replaced"],
)
def test_a_record_of_a_former_agent_of_the_owner_still_opens(
    store_dsn: str, step: Callable[[v.LogBuilder], object]
) -> None:
    """An agent that wrote a record before it was revoked: the record opens until the owed rotation re-signs it."""

    async def main() -> None:
        async with kit_env(store_dsn, agent_world(step)) as env:
            env.hub.records[(ALICE, "github")] = make_record(github_body(), author="bot")
            await env.add_ck()
            async with cache.opened(env.context, ALICE, "github") as opened:
                assert opened["login"] == "octo-user"
            # an author the verified logs do not know stays refused
            env.hub.records[(ALICE, "github")] = make_record(github_body(), author="bot5", version=2)
            with pytest.raises(RecordRefused) as refused:
                await cache.fetch(env.context, ALICE, "github")
            assert refused.value.code == "wrap_unauthorized"

    asyncio.run(main())


def test_a_ck_wrap_of_a_former_agent_opens_but_is_never_sealed_under(store_dsn: str) -> None:
    def removed(log: v.LogBuilder) -> None:
        log.add("remove", "alice", subject=pid("bot"))
        log.invite_user("alice", "bob5", wgs={})
        log.accept("bob5")
        log.agent_add("bob5", "bot5", "bob5", 1)

    world = agent_world(removed)
    forged = {**wrap_for("zed", "cred", ALICE, 4), "wrapped_by": pid("bot")}
    world.wraps += [
        wrap_for("alice", "cred", ALICE, 1),
        wrap_for("bot", "cred", ALICE, 2),  # the former agent rotated before it was removed
        wrap_for("bob", "cred", ALICE, 3),  # a stranger
        forged,  # a signature that is not the former agent's
        wrap_for("bot5", "cred", ALICE, 5),  # the agent of another user
    ]

    async def main() -> None:
        async with kit_env(store_dsn, world) as env:
            rows = await env.rows(
                "SELECT epoch, source FROM runner.scope_key WHERE scope = 'cred' AND scope_id = %s ORDER BY epoch",
                ALICE,
            )
            assert rows == [(1, "wrap"), (2, "former_agent")]
            assert await cache.newest_epoch(env.context, ALICE) == 1
            assert await cache.newest_epoch(env.context, pid("bob")) is None
            codes = sorted(a["detail"]["code"] for a in await env.audits())
            assert codes == ["signature_invalid", "wrap_unauthorized", "wrap_unauthorized"]

    asyncio.run(main())
