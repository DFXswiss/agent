"""U-RUN-profiles (KOR-51, ADR-091 section 3): the pipeline checks every operation against the session's effective
profile before anything else (a refusal is registered `rejected` with `rights_denied` and a sentence), checks the
profile a `subagent.request` names and registers it as `child_profile`, and keeps the child's profile the hub answers.
Without a profile nothing changes (A1, E-11)."""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.types.json import Jsonb

from agent_runner.operations import register
from agent_runner.profiles import model, state

from ..operations.kit import ok, run, setup
from ..reconcile.rig import Env, Rig
from ..sessions.kit import WG, session_id
from .test_state import NARROW, READ_ONLY

SUBAGENT = {"purpose": "Review", "prompt": "Review PR 7."}
TITLE = {"title": "Fix"}
CHILD = session_id(42)
REJECTED = {"state": "rejected", "error_code": "rights_denied"}
CHILD_PROFILE = {"name": "narrow", "operations": ["github.get_issue"], "reads": "task"}


async def _section(env: Env, sessions: dict[str, Any], names: dict[str, Any] | None = None) -> None:
    async with env.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO runner.profile_state (id, section, received_at) VALUES (1, %s, now())"
            " ON CONFLICT (id) DO UPDATE SET section = EXCLUDED.section",
            (Jsonb({"api": 1, "sessions": sessions, "workgroups": {WG: names or {}}}),),
        )


def test_without_a_profile_nothing_changes(rig: Rig) -> None:
    async def body(env: Env) -> None:
        kit_ = await setup(env, executors={"task.set_title": ok, "subagent.request": ok})
        sid = await kit_.session()
        assert (await kit_.run(sid, "task.set_title", TITLE)).state == "succeeded"
        await _section(env, {sid: None})
        assert (await kit_.run(sid, "task.set_title", TITLE)).state == "succeeded"
        kit_.hub.decisions.append({"state": "accepted", "child_session_id": CHILD})
        await kit_.run(sid, "subagent.request", SUBAGENT)
        assert "child_profile" not in kit_.hub.requests[-1]

    run(rig, body)


@pytest.mark.parametrize(("depth", "who"), [(0, "A person can change the task's profile."), (1, "Ask the session")])
def test_a_type_outside_the_profile_is_rejected_locally(rig: Rig, depth: int, who: str) -> None:
    async def body(env: Env) -> None:
        kit_ = await setup(env, executors={"task.set_title": ok})
        sid = await kit_.session(depth=depth)
        await _section(env, {sid: READ_ONLY})
        kit_.hub.decisions.append(REJECTED)
        op = await kit_.run(sid, "task.set_title", TITLE)
        assert op.state == "rejected"
        sent = kit_.hub.requests[-1]
        assert (sent["local_check"], sent["error_code"]) == ("rejected", "rights_denied")
        [answer] = kit_.answers()
        assert answer["error"]["code"] == "rights_denied"
        assert "profile read_only, which does not allow task.set_title" in answer["error"]["message"]
        assert who in answer["error"]["message"]

    run(rig, body)


def test_the_child_profile_is_checked_registered_and_kept(rig: Rig) -> None:
    async def body(env: Env) -> None:
        kit_ = await setup(env, executors={"subagent.request": ok})
        sid = await kit_.session()
        await _section(
            env,
            {sid: READ_ONLY},
            {
                "narrow": NARROW,
                "read_only": READ_ONLY,
                "wide": {**NARROW, "name": "wide", "operations": ["github.push"]},
            },
        )
        kit_.hub.decisions.append({"state": "accepted", "child_session_id": CHILD, "child_profile": CHILD_PROFILE})
        op = await kit_.run(sid, "subagent.request", {**SUBAGENT, "profile": "narrow"})
        assert op.state == "succeeded", op.answer
        assert kit_.hub.requests[-1]["child_profile"] == "narrow"
        async with env.pool.connection() as conn:
            assert await state.effective(conn, CHILD, sid) == model.parse(CHILD_PROFILE)
        for name in ("wide", "unknown"):
            kit_.hub.decisions.append(REJECTED)
            refused = await kit_.run(sid, "subagent.request", {**SUBAGENT, "prompt": name, "profile": name})
            assert refused.state == "rejected"
            assert kit_.hub.requests[-1]["error_code"] == "rights_denied"
        kit_.hub.decisions.append({"state": "rejected", "error_code": "params_invalid"})
        invalid = await kit_.run(sid, "subagent.request", {**SUBAGENT, "prompt": "x", "profile": "Bad Name"})
        assert kit_.hub.requests[-1]["error_code"] == "params_invalid"
        assert invalid.state == "rejected"

    run(rig, body)


def test_a_child_accepted_without_a_named_profile_keeps_nothing(rig: Rig) -> None:
    async def body(env: Env) -> None:
        kit_ = await setup(env, executors={"subagent.request": ok})
        sid = await kit_.session()
        await _section(env, {sid: READ_ONLY})
        kit_.hub.decisions.append({"state": "accepted", "child_session_id": CHILD})
        assert (await kit_.run(sid, "subagent.request", SUBAGENT)).state == "succeeded"
        async with env.pool.connection() as conn:
            cursor = await conn.execute("SELECT count(*) FROM runner.session_profile")
            assert await cursor.fetchone() == (0,)
            assert await state.effective(conn, CHILD, sid) == model.parse(READ_ONLY)

    run(rig, body)


def test_the_decision_with_a_child_profile() -> None:
    found = register.parse_decision({"state": "accepted", "child_session_id": CHILD, "child_profile": None})
    assert (found.child_profile, found.child_profile_named) == (None, True)
    found = register.parse_decision({"state": "accepted", "child_session_id": CHILD})
    assert found.child_profile_named is False
    with pytest.raises(ValueError, match="profile"):
        register.parse_decision({"state": "accepted", "child_profile": {"name": "x"}})
    with pytest.raises(ValueError, match="decision"):
        register.parse_decision({"state": "denied", "error_code": "rights_denied", "child_profile": None})
