"""U-RUN-operations: duplicates return the original's result (03-api.md section 9 "Result delivery", 05-runner.md
section 12 step 6; ADR-012) - from the local row of an original that ran here, or from the verified
`operation.result` event of another device pulled by the real inbox; within the MCP wait, else `pending` with
`duplicate_of`; `get_operation` (`answer`, `wait_answer`) yields the result later."""

from __future__ import annotations

from typing import Any

from agent_runner.crypto.ids import uuid7
from agent_runner.operations.extensions import OperationContext

from ..reconcile.rig import Rig, seal
from ..sessions.kit import ORG, TASK, WG
from .kit import OKit, ok, run, setup

REPO = "example-org/example-repo"
RESULT = {"comment_id": 11, "url": "https://github.com/example-org/example-repo/pull/7#c11"}
PARAMS = {"repo": REPO, "number": 7, "body": "Looks good."}


async def comment_kit(env: Any) -> tuple[OKit, list[str]]:
    posted: list[str] = []

    async def comment(ctx: OperationContext) -> dict[str, Any]:
        posted.append(ctx.operation.id)
        return RESULT

    found = await setup(env, executors={"github.comment": comment})
    await found.settings([REPO])
    await found.connect()
    return found, posted


def duplicate_of(original: str, state: str = "succeeded") -> dict[str, Any]:
    return {"state": "duplicate", "duplicate_of": original, "original": {"state": state}}


def remote_result(kit_: OKit, original: str, body: dict[str, Any], *, label: str, ref: str | None = None) -> None:
    """An `operation.result` event of the original, written by the device, served by the hub at the next seq."""
    kit_.scope_seq += 1
    org = kit_.env.world.orgs[ORG]
    sealed = seal(org, "rdev", "operation.result", label=label, body=body, ref=ref or original, task=TASK)
    kit_.env.world.add_event(WG, kit_.scope_seq, sealed)


def test_the_same_comment_twice_returns_the_original_result_and_posts_once(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, posted = await comment_kit(env)
        sid = await kit_.session()
        first = await kit_.run(sid, "github.comment", PARAMS)
        kit_.hub.decisions.append(duplicate_of(first.id))
        second = await kit_.run(sid, "github.comment", PARAMS)
        assert posted == [first.id]
        assert kit_.hub.requests[0]["dedupe"] == kit_.hub.requests[1]["dedupe"]
        assert (second.state, second.duplicate_of) == ("duplicate", first.id)
        expected = {"operation_id": second.id, "status": "duplicate", "duplicate_of": first.id, "result": RESULT}
        assert second.answer == expected
        assert kit_.answers()[-1] == expected
        # a duplicate writes no event and no transition (the hub stored the decision)
        assert [b["operation_id"] for k, b in await kit_.outbox() if k == "operation_transition"] == [first.id] * 2
        assert kit_.refreshed == []  # the original is local: no pull needed

    run(rig, body)


def test_a_duplicate_of_another_device_reads_the_pulled_result_event(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, posted = await comment_kit(env)
        sid = await kit_.session()
        original = uuid7()
        remote_result(kit_, original, {"operation_id": original, "status": "succeeded", "result": RESULT}, label="r1")
        kit_.hub.decisions.append(duplicate_of(original))
        op = await kit_.run(sid, "github.comment", PARAMS)
        assert op.answer == {"operation_id": op.id, "status": "duplicate", "duplicate_of": original, "result": RESULT}
        assert posted == []
        assert kit_.refreshed == [1]

    run(rig, body)


def test_an_original_that_failed_gives_its_error(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        original = uuid7()
        error = {"code": "upstream_error", "retryable": True, "message": "GitHub answered 502."}
        remote_result(kit_, original, {"operation_id": original, "status": "failed", "error": error}, label="r2")
        kit_.hub.decisions.append(duplicate_of(original, "executing"))
        op = await kit_.run(sid, "github.comment", PARAMS)
        assert op.answer == {"operation_id": op.id, "status": "failed", "error": error}

    run(rig, body)


def test_an_original_failed_on_this_device_gives_its_code(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        first = await kit_.run(sid, "github.comment", PARAMS)
        async with env.pool.connection() as conn:
            await conn.execute(
                "UPDATE runner.operation SET state = 'failed', error_code = 'timeout', retryable = NULL WHERE id = %s",
                (first.id,),
            )
        kit_.hub.decisions.append(duplicate_of(first.id, "executing"))
        op = await kit_.run(sid, "github.comment", PARAMS)
        assert op.answer == {"operation_id": op.id, "status": "failed", "error": {"code": "timeout"}}

    run(rig, body)


def test_nothing_final_within_the_mcp_wait_answers_pending_with_duplicate_of(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        original = uuid7()
        kit_.hub.decisions.append(duplicate_of(original, "executing"))
        start = len(env.clock.sleeps)
        op = await kit_.run(sid, "github.comment", PARAMS)
        assert op.answer == {"operation_id": op.id, "status": "pending", "duplicate_of": original}
        assert 19.0 <= sum(env.clock.sleeps[start:]) <= 20.5
        assert len(kit_.refreshed) >= 19
        assert kit_.notified == []  # not final: no operation.finished
        assert op.answered_at is not None
        # get_operation on the duplicate: still pending, then the original's result once it arrived
        assert await kit_.pipeline.answer(op.id) == op.answer
        remote_result(kit_, original, {"operation_id": original, "status": "succeeded", "result": RESULT}, label="r3")
        await kit_.env.sync.pusher.drain()
        from .kit import pull  # noqa: PLC0415

        await pull(env)
        found = await kit_.pipeline.wait_answer(op.id, 5)
        assert found == {"operation_id": op.id, "status": "duplicate", "duplicate_of": original, "result": RESULT}

    run(rig, body)


def test_a_late_duplicate_answers_pending_at_once(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        original = uuid7()
        kit_.hub.decisions.append(duplicate_of(original, "accepted"))
        op_id = await kit_.request(sid, "github.comment", PARAMS)
        await kit_.age(op_id, 30)
        start = len(env.clock.sleeps)
        await kit_.pipeline.process(op_id)
        op = await kit_.op(op_id)
        assert op.answer is not None
        assert op.answer["status"] == "pending"
        assert env.clock.sleeps[start:] == []

    run(rig, body)


def test_a_local_original_still_running_is_waited_for_without_pulls(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        first_id = await kit_.request(sid, "github.comment", PARAMS)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.operation SET state = 'executing' WHERE id = %s", (first_id,))
        kit_.hub.decisions.append(duplicate_of(first_id, "executing"))
        dup_id = await kit_.request(sid, "github.comment", PARAMS)
        await kit_.pipeline.process(dup_id)
        dup = await kit_.op(dup_id)
        assert dup.answer == {"operation_id": dup_id, "status": "pending", "duplicate_of": first_id}
        assert kit_.refreshed == []
        # the original ends later: get_operation of the duplicate gives its result
        async with env.pool.connection() as conn:
            await conn.execute(
                "UPDATE runner.operation SET state = 'succeeded', result = %s::jsonb WHERE id = %s",
                ('{"comment_id": 11, "url": "https://github.com/example-org/example-repo/pull/7#c11"}', first_id),
            )
        assert await kit_.pipeline.answer(dup_id) == {
            "operation_id": dup_id,
            "status": "duplicate",
            "duplicate_of": first_id,
            "result": RESULT,
        }

    run(rig, body)


def test_failed_pulls_while_waiting_are_logged(rig: Rig) -> None:
    async def body(env: Any) -> None:
        async def broken() -> object:
            raise ConnectionError

        kit_ = await setup(env, executors={"task.set_title": ok}, refresh=broken)
        sid = await kit_.session()
        kit_.hub.decisions.append(duplicate_of(uuid7(), "executing"))
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"}, key="k")
        await kit_.age(op_id, 18)
        await kit_.pipeline.process(op_id)
        assert any("refresh_failed" in line and "ConnectionError" in line for line in kit_.lines)

    run(rig, body)


def test_get_operation_answers(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        assert await kit_.pipeline.answer(uuid7()) is None
        assert await kit_.pipeline.wait_answer(uuid7(), 5) is None
        op = await kit_.run(sid, "github.comment", PARAMS)
        assert await kit_.pipeline.wait_answer(op.id, 5) == op.answer
        # an operation that is still being registered is pending, also after the wait
        op_id = await kit_.request(sid, "github.comment", PARAMS)
        assert await kit_.pipeline.answer(op_id) == {"operation_id": op_id, "status": "pending"}
        start = len(env.clock.sleeps)
        assert await kit_.pipeline.wait_answer(op_id, 2.5) == {"operation_id": op_id, "status": "pending"}
        assert env.clock.sleeps[start:] == [1.0, 1.0, 0.5]

    run(rig, body)


def test_a_pipeline_without_a_refresh_still_waits_for_the_original(rig: Rig) -> None:
    async def body(env: Any) -> None:
        from agent_runner.operations.pipeline import Pipeline  # noqa: PLC0415

        kit_, _ = await comment_kit(env)
        bare = Pipeline(
            kit_.kit.session_env,
            client=env.sync.client,
            sealer=env.sync.pusher.sealer,
            executors={"github.comment": kit_.pipeline.executors["github.comment"]},
            sleep=env.clock.sleep,
        )
        sid = await kit_.session()
        kit_.hub.decisions.append(duplicate_of(uuid7(), "executing"))
        op_id = await kit_.request(sid, "github.comment", PARAMS)
        await kit_.age(op_id, 18)
        await bare.process(op_id)
        op = await kit_.op(op_id)
        assert op.answer is not None
        assert op.answer["status"] == "pending"

    run(rig, body)
