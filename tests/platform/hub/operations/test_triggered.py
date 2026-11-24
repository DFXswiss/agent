"""U-HUB-operations (KOR-46, ADR-083 section 2): the child session of a `subagent.request` is triggered by the
operation's person (`triggered_by`, stored by KOR-47), and the attribution interface of KOR-45."""

from __future__ import annotations

from uuid import UUID

from agent_hub.sessions import attribution
from tests.platform.hub.operations.support import Ops
from tests.platform.hub.orgs.support import new_id

SUBAGENT = "subagent.request"


def test_the_child_session_is_triggered_by_the_same_person(ops: Ops) -> None:
    _task, session = ops.session()
    writer = ops.scene.writer.id
    body = ops.request(session, SUBAGENT)
    body["triggered_by"] = writer
    response = ops.register(body)
    assert response.status_code == 200, response.text
    child = response.json()["child_session_id"]
    found = ops.scene.reader.browser.get(f"/v1/sessions/{child}")
    assert found.status_code == 200, found.text
    assert found.json()["session"]["triggered_by"] == writer
    with ops.hub.pool.connection() as connection:
        assert attribution.session_trigger(connection, child) == UUID(writer)
        assert attribution.session_trigger(connection, session) is None
        assert attribution.session_trigger(connection, new_id()) is None
        assert attribution.operation_trigger(connection, body["operation_id"]) == UUID(writer)
        assert attribution.operation_trigger(connection, new_id()) is None
    _operation, answer = ops.decide(session, SUBAGENT)
    assert ops.desk.rows("SELECT triggered_by FROM hub.session WHERE id = %s", answer["child_session_id"]) == [(None,)]
    with ops.hub.pool.connection() as connection:
        assert attribution.operation_trigger(connection, _operation) is None
