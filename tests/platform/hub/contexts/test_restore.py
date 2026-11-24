"""U-HUB-contexts (restore): what the device state offers a following main session on any device and what the hub keeps
of the reported outcome (ADR-072 sections 5 and 7, 03-api.md section 4.6.3)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import psycopg
import pytest

from agent_hub.contexts import outbox as outcomes
from agent_hub.crypto.encoding import format_time
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.contexts.support import NOT_CONTRACT, Checkpoints
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.tasks.support import audit_rows, frames


def sessions(checkpoints: Checkpoints, device: Actor) -> list[dict[str, Any]]:
    response = checkpoints.client(device).get("/v1/device/state")
    assert response.status_code == 200, response.text
    return list(response.json()["sessions"])


def test_a_follow_up_on_another_device_names_the_previous_session_and_its_checkpoint(checkpoints: Checkpoints) -> None:
    desk, first_device = checkpoints.outbox.desk, checkpoints.outbox.device
    other = checkpoints.scene.other_device
    desk.online(first_device)
    desk.offline(other)
    task, first = desk.started()
    (item,) = sessions(checkpoints, first_device)
    assert "context" not in item
    assert "resume_from" not in item
    desk.walk(first, "vm_assigning", "prompt_delivering", "working")
    saved_at = format_time(desk.hub.clock())
    saved = checkpoints.saved(task, first)
    context = {
        "object_id": saved.object_id,
        "session_id": first,
        "created_at": saved_at,
        "size": saved.manifest["size"],
    }
    # an unfinished upload and another task's checkpoint are never offered
    desk.hub.clock.advance(1)
    unfinished = checkpoints.seal(task, first)
    checkpoints.put(unfinished.object_id, 0, first, unfinished.chunks[0])
    foreign_task, foreign = checkpoints.outbox.session()
    checkpoints.saved(foreign_task, foreign)
    found = {item["session_id"]: item for item in sessions(checkpoints, first_device)}
    assert found[first]["context"] == context
    # the task is closed, its device gone: the next prompt continues the same conversation on the other device
    assert desk.scene.writer.browser.post(f"/v1/tasks/{task}/close", json={}).status_code == 200
    desk.transition(first, "stopped")
    desk.offline(first_device)
    desk.online(other)
    desk.hub.clock.advance(8 * 24 * 3600)
    desk.online(other)
    writer = desk.scene.writer
    browser = desk.hub.browser(UUID(writer.id))  # a new login after eight days
    followed = browser.post(f"/v1/tasks/{task}/prompts", json={"event": desk.prompt_event(writer, task)})
    assert followed.status_code == 200, followed.text
    second = followed.json()["session_id"]
    (item,) = sessions(checkpoints, other)
    assert (item["session_id"], item["resume_from"], item["context"]) == (second, first, context)
    # the second session ends before its own checkpoint arrived: the third resumes from it, and `context` still names
    # the first session's checkpoint - the runner sees that it is not the one of `resume_from` (review 1 decision 1)
    desk.transition(second, "stopped", device=other)
    desk.hub.clock.advance(1)
    third = browser.post(f"/v1/tasks/{task}/prompts", json={"event": desk.prompt_event(writer, task)})
    assert third.status_code == 200, third.text
    (item,) = sessions(checkpoints, other)
    assert (item["session_id"], item["resume_from"], item["context"]) == (third.json()["session_id"], second, context)


def report(checkpoints: Checkpoints, seq: int, session: str | None, action: str, detail: dict[str, Any]) -> Any:
    extra = {} if session is None else {"session_id": session}
    return checkpoints.outbox.audit(seq, action, detail, **extra)


def test_the_restore_outcome_reaches_the_session_row_the_frame_and_the_metric(
    checkpoints: Checkpoints, published: MemoryPublisher, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agent_hub.contexts")
    outbox, desk = checkpoints.outbox, checkpoints.outbox.desk
    _task, session = outbox.session()
    row = desk.session(session)
    assert (row["context_restore"], row["context_reason"]) == (None, None)
    before = dict(outcomes.COUNTS)
    fallback = report(checkpoints, 1, session, "session.context_fallback", {"reason": "context_invalid"})
    assert outbox.statuses(fallback) == [(1, "ok", None)]
    row = desk.session(session)
    assert (row["context_restore"], row["context_reason"]) == ("fallback", "context_invalid")
    frame = frames(published, "session")[-1]["session"]
    assert (frame["context_restore"], frame["context_reason"]) == ("fallback", "context_invalid")
    # a replay of the item is not counted again
    assert outbox.statuses(fallback) == [(1, "replayed", None)]
    # a restore: the workspace decides the reason the session row names (review 1 decision 5)
    object_id = new_id()
    reported = []
    for seq, (workspace, stale, reason) in enumerate(
        (
            ("full", False, None),
            ("partial", True, "workspace_partial"),
            ("omitted", False, "workspace_omitted"),
            ("empty", True, None),
        ),
        start=2,
    ):
        detail = {"object_id": object_id, "workspace": workspace, "stale": stale}
        reported.append(detail)
        restored = report(checkpoints, seq, session, "session.context_restored", detail)
        assert outbox.statuses(restored) == [(seq, "ok", None)]
        row = desk.session(session)  # GET /v1/sessions/{id}
        assert (row["context_restore"], row["context_reason"]) == ("restored", reason)
        frame = frames(published, "session")[-1]["session"]
        assert (frame["context_restore"], frame["context_reason"]) == ("restored", reason)
    assert [item[3] for item in audit_rows(desk, "session.context_fallback")] == [{"reason": "context_invalid"}]
    assert [item[3] for item in audit_rows(desk, "session.context_restored")] == reported
    metric = "metric agent_hub_session_context_total result="
    assert [line for line in caplog.messages if line.startswith(metric)] == [
        metric + "fallback reason=context_invalid",
        metric + "restored reason=none",
        metric + "restored reason=workspace_partial",
        metric + "restored reason=workspace_omitted",
        metric + "restored reason=none",
    ]
    for key, times in (
        (("fallback", "context_invalid"), 1),
        (("restored", "none"), 2),
        (("restored", "workspace_partial"), 1),
        (("restored", "workspace_omitted"), 1),
    ):
        assert outcomes.COUNTS[key] == before.get(key, 0) + times


@pytest.mark.parametrize(
    ("restore", "reason"),
    [
        ("fallback", None),
        ("fallback", "workspace_partial"),
        ("restored", "context_missing"),
        (None, "workspace_omitted"),
        (None, "context_invalid"),
        ("restored", "other"),
    ],
)
def test_the_session_row_refuses_a_reason_that_does_not_fit(
    checkpoints: Checkpoints, restore: str | None, reason: str | None
) -> None:
    desk = checkpoints.outbox.desk
    _task, session = checkpoints.outbox.session()
    with pytest.raises(psycopg.errors.CheckViolation):
        desk.execute(
            "UPDATE hub.session SET context_restore = %s, context_reason = %s WHERE id = %s", restore, reason, session
        )


def test_refused_outcomes_change_nothing(checkpoints: Checkpoints, published: MemoryPublisher) -> None:
    outbox, desk = checkpoints.outbox, checkpoints.outbox.desk
    _task, session = outbox.session()
    _task, foreign = outbox.session(checkpoints.scene.other_device)
    detail = {"reason": "context_missing"}
    response = outbox.push(
        report(checkpoints, 1, None, "session.context_fallback", detail),
        report(checkpoints, 2, foreign, "session.context_fallback", detail),
        report(checkpoints, 3, new_id(), "session.context_fallback", detail),
        report(checkpoints, 4, session, "session.context_fallback", {"reason": "other"}),
        report(checkpoints, 5, session, "runner.alert", {"alert": "vm_budget_exceeded", "value": 2}),
        extensions=NOT_CONTRACT,
    )
    assert [(found["status"], found.get("error")) for found in response.json()["results"]] == [
        ("rejected", "request_invalid"),
        ("rejected", "forbidden"),
        ("rejected", "not_found"),
        ("rejected", "request_invalid"),
        ("ok", None),
    ]
    for found in (session, foreign):
        row = desk.session(found)
        assert (row["context_restore"], row["context_reason"]) == (None, None)
    assert audit_rows(desk, "session.context_fallback") == []
    assert frames(published, "session") == []
