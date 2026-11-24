"""U-HUB-contexts (pull-request links): the opaque tags of ADR-059 section 8."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from agent_hub.crypto import context
from agent_hub.crypto.encoding import format_time
from tests.platform.hub.agents.support import agent_client
from tests.platform.hub.contexts.support import NOT_CONTRACT, WK, Checkpoints, error
from tests.platform.hub.orgs.support import Actor, new_id

TAG = context.pr_link_tag(context.pr_link_key(WK), "example-org/example-repo", 17)
OTHER = context.pr_link_tag(context.pr_link_key(WK), "example-org/example-repo", 18)


def post(checkpoints: Checkpoints, session: str, device: Actor | None = None, **changes: Any) -> tuple[int, Any]:
    body = {"session_id": session, "epoch": 1, "tag": TAG, **changes}
    return error(checkpoints.client(device).post("/v1/device/pr-links", json=body))


def test_a_pull_request_leads_to_its_task(checkpoints: Checkpoints) -> None:
    hub, scene = checkpoints.outbox.hub, checkpoints.scene
    task, session = checkpoints.outbox.session()
    path = f"/v1/workgroups/{scene.wg}/pr-links/1/{TAG}"
    assert error(scene.writer.browser.get(path)) == (404, {"error": "not_found"})
    at = format_time(hub.clock())
    assert post(checkpoints, session) == (200, {"replayed": False})
    hub.clock.advance(5)
    assert post(checkpoints, session) == (200, {"replayed": True})
    found = {"task_id": task, "session_id": session, "created_at": at}
    for client in (scene.writer.browser, scene.reader.browser, agent_client(hub, UUID(scene.reader.id))):
        assert error(client.get(path)) == (200, found)
    for client in (scene.stranger.browser, agent_client(hub, UUID(scene.stranger.id))):
        assert client.get(path).status_code in (403, 404)
    # the first task keeps the tag
    _task, second = checkpoints.outbox.session()
    assert post(checkpoints, second) == (409, {"error": "id_conflict"})
    assert error(scene.writer.browser.get(path)) == (200, found)
    assert post(checkpoints, second, tag=OTHER) == (200, {"replayed": False})


def test_refused_registrations(checkpoints: Checkpoints) -> None:
    _task, session = checkpoints.outbox.session()
    assert post(checkpoints, new_id()) == (404, {"error": "not_found"})
    assert post(checkpoints, session, checkpoints.scene.other_device) == (403, {"error": "forbidden"})
    _task, outside = checkpoints.scene.start_session(wg=checkpoints.scene.draft.default_wg)
    assert post(checkpoints, outside) == (403, {"error": "forbidden"})
    assert post(checkpoints, session, epoch=2) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": ["/epoch"]}},
    )
    assert checkpoints.outbox.desk.rows("SELECT count(*) FROM hub.pr_link") == [(0,)]


@pytest.mark.parametrize(("epoch", "tag"), [("0", TAG), ("2147483648", TAG), ("x", TAG), ("1", "short"), ("2", TAG)])
def test_a_malformed_or_unknown_path_names_nothing(checkpoints: Checkpoints, epoch: str, tag: str) -> None:
    _task, session = checkpoints.outbox.session()
    assert post(checkpoints, session) == (200, {"replayed": False})
    path = f"/v1/workgroups/{checkpoints.scene.wg}/pr-links/{epoch}/{tag}"
    found = checkpoints.scene.writer.browser.get(path, extensions=NOT_CONTRACT)
    assert error(found) == (404, {"error": "not_found"})
