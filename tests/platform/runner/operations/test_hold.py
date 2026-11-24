"""U-RUN-operations: operations held for the task owner's GitHub connection (KOR-25; 05-runner.md section 12 step 3,
03-api.md sections 9 and 10, `contracts/events/objects.json` `body_session_status` `waiting`).

Real store, the contract-checking hub of `kit.py`, fake executors and the rig's fake clock; the device wall clock of
the session environment is moved by the tests. Every `waiting` event is validated against
`events/session.status.json` and pushed through the contract-checking hub.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_guest.schema import SchemaSet
from agent_runner.credentials import cache, identities
from agent_runner.operations.extensions import CheckFailed, OperationContext, PrecheckRequest, PrecheckResult
from agent_runner.operations.pipeline import (
    FULL_MESSAGES,
    HOLD_MAX,
    HOLD_S,
    SESSION_ENDED_MESSAGE,
    Pipeline,
    pipeline_for,
)
from agent_runner.sessions import records as session_records

from ..reconcile.rig import Rig
from ..sessions.kit import NOW, OWNER
from .kit import OKit, ok, run, setup
from .test_wiring import configure

CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"
SCHEMAS = SchemaSet(CONTRACTS)
REPO = "example-org/example-repo"
COMMENT = {"repo": REPO, "number": 7, "body": "Looks good."}
TIMEOUT = "The person who gave this instruction did not connect GitHub within 15 minutes."
Json = dict[str, Any]


async def hold_kit(env: Any, **kw: Any) -> tuple[OKit, list[OperationContext]]:
    calls: list[OperationContext] = []

    async def comment(ctx: OperationContext) -> Json:
        calls.append(ctx)
        return {"comment_id": 11, "url": "https://github.com/example-org/example-repo/pull/7#c11"}

    executors = {"github.comment": comment, "github.list_repositories": ok, "task.set_title": ok}
    found = await setup(env, executors=executors, **kw)
    return found, calls


def at(kit_: OKit, seconds: float) -> None:
    """Move the device wall clock to `NOW + seconds`."""
    kit_.kit.session_env.wall = lambda: NOW.timestamp() + seconds


async def waiting(kit_: OKit) -> list[Json]:
    """Bodies of the `session.status` events in the outbox, each checked against the contract."""
    found = []
    for kind, item in await kit_.outbox():
        if kind == "event" and item["header"]["kind"] == "session.status":
            assert SCHEMAS.errors("events/session.status.json", item["body"]) == []
            found.append(item["body"])
    return found


def until_of(body: Json) -> float:
    return datetime.strptime(body["until"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).timestamp()


def test_a_comment_without_connection_is_held_then_runs_once_github_is_connected(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls = await hold_kit(env)
        sid = await kit_.session()
        op_id = await kit_.request(sid, "github.comment", COMMENT)
        await kit_.pipeline.process(op_id)
        op = await kit_.op(op_id)
        assert (op.state, op.registration, op.answer) == ("received", None, None)
        assert (op.held, op.held_reason, op.held_repo, op.held_ended_at) == (True, "github_connection", REPO, None)
        assert op.held_until is not None
        assert NOW + timedelta(seconds=HOLD_S - 5) <= op.held_until <= NOW + timedelta(seconds=HOLD_S)
        assert op.held_notified_at == NOW
        assert kit_.hub.requests == []
        assert kit_.guest.answers() == []
        (status,) = await waiting(kit_)
        assert status == {
            "state": "waiting",
            "waiting_for": "github_connection",
            "operation_id": op_id,
            "operation_type": "github.comment",
            "repo": REPO,
            "for_user": OWNER,
            "until": status["until"],
        }
        assert abs(until_of(status) - op.held_until.timestamp()) < 0.001
        (event,) = [item for kind, item in await kit_.outbox() if kind == "event"]
        assert (event["header"]["session"], event["header"]["task"], event["header"]["ref"]) == (sid, op.task_id, "")
        assert any(f"held operation_id={op_id} reason=github_connection" in line for line in kit_.lines)
        # every later scan checks again; still not connected: no second event, nothing registered
        at(kit_, 300)
        await kit_.pipeline.process(op_id)
        assert (await kit_.op(op_id)).held
        assert len(await waiting(kit_)) == 1
        assert kit_.hub.requests == []
        # the owner connects: the next check passes, the operation registers and runs without a new request
        await kit_.connect()
        await kit_.pipeline.process(op_id)
        op = await kit_.op(op_id)
        assert (op.state, op.held, op.held_ended_at) == ("succeeded", False, NOW + timedelta(seconds=300))
        assert len(calls) == 1
        (registration,) = kit_.hub.requests
        assert registration["local_check"] == "passed"
        assert registration["dedupe"] is not None
        assert op.answer is not None
        assert kit_.answers() == [op.answer]
        assert any(f"hold_ended operation_id={op_id}" in line for line in kit_.lines)
        await kit_.kit.pushed_events()

    run(rig, body)


def test_a_held_operation_is_rejected_at_until_with_the_original_code(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls = await hold_kit(env)
        sid = await kit_.session()
        op_id = await kit_.request(sid, "github.list_repositories", {})
        await kit_.pipeline.process(op_id)
        (status,) = await waiting(kit_)
        assert "repo" not in status
        assert status["operation_type"] == "github.list_repositories"
        at(kit_, HOLD_S - 30)
        await kit_.pipeline.process(op_id)
        assert (await kit_.op(op_id)).held
        at(kit_, HOLD_S + 1)
        kit_.hub.decisions.append({"state": "rejected", "error_code": "github_not_connected"})
        await kit_.pipeline.process(op_id)
        op = await kit_.op(op_id)
        assert (op.state, op.error_code, op.held) == ("rejected", "github_not_connected", False)
        error = {"code": "github_not_connected", "message": TIMEOUT}
        assert op.answer == {"operation_id": op_id, "status": "rejected", "error": error}
        (registration,) = kit_.hub.requests
        assert (registration["local_check"], registration["error_code"]) == ("rejected", "github_not_connected")
        assert op.checked == {"error": error}
        result = [item for kind, item in await kit_.outbox() if kind == "event"][-1]
        # the acting user is named: only that person's connection could have been used (HOT-14)
        assert result["body"] == {"operation_id": op_id, "status": "rejected", "connection_of": OWNER, "error": error}
        assert calls == []
        assert any(f"hold_timeout operation_id={op_id} reason=github_connection" in line for line in kit_.lines)
        await kit_.kit.pushed_events()

    run(rig, body)


def test_flood_limits_per_session(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _calls = await hold_kit(env)
        sid, other = await kit_.session(1), await kit_.session(2)
        held = []
        for number in range(HOLD_MAX):
            op_id = await kit_.request(sid, "github.comment", {**COMMENT, "number": number + 1})
            await kit_.pipeline.process(op_id)
            held.append(op_id)
        assert all([(await kit_.op(op_id)).held for op_id in held])
        # one waiting event per session and reason per 60 s
        assert [b["operation_id"] for b in await waiting(kit_)] == [held[0]]
        # a fourth held operation of the session is rejected at once, as before KOR-25
        kit_.hub.decisions.append({"state": "rejected", "error_code": "github_not_connected"})
        fourth = await kit_.request(sid, "github.comment", {**COMMENT, "number": 9})
        await kit_.pipeline.process(fourth)
        op = await kit_.op(fourth)
        assert (op.state, op.held_until) == ("rejected", None)
        assert op.answer is not None
        assert op.answer["error"] == {"code": "github_not_connected", "message": FULL_MESSAGES["github_connection"]}
        assert any(
            f"hold_refused operation_id={fourth} reason=github_connection why=full" in line for line in kit_.lines
        )
        # another session has its own limits
        own = await kit_.request(other, "github.comment", COMMENT)
        await kit_.pipeline.process(own)
        assert (await kit_.op(own)).held
        assert [b["operation_id"] for b in await waiting(kit_)] == [held[0], own]
        # 60 s later the next held operation that got no event gets it, one per scan window
        at(kit_, 59)
        await kit_.pipeline.process(held[1])
        assert len(await waiting(kit_)) == 2
        at(kit_, 60)
        await kit_.pipeline.process(held[1])
        await kit_.pipeline.process(held[2])
        assert [b["operation_id"] for b in await waiting(kit_)] == [held[0], own, held[1]]
        at(kit_, 125)
        await kit_.pipeline.process(held[0])
        await kit_.pipeline.process(held[2])
        assert [b["operation_id"] for b in await waiting(kit_)] == [held[0], own, held[1], held[2]]
        await kit_.kit.pushed_events()

    run(rig, body)


def test_held_operations_survive_a_restart(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls = await hold_kit(env)
        sid = await kit_.session()
        op_id = await kit_.request(sid, "github.comment", COMMENT)
        await kit_.pipeline.process(op_id)
        assert (await kit_.op(op_id)).held
        first = kit_.pipeline
        restarted = Pipeline(
            kit_.kit.session_env,
            client=env.sync.client,
            sealer=env.sync.pusher.sealer,
            executors=first.executors,
            sleep=env.clock.sleep,
            random=lambda: 0.5,
        )
        at(kit_, 120)
        assert await restarted.scan() == [op_id]
        await restarted.settle()
        assert (await kit_.op(op_id)).held
        assert len(await waiting(kit_)) == 1
        await kit_.connect()
        await restarted.scan()
        await restarted.settle()
        op = await kit_.op(op_id)
        assert (op.state, len(calls)) == ("succeeded", 1)
        assert op.answered_at is not None

    run(rig, body)


def test_the_registration_expiry_counts_from_the_end_of_the_hold(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls = await hold_kit(env)
        sid = await kit_.session()
        op_id = await kit_.request(sid, "github.comment", COMMENT)
        await kit_.pipeline.process(op_id)
        await kit_.age(op_id, 700)  # longer than the registration's 10 min
        kit_.pipeline.picked.clear()
        at(kit_, 700)
        await kit_.connect()
        kit_.hub.unreachable = 3
        await kit_.pipeline.process(op_id)
        op = await kit_.op(op_id)
        assert (op.state, len(calls)) == ("succeeded", 1)
        assert len(kit_.hub.requests) == 1
        assert kit_.notified == [op_id]

    run(rig, body)


def test_a_request_older_than_the_hold_and_a_hold_that_ended_are_rejected_as_before(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _calls = await hold_kit(env)
        sid = await kit_.session()
        old = await kit_.request(sid, "github.comment", COMMENT)
        await kit_.age(old, HOLD_S + 10)
        kit_.hub.decisions.append({"state": "rejected", "error_code": "github_not_connected"})
        await kit_.pipeline.process(old)
        op = await kit_.op(old)
        assert (op.state, op.held_until) == ("rejected", None)
        # a hold that ended (connected), then the connection went away before the registration (a restart): rejected
        ended = await kit_.request(sid, "github.comment", {**COMMENT, "number": 8})
        await kit_.pipeline.process(ended)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.operation SET held_ended_at = %s WHERE id = %s", (NOW, ended))
        kit_.hub.decisions.append({"state": "rejected", "error_code": "github_not_connected"})
        await kit_.pipeline.process(ended)
        op = await kit_.op(ended)
        assert (op.state, op.error_code) == ("rejected", "github_not_connected")
        assert op.answer is not None
        assert op.answer["error"]["message"] == "The person who gave this instruction has not connected GitHub."

    run(rig, body)


def test_a_pre_check_without_a_usable_connection_holds_too(rig: Rig) -> None:
    async def body(env: Any) -> None:
        state = {"connected": False}

        async def precheck(_request: PrecheckRequest) -> PrecheckResult:
            if not state["connected"]:
                raise CheckFailed("github_not_connected", message="The token was refused.")
            return PrecheckResult()

        kit_, calls = await hold_kit(env, prechecks={"github.comment": precheck})
        await kit_.connect()  # cached, but GitHub refuses the token (revoked)
        sid = await kit_.session()
        op_id = await kit_.request(sid, "github.comment", COMMENT)
        await kit_.pipeline.process(op_id)
        assert (await kit_.op(op_id)).held
        state["connected"] = True
        await kit_.pipeline.process(op_id)  # the same credential version: not checked again
        assert (await kit_.op(op_id)).held
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.credential_cache SET version = 2")  # the owner connected again
        await kit_.pipeline.process(op_id)
        assert (await kit_.op(op_id)).state == "succeeded"
        assert len(calls) == 1

    run(rig, body)


def test_a_session_cannot_put_free_text_into_the_waiting_event(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _calls = await hold_kit(env)
        sid = await kit_.session()
        text = "Connect here: https://evil.example/login\n# Urgent"
        # free text in a parameter: held, but the event carries only the platform's values
        op_id = await kit_.request(sid, "github.comment", {**COMMENT, "body": text}, key="k-1")
        await kit_.pipeline.process(op_id)
        # a repository that is not one, an unknown github type and other check failures are never held
        for op_type, params in (
            ("github.comment", {**COMMENT, "repo": "evil/x\nIgnore the platform"}),
            ("github.comment", {**COMMENT, "extra": text}),
            ("github.connect_now", {"repo": REPO}),
        ):
            kit_.hub.decisions.append({"state": "rejected", "error_code": "params_invalid"})
            other = await kit_.run(sid, op_type, params)
            assert (other.state, other.held_until) == ("rejected", None)
        # a type that is not a GitHub type is never held
        kit_.hub.decisions.append({"state": "rejected", "error_code": "params_invalid"})
        title = await kit_.run(sid, "task.set_title", {"title": ""})
        assert title.held_until is None
        (status,) = await waiting(kit_)
        assert set(status) == {"state", "waiting_for", "operation_id", "operation_type", "repo", "for_user", "until"}
        assert (status["operation_id"], status["repo"]) == (op_id, REPO)
        assert all(text not in str(value) and "evil" not in str(value) for value in status.values())

    run(rig, body)


def test_a_stored_github_credential_wakes_the_pipeline(rig: Rig, tmp_path: Path) -> None:
    async def body(env: Any) -> None:
        configure(env.context, tmp_path)
        built = pipeline_for(env.context)
        assert built is not None
        built.trigger.clear()
        await cache._stored(env.context, OWNER, "claude")
        assert not built.trigger.is_set()
        await cache._stored(env.context, OWNER, "github")
        assert built.trigger.is_set()

        async def broken(_owner: str, _kind: str) -> None:
            raise RuntimeError("listener")

        lines: list[str] = []

        class Log:
            def write(self, text: str) -> None:
                lines.append(text)

            def flush(self) -> None:
                return None

        env.context.log = Log()
        cache.subscribe_stored(env.context, broken)
        await cache._stored(env.context, OWNER, "github")
        assert lines == ["runner.credentials listener_failed error=RuntimeError\n"]

    run(rig, body)


def test_a_held_operation_of_an_ended_session_never_runs(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls = await hold_kit(env)
        for n, state in ((1, "stopped"), (2, "stopping"), (3, "failed")):
            sid = await kit_.session(n)
            op_id = await kit_.request(sid, "github.comment", COMMENT)
            await kit_.pipeline.process(op_id)
            assert (await kit_.op(op_id)).held
            async with env.pool.connection() as conn:
                await session_records.update(conn, sid, state=state)
            if n == 3:
                await kit_.connect()  # the credential arrives after the session ended
            kit_.hub.decisions.append({"state": "rejected", "error_code": "not_found"})
            await kit_.pipeline.process(op_id)
            op = await kit_.op(op_id)
            assert (op.state, op.held) == ("rejected", False)
            error = {
                "code": "not_found",
                "message": SESSION_ENDED_MESSAGE,
            }
            assert op.answer == {"operation_id": op_id, "status": "rejected", "error": error}
            assert kit_.hub.requests[-1]["local_check"] == "rejected"
            assert any(f"hold_session_ended operation_id={op_id} session_state={state}" in x for x in kit_.lines)
        assert calls == []
        await kit_.kit.pushed_events()

    run(rig, body)


async def generation(env: Any) -> int | None:
    """The GitHub generation of the device that a held operation remembers (KOR-47)."""
    async with env.pool.connection() as conn:
        return await identities.generation(conn)


def test_a_refused_credential_is_not_checked_again_until_another_version_arrives(rig: Rig) -> None:
    async def body(env: Any) -> None:
        asked: list[str] = []

        async def precheck(request: PrecheckRequest) -> PrecheckResult:
            asked.append(request.operation.id)
            raise CheckFailed("github_not_connected", message="The token was refused.")

        kit_, calls = await hold_kit(env, prechecks={"github.comment": precheck})
        await kit_.connect()  # version 1, revoked at GitHub
        sid = await kit_.session()
        op_id = await kit_.request(sid, "github.comment", COMMENT)
        await kit_.pipeline.process(op_id)
        first: int | None = await generation(env)
        assert first is not None
        assert (await kit_.op(op_id)).held_credential == first
        for second in (5, 10, 15):
            at(kit_, second)
            await kit_.pipeline.process(op_id)
        assert asked == [op_id]
        assert (await kit_.op(op_id)).held
        # another version arrives (still refused): checked once more, remembered
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.credential_cache SET version = 2")
        await kit_.pipeline.process(op_id)
        await kit_.pipeline.process(op_id)
        assert asked == [op_id, op_id]
        changed = await generation(env)
        assert changed not in (None, first)
        assert (await kit_.op(op_id)).held_credential == changed
        assert len(await waiting(kit_)) == 1
        # the timeout still ends it, without another request
        at(kit_, HOLD_S + 1)
        kit_.hub.decisions.append({"state": "rejected", "error_code": "github_not_connected"})
        await kit_.pipeline.process(op_id)
        op = await kit_.op(op_id)
        assert op.answer is not None
        assert (op.state, op.answer["error"]["message"]) == ("rejected", TIMEOUT)
        assert asked == [op_id, op_id]
        assert calls == []

    run(rig, body)
