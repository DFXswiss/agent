"""U-HUB-stream (ADR-063 section 3): the browser live channel is a read. Opening `GET /v1/stream` records
`read.stream` of the organization and `read.stream.terminal` of every watched session before the first frame; a
stream whose terminal rows cannot be recorded is refused and forgotten."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.audit.chain import AuditRow
from tests.platform.hub.conftest import SigningKey
from tests.platform.hub.events.support import Scene
from tests.platform.hub.stream.support import LiveHub, Refused


def reads(scene: Scene) -> list[tuple[Any, ...]]:
    return scene.rows(
        "SELECT action, result, org_id::text, wg_id::text, task_id::text, session_id::text FROM hub.audit"
        " WHERE action LIKE 'read.stream%%' ORDER BY seq"
    )


def test_a_stream_records_the_organization_and_each_watched_terminal(scene: Scene, live: LiveHub) -> None:
    task, session = scene.start_session()
    stream = live.stream(cast(SigningKey, scene.writer.browser.key), org=scene.org, terminal=session)
    assert stream.frames(1)[0].event == "hello"
    # the route records the stream and its terminals before the stream starts
    assert reads(scene) == [
        ("read.stream", "ok", scene.org, None, None, None),
        ("read.stream.terminal", "ok", scene.org, scene.wg, task, session),
    ]


def test_a_refused_stream_is_recorded_as_refused(scene: Scene, live: LiveHub) -> None:
    stranger = scene.world.user()
    with pytest.raises(Refused) as refused:
        live.stream(cast(SigningKey, stranger.browser.key), org=scene.org)
    assert refused.value.status == 404
    # the stranger is no member: the refused read names no organization (placement of refusals, ADR-063)
    assert reads(scene) == [("read.stream", "denied", None, None, None, None)]


@pytest.mark.parametrize("failing", ["read.stream", "read.stream.terminal"])
def test_rows_that_cannot_be_recorded_refuse_the_stream(
    scene: Scene, live: LiveHub, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    _task, session = scene.start_session()
    log = live.app.state.hub.read_log
    original = log.admit

    def admit(row: AuditRow, query: str = "") -> None:
        if row.action == failing:
            raise RuntimeError("database gone")
        original(row, query)

    monkeypatch.setattr(log, "admit", admit)
    with pytest.raises(Refused) as refused:
        live.stream(cast(SigningKey, scene.writer.browser.key), org=scene.org, terminal=session)
    assert (refused.value.status, refused.value.body) == (503, {"error": "unavailable"})
    # the opened stream was forgotten: nothing is left to serve
    assert UUID(scene.org) not in live.service.by_org
