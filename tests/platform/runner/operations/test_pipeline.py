"""U-RUN-operations: the pipeline "register-then-execute" with a contract-checking hub, fake executors and the rig's
fake clock (05-runner.md section 12, 03-api.md sections 8.3, 9, 12; ADR-012).

Every registration is validated against `api/device-operations.request.json` and every decision against the response
schema; every outbox item against `outbox/<kind>.json` when it is pushed.
"""

from __future__ import annotations

from typing import Any

from agent_runner.guestlink.errors import GuestRefused, GuestUnreachable
from agent_runner.operations.extensions import (
    CheckFailed,
    OperationContext,
    OperationFailed,
    PrecheckRequest,
    PrecheckResult,
)
from agent_runner.operations.pipeline import Pipeline

from ..reconcile.rig import Rig
from ..sessions.kit import TASK, WG
from .kit import OKit, ok, run, setup

REPO = "example-org/example-repo"
SHA = "c" * 40


async def comment_kit(env: Any, **kw: Any) -> tuple[OKit, list[OperationContext]]:
    calls: list[OperationContext] = []

    async def comment(ctx: OperationContext) -> dict[str, Any]:
        calls.append(ctx)
        return {"comment_id": 11, "url": "https://github.com/example-org/example-repo/pull/7#c11"}

    executors = {"github.comment": comment, "task.set_title": ok, **kw.pop("executors", {})}
    found = await setup(env, executors=executors, **kw)
    await found.settings([REPO])
    await found.connect()
    return found, calls


def test_set_title_writes_a_task_meta_event_then_the_result_and_the_transition(rig: Rig) -> None:
    async def body(env: Any) -> None:
        from agent_runner.operations.executors import EXECUTORS  # noqa: PLC0415

        ok_kit = await setup(env, executors=EXECUTORS)
        sid = await ok_kit.session()
        op = await ok_kit.run(sid, "task.set_title", {"title": "Fix the login"})
        assert (op.state, op.error_code, op.attempt) == ("succeeded", None, 1)
        assert op.answer == {"operation_id": op.id, "status": "succeeded", "result": {}}
        assert ok_kit.answers() == [op.answer]
        assert op.answered_at is not None
        # registration: passed, no dedupe slot for task.set_title, the params event carries the request
        (registration,) = ok_kit.hub.requests
        assert registration["local_check"] == "passed"
        assert registration["dedupe"] is None
        assert registration["type"] == "task.set_title"
        assert registration["requested_at"] == "2026-11-21T12:00:01.000Z"
        header, params = ok_kit.hub.params_event()
        assert (header["kind"], header["ref"], header["task"], header["session"]) == (
            "operation.params",
            op.id,
            TASK,
            sid,
        )
        assert params == {"operation_id": op.id, "type": "task.set_title", "params": {"title": "Fix the login"}}
        # outbox: executing, the task.meta event, the result event, then the transition (result before transition)
        items = await ok_kit.outbox()
        kinds = [(kind, b.get("state") or b.get("header", {}).get("kind")) for kind, b in items]
        assert kinds == [
            ("operation_transition", "executing"),
            ("event", "task.meta"),
            ("event", "operation.result"),
            ("operation_transition", "succeeded"),
        ]
        meta, result, final = items[1][1], items[2][1], items[3][1]
        assert meta["header"]["session"] == ""
        assert meta["header"]["ref"] == ""
        assert meta["header"]["task"] == TASK
        assert meta["body"] == {"title": "Fix the login"}
        assert result["body"] == {"operation_id": op.id, "status": "succeeded", "result": {}}
        assert result["header"]["ref"] == op.id
        assert final["result_event_id"] == result["header"]["id"] == op.result_event_id
        # the pushed items pass the hub's contract checks and the device-signed events open
        pushed = await ok_kit.kit.pushed_events()
        assert [h["kind"] for h, _ in pushed] == ["task.meta", "operation.result"]
        assert ok_kit.notified == []

    run(rig, body)


def test_a_comment_registers_a_dedupe_tag_and_returns_the_executor_result(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls = await comment_kit(env)
        sid = await kit_.session()
        params = {"repo": "Example-Org/Example-Repo", "number": 7, "body": "Looks good."}
        op = await kit_.run(sid, "github.comment", params)
        assert op.state == "succeeded"
        assert op.result == {"comment_id": 11, "url": "https://github.com/example-org/example-repo/pull/7#c11"}
        (registration,) = kit_.hub.requests
        assert registration["dedupe"]["epoch"] == 1
        assert len(registration["dedupe"]["tag"]) == 43
        assert op.dedupe_tag is None or op.dedupe_tag == registration["dedupe"]["tag"]
        (ctx,) = calls
        assert (ctx.attempt, ctx.params, ctx.data, ctx.session.id) == (1, params, {}, sid)
        # the same comment again gives the same tag (normalised repository)
        await kit_.run(sid, "github.comment", {**params, "repo": REPO})
        assert kit_.hub.requests[1]["dedupe"] == registration["dedupe"]

    run(rig, body)


def test_formal_check_failures_are_registered_as_rejected_with_their_code(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls = await comment_kit(env)
        sid = await kit_.session()
        kit_.hub.decisions.append({"state": "rejected", "error_code": "repo_not_allowed"})
        op = await kit_.run(sid, "github.comment", {"repo": "other/repo", "number": 7, "body": "x"})
        assert (op.state, op.error_code) == ("rejected", "repo_not_allowed")
        (registration,) = kit_.hub.requests
        assert registration["local_check"] == "rejected"
        assert registration["error_code"] == "repo_not_allowed"
        assert registration["dedupe"] is None
        assert op.answer == {
            "operation_id": op.id,
            "status": "rejected",
            "error": {"code": "repo_not_allowed", "message": "The repository is not allowed for this workgroup."},
        }
        assert kit_.answers() == [op.answer]
        assert calls == []
        # the error result as an event (03 §9 "every operation has a result"); no transition: the hub decided
        ((kind, event),) = await kit_.outbox()
        assert kind == "event"
        assert event["body"] == {"operation_id": op.id, "status": "rejected", "error": op.answer["error"]}
        assert op.result_event_id == event["header"]["id"]
        await kit_.kit.pushed_events()

    run(rig, body)


def test_params_invalid_answers_carry_the_pointers(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        kit_.hub.decisions.append({"state": "rejected", "error_code": "params_invalid"})
        op = await kit_.run(sid, "github.comment", {"repo": REPO, "number": True, "body": "x"})
        assert op.answer is not None
        assert op.answer["error"]["pointers"] == ["/params/number"]
        assert op.checked == {"error": op.answer["error"]}

    run(rig, body)


def test_a_guest_rejection_is_binding_and_not_answered_again(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls = await comment_kit(env)
        sid = await kit_.session()
        kit_.hub.decisions.append({"state": "rejected", "error_code": "unknown_type"})
        error = {"code": "unknown_type", "message": "The operation type is not known."}
        op = await kit_.run(sid, "Run Tests; rm -rf /", {"cmd": "rm -rf /"}, rejected=error)
        assert (op.state, op.error_code) == ("rejected", "unknown_type")
        (registration,) = kit_.hub.requests
        assert registration["type"] == "invalid.type"
        assert (registration["local_check"], registration["error_code"]) == ("rejected", "unknown_type")
        _, params = kit_.hub.params_event()
        assert params["type"] == "Run Tests; rm -rf /"
        assert params["params"] == {"cmd": "rm -rf /"}
        assert kit_.answers() == []
        assert op.answered_at is not None
        assert calls == []

    run(rig, body)


def test_a_guest_rejection_of_a_type_that_passes_the_runner_stays_rejected(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls = await comment_kit(env)
        sid = await kit_.session()
        kit_.hub.decisions.append({"state": "rejected", "error_code": "params_invalid"})
        params = {"repo": REPO, "number": 7, "body": "fine"}
        op = await kit_.run(sid, "github.comment", params, rejected={"code": "params_invalid", "pointers": ["/params"]})
        assert op.state == "rejected"
        assert calls == []
        assert kit_.hub.requests[0]["error_code"] == "params_invalid"

    run(rig, body)


def test_a_known_type_without_an_executor_is_unknown_on_this_device(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        kit_.hub.decisions.append({"state": "rejected", "error_code": "unknown_type"})
        op = await kit_.run(sid, "github.get_pull_request", {"repo": REPO, "number": 1})
        assert op.state == "rejected"
        assert op.answer is not None
        assert op.answer["error"]["message"] == "The operation type is not enabled on this device."

    run(rig, body)


def test_denied_by_the_hub_is_answered_with_the_hub_code(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls = await comment_kit(env)
        sid = await kit_.session()
        kit_.hub.decisions.append({"state": "denied", "error_code": "rights_denied"})
        op = await kit_.run(sid, "task.set_title", {"title": "x"})
        assert (op.state, op.error_code) == ("denied", "rights_denied")
        assert op.answer == {"operation_id": op.id, "status": "denied", "error": {"code": "rights_denied"}}
        assert calls == []

    run(rig, body)


def test_a_hub_rejection_of_a_passed_request_uses_the_hub_code(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        kit_.hub.decisions.append({"state": "rejected", "error_code": "limit_exceeded"})
        op = await kit_.run(sid, "task.set_title", {"title": "x"})
        assert op.answer == {"operation_id": op.id, "status": "rejected", "error": {"code": "limit_exceeded"}}

    run(rig, body)


def test_retryable_failures_are_retried_with_2_8_30_seconds_then_fail_retryable(rig: Rig) -> None:
    async def body(env: Any) -> None:
        attempts: list[int] = []

        async def flaky(ctx: OperationContext) -> dict[str, Any]:
            attempts.append(ctx.attempt)
            raise OperationFailed("upstream_error", retryable=True, message="GitHub answered 502.")

        kit_, _ = await comment_kit(env, executors={"github.comment": flaky})
        sid = await kit_.session()
        start = len(env.clock.sleeps)
        op = await kit_.run(sid, "github.comment", {"repo": REPO, "number": 7, "body": "x"})
        assert attempts == [1, 2, 3, 4]
        assert env.clock.sleeps[start:] == [2.0, 8.0, 30.0]
        assert (op.state, op.error_code, op.retryable, op.attempt) == ("failed", "upstream_error", True, 4)
        error = {"code": "upstream_error", "retryable": True, "message": "GitHub answered 502."}
        assert op.answer == {"operation_id": op.id, "status": "failed", "error": error}
        items = await kit_.outbox()
        assert [(k, b.get("state")) for k, b in items if k == "operation_transition"] == [
            ("operation_transition", "executing"),
            ("operation_transition", "failed"),
        ]
        final = items[-1][1]
        assert (final["error_code"], final["retryable"]) == ("upstream_error", True)
        assert items[-2][1]["body"] == {"operation_id": op.id, "status": "failed", "error": error}
        await kit_.kit.pushed_events()  # contract-valid

    run(rig, body)


def test_a_retry_succeeds_and_honours_retry_after(rig: Rig) -> None:
    async def body(env: Any) -> None:
        failures = [OperationFailed("upstream_error", retryable=True, retry_after=12.0)]

        async def once(ctx: OperationContext) -> dict[str, Any]:
            if failures:
                raise failures.pop(0)
            return {"comment_id": 1, "url": "https://github.com/x/y/pull/1#c1"}

        kit_, _ = await comment_kit(env, executors={"github.comment": once})
        sid = await kit_.session()
        start = len(env.clock.sleeps)
        op = await kit_.run(sid, "github.comment", {"repo": REPO, "number": 7, "body": "x"})
        assert env.clock.sleeps[start:] == [12.0]
        assert (op.state, op.attempt) == ("succeeded", 2)

    run(rig, body)


def test_a_retry_after_beyond_a_minute_ends_the_retries(rig: Rig) -> None:
    async def body(env: Any) -> None:
        async def limited(ctx: OperationContext) -> dict[str, Any]:
            raise OperationFailed("upstream_error", retryable=True, retry_after=3600.0)

        kit_, _ = await comment_kit(env, executors={"github.comment": limited})
        sid = await kit_.session()
        op = await kit_.run(sid, "github.comment", {"repo": REPO, "number": 7, "body": "x"})
        assert (op.state, op.retryable, op.attempt) == ("failed", True, 1)

    run(rig, body)


def test_non_retryable_failures_and_crashing_executors_end_at_once(rig: Rig) -> None:
    async def body(env: Any) -> None:
        async def exists(ctx: OperationContext) -> dict[str, Any]:
            raise OperationFailed("branch_exists")

        async def crash(ctx: OperationContext) -> dict[str, Any]:
            raise RuntimeError("bug")

        async def wrong(ctx: OperationContext) -> Any:
            return ["not", "an", "object"]

        executors = {"github.create_branch": exists, "github.comment": crash, "task.set_title": wrong}
        kit_, _ = await comment_kit(env, executors=executors)
        sid = await kit_.session()
        op = await kit_.run(sid, "github.create_branch", {"repo": REPO, "branch": "feature/x", "from": "main"})
        assert (op.state, op.error_code, op.retryable, op.attempt) == ("failed", "branch_exists", False, 1)
        op = await kit_.run(sid, "github.comment", {"repo": REPO, "number": 7, "body": "x"})
        assert (op.state, op.error_code, op.retryable) == ("failed", "upstream_error", False)
        assert any("executor_failed" in line and "RuntimeError" in line for line in kit_.lines)
        op = await kit_.run(sid, "task.set_title", {"title": "x"})
        assert (op.state, op.error_code) == ("failed", "upstream_error")

    run(rig, body)


def test_an_existing_pull_request_succeeds_and_is_answered_as_duplicate(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pr = {"number": 5, "url": "https://github.com/example-org/example-repo/pull/5", "id": 99, "draft": True}

        async def existing(ctx: OperationContext) -> dict[str, Any]:
            assert ctx.params["draft"] is True  # the settings' default
            return {**pr, "existing": True}

        kit_, _ = await comment_kit(env, executors={"github.create_pull_request": existing})
        sid = await kit_.session()
        params = {"repo": REPO, "head": "refs/heads/feature/x", "base": "main", "title": "Fix"}
        op = await kit_.run(sid, "github.create_pull_request", params)
        assert op.state == "succeeded"
        assert op.answer == {"operation_id": op.id, "status": "duplicate", "result": {**pr, "existing": True}}
        assert op.checked is not None
        assert op.checked["params"]["head"] == "feature/x"

    run(rig, body)


def test_a_result_too_large_for_an_event_fails_size_exceeded(rig: Rig) -> None:
    async def body(env: Any) -> None:
        async def huge(ctx: OperationContext) -> dict[str, Any]:
            return {"blob": "x" * 300_000}

        kit_, _ = await comment_kit(env, executors={"task.set_title": huge})
        sid = await kit_.session()
        op = await kit_.run(sid, "task.set_title", {"title": "x"})
        assert (op.state, op.error_code) == ("failed", "size_exceeded")

    run(rig, body)


def test_parameters_too_large_for_the_params_event_are_left_out_of_it(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        kit_.hub.decisions.append({"state": "rejected", "error_code": "params_invalid"})
        op = await kit_.run(sid, "github.comment", {"repo": REPO, "number": 7, "body": "x" * 300_000})
        # the body is too long (params_invalid) and too large for the event: the code stays, the params are left out
        registration = kit_.hub.requests[0]
        assert (registration["local_check"], registration["error_code"]) == ("rejected", "params_invalid")
        _, params = kit_.hub.params_event()
        assert params["params"] == {}
        assert op.state == "rejected"

    run(rig, body)


def test_a_huge_but_valid_request_is_rejected_size_exceeded(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls = await comment_kit(env)
        sid = await kit_.session()
        kit_.hub.decisions.append({"state": "rejected", "error_code": "size_exceeded"})
        op = await kit_.run(sid, "github.comment", {"repo": REPO, "number": 7, "body": "\U0001f600" * 65_536})
        assert kit_.hub.requests[0]["error_code"] == "size_exceeded"
        assert op.answer is not None
        assert op.answer["error"]["code"] == "size_exceeded"
        assert op.checked == {"error": op.answer["error"]}
        assert calls == []

    run(rig, body)


def test_prechecks_add_dedupe_fields_and_data_for_the_executor(rig: Rig) -> None:
    async def body(env: Any) -> None:
        seen: list[PrecheckRequest] = []

        async def resolve(request: PrecheckRequest) -> PrecheckResult:
            seen.append(request)
            return PrecheckResult(dedupe={"ref": SHA}, data={"sha": SHA})

        async def checkout(ctx: OperationContext) -> dict[str, Any]:
            assert ctx.data == {"sha": SHA}
            return {"repo": REPO, "commit_sha": SHA, "dir": "repos/example-org/example-repo", "files": 1, "bytes": 2}

        kit_, _ = await comment_kit(
            env, executors={"github.checkout": checkout}, prechecks={"github.checkout": resolve}
        )
        sid = await kit_.session()
        op = await kit_.run(sid, "github.checkout", {"repo": REPO})
        assert op.state == "succeeded"
        (request,) = seen
        assert (request.session.id, request.params, request.operation.id) == (sid, {"repo": REPO}, op.id)
        assert REPO in request.settings.repositories
        assert op.checked == {"params": {"repo": REPO}, "dedupe": {"ref": SHA}, "data": {"sha": SHA}}
        assert kit_.hub.requests[0]["dedupe"] is not None

    run(rig, body)


def test_precheck_failures_and_missing_prechecks(rig: Rig) -> None:
    async def body(env: Any) -> None:
        async def inaccessible(request: PrecheckRequest) -> PrecheckResult:
            raise CheckFailed("repo_not_accessible", message="The repository is not accessible.")

        async def broken(request: PrecheckRequest) -> PrecheckResult:
            raise TimeoutError

        async def incomplete(request: PrecheckRequest) -> PrecheckResult:
            return PrecheckResult(dedupe={"parent_sha": SHA})

        kit_, _ = await comment_kit(
            env,
            executors={"github.checkout": ok, "github.comment": ok, "github.commit": ok, "github.create_branch": ok},
            prechecks={"github.comment": inaccessible, "github.create_branch": broken, "github.commit": incomplete},
        )
        sid = await kit_.session()
        for code in ("repo_not_accessible", "upstream_error", "unknown_type", "unknown_type"):
            kit_.hub.decisions.append({"state": "rejected", "error_code": code})
        op = await kit_.run(sid, "github.comment", {"repo": REPO, "number": 7, "body": "x"})
        assert kit_.hub.requests[-1]["error_code"] == "repo_not_accessible"
        assert op.state == "rejected"
        await kit_.run(sid, "github.create_branch", {"repo": REPO, "branch": "feature/y", "from": "main"})
        assert kit_.hub.requests[-1]["error_code"] == "upstream_error"
        assert any("precheck_failed" in line and "TimeoutError" in line for line in kit_.lines)
        # github.checkout needs a pre-check that resolves the ref; github.commit one with both SHAs
        await kit_.run(sid, "github.checkout", {"repo": REPO})
        assert kit_.hub.requests[-1]["error_code"] == "unknown_type"
        await kit_.run(sid, "github.commit", {"repo": REPO, "branch": "feature/y", "message": "m"})
        assert kit_.hub.requests[-1]["error_code"] == "unknown_type"

    run(rig, body)


def test_push_needs_a_commit_operation_of_the_task(rig: Rig) -> None:
    async def body(env: Any) -> None:
        async def commit(ctx: OperationContext) -> dict[str, Any]:
            return {"commit_sha": SHA, "parent_sha": "d" * 40, "tree_sha": "e" * 40, "files_changed": 1}

        async def snapshot(request: PrecheckRequest) -> PrecheckResult:
            return PrecheckResult(dedupe={"parent_sha": "d" * 40, "tree_sha": "e" * 40})

        async def push(ctx: OperationContext) -> dict[str, Any]:
            return {"branch": "feature/x", "sha": SHA, "url": "https://github.com/x/y/tree/feature/x"}

        kit_, _ = await comment_kit(
            env, executors={"github.commit": commit, "github.push": push}, prechecks={"github.commit": snapshot}
        )
        sid = await kit_.session()
        kit_.hub.decisions.append({"state": "rejected", "error_code": "not_found"})
        op = await kit_.run(sid, "github.push", {"repo": REPO, "branch": "feature/x", "commit_sha": SHA})
        assert op.state == "rejected"
        op = await kit_.run(sid, "github.commit", {"repo": REPO, "branch": "feature/x", "message": "m"})
        assert op.state == "succeeded"
        op = await kit_.run(sid, "github.push", {"repo": REPO, "branch": "feature/x", "commit_sha": SHA})
        assert op.state == "succeeded"

    run(rig, body)


def test_idempotency_keys_replace_the_dedupe_fields(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        await kit_.run(sid, "task.set_title", {"title": "a"}, key="client-1")
        await kit_.run(sid, "task.set_title", {"title": "b"}, key="client-1")
        first, second = kit_.hub.requests
        assert first["dedupe"] is not None
        assert first["dedupe"] == second["dedupe"]
        _, params = kit_.hub.params_event(0)
        assert params["idempotency_key"] == "client-1"

    run(rig, body)


def test_subagent_requests_keep_the_child_session_of_the_hub(rig: Rig) -> None:
    async def body(env: Any) -> None:
        child = "01930005-0000-7000-8000-000000000042"

        async def start(ctx: OperationContext) -> dict[str, Any]:
            assert ctx.operation.child_session_id == child
            return {"session_id": child}

        kit_, _ = await comment_kit(env, executors={"subagent.request": start})
        sid = await kit_.session()
        kit_.hub.decisions.append({"state": "accepted", "child_session_id": child})
        op = await kit_.run(sid, "subagent.request", {"purpose": "Review", "prompt": "Review PR 7."})
        assert (op.state, op.child_session_id, op.result) == ("succeeded", child, {"session_id": child})
        assert kit_.hub.requests[0]["dedupe"] is not None

    run(rig, body)


def test_the_executor_sees_a_missing_session_and_registry_changes(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"})
        await kit_.pipeline.process(op_id)  # succeeded with `ok`
        # an accepted operation whose executor vanished (registry changed across a restart)
        other = Pipeline(
            kit_.kit.session_env,
            client=env.sync.client,
            sealer=env.sync.pusher.sealer,
            executors={},
            sleep=env.clock.sleep,
        )
        op_id = await kit_.request(sid, "task.set_title", {"title": "y"})
        await kit_.pipeline.process(op_id)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.operation SET state = 'accepted', answer = NULL WHERE id = %s", (op_id,))
        await other.process(op_id)
        op = await kit_.op(op_id)
        assert (op.state, op.error_code) == ("failed", "unknown_type")
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.operation SET state = 'accepted', answer = NULL WHERE id = %s", (op_id,))
            await conn.execute("DELETE FROM runner.session WHERE id = %s", (sid,))
        await kit_.pipeline.process(op_id)
        op = await kit_.op(op_id)
        assert (op.state, op.error_code) == ("failed", "not_found")

    run(rig, body)


def test_a_request_of_an_unknown_session_is_rejected_not_found(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"})
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.session WHERE id = %s", (sid,))
        kit_.hub.decisions.append({"state": "rejected", "error_code": "not_found"})
        await kit_.pipeline.process(op_id)
        op = await kit_.op(op_id)
        assert kit_.hub.requests[0]["error_code"] == "not_found"
        assert op.state == "rejected"

    run(rig, body)


def test_unknown_operations_are_ignored(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        await kit_.pipeline.process("01930009-0000-7000-8000-000000000001")
        assert kit_.hub.requests == []

    run(rig, body)


def test_answers_are_retried_and_a_refusal_ends_them(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        kit_.guest.answer_errors[:] = [GuestUnreachable("pending"), GuestUnreachable("pending")]
        start = len(env.clock.sleeps)
        op = await kit_.run(sid, "task.set_title", {"title": "x"})
        assert len(kit_.guest.answers()) == 3
        assert op.answered_at is not None
        assert env.clock.sleeps[start:][-2:] == [1.0, 2.0]
        # three failures: the answer stays open for the next scan
        kit_.guest.answer_errors[:] = [GuestUnreachable("pending")] * 3
        op = await kit_.run(sid, "task.set_title", {"title": "y"})
        assert op.answered_at is None
        assert any("answer_failed" in line for line in kit_.lines)
        # a refusal (unknown or already answered pending call) is final
        kit_.guest.answer_errors[:] = [GuestRefused("pending", 404, "not_found")]
        await kit_.pipeline.process(op.id)
        op = await kit_.op(op.id)
        assert op.answered_at is not None
        assert any("answer_refused" in line and "not_found" in line for line in kit_.lines)

    run(rig, body)


def test_an_answer_for_a_vm_that_is_gone_is_dropped(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"})
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.operation SET vm_id = 'vm-gone' WHERE id = %s", (op_id,))
        await kit_.pipeline.process(op_id)
        op = await kit_.op(op_id)
        assert op.answered_at is not None
        assert kit_.guest.answers() == []
        assert any("answer_unroutable" in line for line in kit_.lines)

    run(rig, body)


def test_a_late_final_answer_pings_operation_finished(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"})
        await kit_.age(op_id, 25)  # the MCP call already returned `pending`
        await kit_.pipeline.process(op_id)
        assert kit_.notified == [op_id]
        assert len(kit_.guest.answers()) == 1  # the late answer is still given (get_operation reads it)

    run(rig, body)


def test_without_a_notifier_the_finished_operation_is_logged_and_notifier_errors_are_contained(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env, notify=False)
        sid = await kit_.session()
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"})
        await kit_.age(op_id, 25)
        await kit_.pipeline.process(op_id)
        assert any("finished_unrouted" in line for line in kit_.lines)

        async def failing(_context: Any, _op: Any) -> None:
            raise RuntimeError("ping")

        kit_.pipeline.notify = failing
        op_id = await kit_.request(sid, "task.set_title", {"title": "y"})
        await kit_.age(op_id, 25)
        await kit_.pipeline.process(op_id)
        assert any("notify_failed" in line for line in kit_.lines)
        assert (await kit_.op(op_id)).state == "succeeded"

    run(rig, body)


def test_an_operation_interrupted_while_executing_fails_timeout_and_is_never_run_again(rig: Rig) -> None:
    async def body(env: Any) -> None:
        runs: list[str] = []

        async def count(ctx: OperationContext) -> dict[str, Any]:
            runs.append(ctx.operation.id)
            return {}

        kit_, _ = await comment_kit(env, executors={"task.set_title": count})
        sid = await kit_.session()
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"})
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.operation SET state = 'executing' WHERE id = %s", (op_id,))
        await kit_.pipeline.process(op_id)
        op = await kit_.op(op_id)
        assert (op.state, op.error_code, op.retryable) == ("failed", "timeout", True)
        assert runs == []
        assert [b["state"] for k, b in await kit_.outbox() if k == "operation_transition"] == ["failed"]

    run(rig, body)


def test_wg_scope_of_the_result_event(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await comment_kit(env)
        sid = await kit_.session()
        op = await kit_.run(sid, "task.set_title", {"title": "x"})
        events = [b for k, b in await kit_.outbox() if k == "event" and b["header"]["kind"] == "operation.result"]
        (event,) = events
        header = event["header"]
        assert (header["scope"], header["scope_id"], header["wg"], header["session"]) == ("wg", WG, WG, sid)
        assert header["ref"] == op.id

    run(rig, body)
