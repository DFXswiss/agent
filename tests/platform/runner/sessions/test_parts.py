"""U-RUN-sessions: extension points, desired-state checks, the profile store and consumer, defaults of `SessionEnv`
(ADR-039, ADR-032; 03-api.md sections 6.1 and 10; 05-runner.md section 8)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.errors import RunnerError
from agent_runner.inbox.dispatch import VerifiedEvent
from agent_runner.inbox.pull import inbox_for
from agent_runner.reconcile.wire import ResponseInvalid
from agent_runner.sessions import consumers, profiles
from agent_runner.sessions import desired as desired_module
from agent_runner.sessions import env as env_module
from agent_runner.sessions.extensions import (
    StateObserver,
    first_prompt_provider,
    no_first_prompt,
    state_observers,
)
from agent_runner.sessions.start import Starter

from ..reconcile.rig import Rig, World, scope_key, seal, standard_world
from .kit import ORG, OWNER, TASK, WG, WG_OTHER, desired, kit, session_id, wire

# -- extension points -----------------------------------------------------------------------------------------------


def test_no_providers_in_an_empty_package(area_package: Callable[[dict[str, str]], str]) -> None:
    package = area_package({"other/x.py": ""})
    assert first_prompt_provider(package) is no_first_prompt
    assert state_observers(package) == []
    assert asyncio.run(no_first_prompt(None, None)) is None  # type: ignore[arg-type]


def test_providers_are_discovered(area_package: Callable[[dict[str, str]], str]) -> None:
    package = area_package(
        {
            "prompts/session_prompt.py": "async def first(context, session):\n    return None\nFIRST_PROMPT = first\n",
            "prompts/session_state.py": (
                "from agent_runner.sessions.extensions import StateObserver\n"
                "async def handle(journal, conn, session, body):\n    return None\n"
                "OBSERVERS = [StateObserver('prompts.delivery', handle)]\n"
            ),
        }
    )
    assert first_prompt_provider(package).__name__ == "first"
    assert [o.name for o in state_observers(package)] == ["prompts.delivery"]


@pytest.mark.parametrize(
    ("files", "code"),
    [
        ({"a/session_prompt.py": "FIRST_PROMPT = 1\n"}, "registry_invalid"),
        (
            {
                "a/session_prompt.py": "FIRST_PROMPT = print\n",
                "b/session_prompt.py": "FIRST_PROMPT = print\n",
            },
            "registry_duplicate",
        ),
        ({"a/session_state.py": "OBSERVERS = 'x'\n"}, "registry_invalid"),
        ({"a/session_state.py": "OBSERVERS = [1]\n"}, "registry_invalid"),
        (
            {
                "a/session_state.py": (
                    "from agent_runner.sessions.extensions import StateObserver\n"
                    "OBSERVERS = [StateObserver('Bad Name', print)]\n"
                )
            },
            "registry_invalid",
        ),
        (
            {
                "a/session_state.py": (
                    "from agent_runner.sessions.extensions import StateObserver\n"
                    "OBSERVERS = [StateObserver('x', print), StateObserver('x', print)]\n"
                )
            },
            "registry_duplicate",
        ),
    ],
)
def test_invalid_registrations(area_package: Callable[[dict[str, str]], str], files: dict[str, str], code: str) -> None:
    package = area_package(files)

    def load() -> None:
        first_prompt_provider(package)
        state_observers(package)

    with pytest.raises(RunnerError) as caught:
        load()
    assert caught.value.code == code


def test_observer_type() -> None:
    observer = StateObserver("x", print)  # type: ignore[arg-type]
    assert observer.name == "x"


# -- desired state --------------------------------------------------------------------------------------------------


def test_desired_sessions_are_parsed() -> None:
    item = desired(model_id_pref="deepseek/deepseek-v4-pro", parent_session_id=None)
    assert desired_module.desired_sessions({"sessions": [wire(item)]}) == [item]
    child = desired(2, depth=1, parent_session_id=session_id(1))
    assert desired_module.parse(wire(child)) == child
    # ADR-075: the previous session and the preferred VM
    placed = replace(desired(3), resume_from=session_id(1), vm_id_pref="vm-0000abcd")
    assert desired_module.parse(wire(placed)) == placed
    # an older hub's `vm_new_allowed` is read past: no fixed number of VMs per workgroup (ADR-075 addendum 2)
    assert desired_module.parse({**wire(placed), "vm_new_allowed": False}) == placed
    older = {key: value for key, value in wire(desired(4)).items() if key != "vm_id_pref"}
    parsed = desired_module.parse({**older, "resume_from": None})
    assert (parsed.resume_from, parsed.vm_id_pref) == (None, None)


@pytest.mark.parametrize(
    "change",
    [
        {"depth": -1},
        {"depth": True},
        {"state": "bogus"},
        {"model_access_pref": "free"},
        {"model_id_pref": ""},
        {"model_id_pref": 5},
        {"session_id": "not-a-uuid"},
        {"parent_session_id": "x"},
        {"resume_from": "x"},
        {"vm_id_pref": "vm-1"},
        {"vm_id_pref": 5},
    ],
)
def test_malformed_desired_sessions_are_refused(change: dict[str, Any]) -> None:
    with pytest.raises(ResponseInvalid):
        desired_module.parse({**wire(desired()), **change})
    with pytest.raises(ResponseInvalid):
        desired_module.desired_sessions({"sessions": {}})


# -- env defaults ---------------------------------------------------------------------------------------------------


def test_env_defaults_do_nothing() -> None:
    assert asyncio.run(env_module._no_refresh()) is None
    env_module._no_log("x")
    env_module._no_notify()


# -- profiles -------------------------------------------------------------------------------------------------------


def run(rig: Rig, body: Any, world: World | None = None) -> None:
    async def main() -> None:
        async with rig.env(world or standard_world()) as env:
            await body(env)

    asyncio.run(main())


def profiles_world(title: str = "Title", ts_label: str = "a") -> World:
    world = standard_world()
    org = next(iter(world.orgs.values()))
    world.add_event(WG, 1, seal(org, "alice", "task.meta", label=f"tm-{ts_label}", task=TASK, body={"title": title}))
    return world


def test_profiles_are_kept_latest_first_and_names_known_before_the_start_need_no_pull(rig: Rig) -> None:
    async def body(env: Any) -> None:
        async def never() -> None:
            raise AssertionError("no pull needed")

        found = await kit(env, refresh=never)
        org = next(iter(env.world.orgs.values()))
        env.world.add_event(org.org, 1, seal(org, "alice", "org.profile", scope="org", label="o", body={"name": "O"}))
        inbox = inbox_for(env.context)
        assert inbox is not None
        await inbox.pull_all()
        async with env.pool.connection() as conn:
            assert await profiles.name(conn, env.sync.storage_key, "task.meta", ORG, TASK, scope_id=WG) == "Title"
            # an older event (same ts, smaller id) never replaces the stored one; a newer one does
            cur = await conn.execute("SELECT event_id::text FROM runner.profile_event WHERE kind = 'task.meta'")
            stored = (await cur.fetchone())[0]
            sealed = seal(org, "alice", "task.meta", label="tm-z", task=TASK, body={"title": "Newer"})
            header = dict(sealed["header"])
            older = {**header, "id": "00000000-0000-7000-8000-000000000000"}
            assert not await profiles.remember(conn, header=older, sealed=sealed, author_sig="x")
            newer = {**header, "ts": "2026-11-21T13:00:00.000Z"}
            assert await profiles.remember(conn, header=newer, sealed=sealed, author_sig=_alice_sig(env))
            assert await profiles.name(conn, env.sync.storage_key, "task.meta", ORG, TASK, scope_id=WG) == "Newer"
            assert stored != header["id"]
        assert (await Starter(found.session_env).start(desired())).started
        markdown = found.guest.calls[1][1]["harness"]["context_markdown"]
        assert "- Organization: O" in markdown
        assert "- Task: Newer" in markdown

    run(rig, body, profiles_world())


def _alice_sig(env: Any) -> str:
    from agent_runner.crypto.log import vectors as v  # noqa: PLC0415

    return str(v.sig_pk("alice"))


def test_names_that_do_not_open_are_unknown(rig: Rig) -> None:
    async def body(env: Any) -> None:
        await kit(env)
        org = next(iter(env.world.orgs.values()))
        async with env.pool.connection() as conn:
            assert await profiles.name(conn, env.sync.storage_key, "org.profile", ORG, ORG) is None
            sealed = seal(org, "alice", "org.profile", scope="org", label="x", body={"name": "  "})
            await profiles.remember(conn, header=sealed["header"], sealed=sealed, author_sig=_alice_sig(env))
            assert await profiles.name(conn, env.sync.storage_key, "org.profile", ORG, ORG) is None  # blank
            wrong = seal(org, "alice", "member.profile", scope="org", label="y", ref=OWNER, body={"display_name": "A"})
            await profiles.remember(conn, header=wrong["header"], sealed=wrong, author_sig=_alice_sig(env)[::-1])
            assert await profiles.name(conn, env.sync.storage_key, "member.profile", ORG, OWNER) is None  # bad sig
            later = seal(org, "alice", "wg.profile", scope="org", label="z", wg=WG, epoch=7, body={"name": "W"},
                         key=scope_key("org", ORG, 7))  # fmt: skip
            await profiles.remember(conn, header=later["header"], sealed=later, author_sig=_alice_sig(env))
            assert await profiles.name(conn, env.sync.storage_key, "wg.profile", ORG, WG) is None  # key not held

    run(rig, body)


def test_consumer_skips_events_it_cannot_attribute(rig: Rig) -> None:
    async def body(env: Any) -> None:
        await kit(env)
        org = next(iter(env.world.orgs.values()))
        sealed = seal(org, "alice", "org.profile", scope="org", label="c", body={"name": "N"})
        header = sealed["header"]

        def event(**changes: Any) -> VerifiedEvent:
            return VerifiedEvent(
                org_id=changes.pop("org_id", ORG),
                scope="org",
                scope_id=ORG,
                scope_seq=1,
                received_at="2026-11-21T12:31:00.000Z",
                header={**header, **changes},
                body={"name": "N"},
                sealed=sealed,
            )

        async with env.pool.connection() as conn:
            await consumers.keep_profile(env.context, conn, event(org_id="01930001-0000-7000-8000-0000000000ff"))
            await consumers.keep_profile(env.context, conn, event(lh=999))
            await consumers.keep_profile(env.context, conn, event(lh="0"))
            await consumers.keep_profile(env.context, conn, event(author="01930003-0000-7000-8000-0000000000ff"))
            cur = await conn.execute("SELECT count(*) FROM runner.profile_event")
            assert (await cur.fetchone())[0] == 0
            await consumers.keep_profile(env.context, conn, event())
            cur = await conn.execute("SELECT count(*) FROM runner.profile_event")
            assert (await cur.fetchone())[0] == 1

    run(rig, body)


def test_consumer_registration() -> None:
    (consumer,) = consumers.CONSUMERS
    assert consumer.kinds == {"org.profile", "wg.profile", "member.profile", "task.meta", "task.agent_name"}
    assert profiles.object_of("member.profile", {"ref": "r"}) == "r"
    assert SimpleNamespace is not None


def test_a_task_title_of_another_workgroup_is_never_used(rig: Rig) -> None:
    """The hub names the task; a title stored for that task ID in another workgroup of the device stays unknown."""

    async def body(env: Any) -> None:
        await kit(env)
        org = next(iter(env.world.orgs.values()))
        foreign = seal(org, "alice", "task.meta", scope_id=WG_OTHER, label="tf", task=TASK, body={"title": "Secret"},
                       key=scope_key("wg", WG_OTHER, 1))  # fmt: skip
        async with env.pool.connection() as conn:
            await profiles.remember(conn, header=foreign["header"], sealed=foreign, author_sig=_alice_sig(env))
            found = await profiles.names(conn, env.sync.storage_key, org_id=ORG, wg_id=WG, task_id=TASK,
                                         owner_id=OWNER)  # fmt: skip
            assert found.task is None
            other = await profiles.name(conn, env.sync.storage_key, "task.meta", ORG, TASK, scope_id=WG_OTHER)
            assert other is None  # the device holds no key of that workgroup in this world either

    run(rig, body)
