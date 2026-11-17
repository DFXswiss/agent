from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_core.db import dumps
from tests.conftest import pair_device, sign_in


ORIGIN = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa50"


def _insert_replica(
    hub: TestClient,
    *,
    table: str,
    row_id: str,
    updated_at: str,
    payload: dict,
    login: str = "alice",
    origin: str = ORIGIN,
) -> None:
    hub.app.state.hub.store.execute(
        "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (table, row_id, origin, login, dumps(payload), updated_at),
    )


def test_api_state_lists_newest_first(hub: TestClient) -> None:
    sign_in(hub, "code-alice")
    _insert_replica(
        hub,
        table="session",
        row_id="sess-old",
        updated_at="2026-08-21T10:00:00Z",
        payload={"id": "sess-old", "status": "active", "last_seen_at": "2026-08-21T10:00:00Z"},
    )
    _insert_replica(
        hub,
        table="session",
        row_id="sess-new",
        updated_at="2026-08-22T21:00:00Z",
        payload={"id": "sess-new", "status": "active", "last_seen_at": "2026-08-22T21:00:00Z"},
    )
    _insert_replica(
        hub,
        table="task",
        row_id="task-old",
        updated_at="2026-08-20T08:00:00Z",
        payload={"id": "task-old", "title": "older", "state": "open"},
    )
    _insert_replica(
        hub,
        table="task",
        row_id="task-new",
        updated_at="2026-08-22T09:00:00Z",
        payload={"id": "task-new", "title": "newer", "state": "pr-review"},
    )
    _insert_replica(
        hub,
        table="ping",
        row_id="ping-old",
        updated_at="2026-08-22T01:00:00Z",
        payload={"id": "ping-old", "from_login": "bob", "to_login": "alice", "kind": "ping"},
    )
    _insert_replica(
        hub,
        table="ping",
        row_id="ping-new",
        updated_at="2026-08-22T02:00:00Z",
        payload={"id": "ping-new", "from_login": "bob", "to_login": "alice", "kind": "question"},
    )
    _insert_replica(
        hub,
        table="session",
        row_id="sess-tie-b",
        updated_at="2026-08-22T20:00:00Z",
        payload={"id": "sess-tie-b", "status": "active"},
    )
    _insert_replica(
        hub,
        table="session",
        row_id="sess-tie-a",
        updated_at="2026-08-22T20:00:00Z",
        payload={"id": "sess-tie-a", "status": "active"},
    )

    pair_device(hub, "code-alice", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa51", "old-box")
    pair_device(hub, "code-alice", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa52", "new-box")
    store = hub.app.state.hub.store
    store.execute(
        "UPDATE device SET created_at = ? WHERE id = ?",
        ("2026-08-01T00:00:00Z", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa51"),
    )
    store.execute(
        "UPDATE device SET created_at = ? WHERE id = ?",
        ("2026-08-22T00:00:00Z", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa52"),
    )

    state = hub.get("/api/state").json()
    assert [s["id"] for s in state["session"]] == ["sess-new", "sess-tie-b", "sess-tie-a", "sess-old"]
    assert [t["id"] for t in state["task"]] == ["task-new", "task-old"]
    assert [p["id"] for p in state["pings"]] == ["ping-new", "ping-old"]
    device_ids = [d["id"] for d in state["devices"]]
    assert device_ids.index("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa52") < device_ids.index(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa51"
    )


def test_api_assignments_lists_newest_first_with_row_id_tiebreak(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    for row_id, updated_at, number in (
        ("assignment-old", "2026-11-04T08:00:00Z", 1),
        ("assignment-tie-a", "2026-11-04T09:00:00Z", 2),
        ("assignment-tie-b", "2026-11-04T09:00:00Z", 3),
    ):
        _insert_replica(
            agent_hub,
            table="activity",
            row_id=row_id,
            updated_at=updated_at,
            login="alice",
            origin="web:alice",
            payload={
                "id": row_id,
                "type": "assignment.created",
                "payload": {
                    "agent": "helperbot",
                    "actor": "alice",
                    "repo": "Acme/Widget",
                    "number": number,
                    "title": row_id,
                    "url": f"https://github.com/Acme/Widget/issues/{number}",
                },
            },
        )

    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})
    assert response.status_code == 200
    assert [row["title"] for row in response.json()["assignments"]] == [
        "assignment-tie-b",
        "assignment-tie-a",
        "assignment-old",
    ]


def test_dashboard_keeps_api_order(hub: TestClient) -> None:
    html = (
        Path(__file__).resolve().parents[1] / "src" / "agent_core" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert "newest first" in html
    assert ".reverse(" not in html
    assert "fillSessions(data.session || [])" in html
    assert 'fill("tasks", data.task || []' in html
    assert 'fill("pings", data.pings || []' in html
    assert 'fill("devices", (data.devices || [])' in html
    assert "fillUsage(data.grok" in html
