from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_core.db import dumps
from tests.conftest import sign_in
from tests.test_newest_first import _insert_replica


def test_usage_and_timeline_apis_require_a_session(
    agent_hub: TestClient,
) -> None:
    for path in (
        "/api/usage",
        "/api/session-timeline?id=missing",
    ):
        response = agent_hub.get(path)
        assert response.status_code == 401
        assert response.json() == {"detail": "not signed in"}


def test_session_page_direct_entry_without_cookie_is_static_html(
    agent_hub: TestClient,
) -> None:
    response = agent_hub.get("/session?id=missing")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "session-timeline" in response.text
    assert agent_hub.get("/api/session-timeline?id=missing").status_code == 401


def test_session_page_without_id_is_static_html(agent_hub: TestClient) -> None:
    response = agent_hub.get("/session")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Invalid session id." in response.text


def test_session_timeline_without_id_is_a_client_error(
    agent_hub: TestClient,
) -> None:
    response = agent_hub.get("/api/session-timeline")

    assert 400 <= response.status_code < 500


def test_removed_session_page_path_returns_404(agent_hub: TestClient) -> None:
    assert agent_hub.get("/sessions/abc").status_code == 404


def test_removed_session_timeline_path_returns_404(agent_hub: TestClient) -> None:
    assert agent_hub.get("/api/sessions/abc/timeline").status_code == 404


@pytest.mark.parametrize("session_id", ["..", "a/b", "x y"])
def test_timeline_query_route_preserves_path_unsafe_session_ids(
    agent_hub: TestClient, session_id: str
) -> None:
    sign_in(agent_hub, "code-alice")
    _insert_replica(
        agent_hub,
        table="session",
        row_id=session_id,
        updated_at="2026-11-05T10:00:00Z",
        login="helperbot",
        payload={"id": session_id, "created_at": "2026-11-05T10:00:00Z"},
    )

    response = agent_hub.get("/api/session-timeline", params={"id": session_id})

    assert response.status_code == 200, response.text
    assert response.json()["session_id"] == session_id
    assert response.json()["timeline"][0]["row_id"] == session_id
    assert agent_hub.get("/session", params={"id": session_id}).status_code == 200


def test_usage_aggregates_lanes_openrouter_and_keeps_grok_shape(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="lane-new",
        updated_at="2026-11-04T11:00:00Z",
        login="helperbot",
        payload={
            "id": "lane-new",
            "type": "lane.run",
            "session_id": "session-usage",
            "ran_at": "2026-11-04T11:00:00Z",
            "payload": {
                "vendor": "anthropic",
                "model": "model-a",
                "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 3},
            },
        },
    )
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="lane-old",
        updated_at="2026-11-04T10:00:00Z",
        login="helperbot",
        payload={
            "id": "lane-old",
            "type": "lane.run",
            "session_id": "session-usage",
            "ran_at": "2026-11-04T10:00:00Z",
            "payload": {"vendor": "anthropic", "model": "model-a", "usage": None},
        },
    )
    for row_id, fetched, used in (
        ("openrouter-new", "2026-11-04T12:00:00Z", 9.5),
        ("openrouter-old", "2026-11-03T12:00:00Z", 4.0),
    ):
        _insert_replica(
            agent_hub,
            table="activity",
            row_id=row_id,
            updated_at=fetched,
            login="helperbot",
            payload={
                "id": row_id,
                "type": "usage.snapshot",
                "payload": {
                    "vendor": "openrouter",
                    "account": "team-account",
                    "usage_usd": used,
                    "usage_daily_usd": 1.0,
                    "usage_weekly_usd": 5.0,
                    "usage_monthly_usd": used,
                    "limit_usd": 100.0,
                    "limit_remaining_usd": 90.5,
                    "fetched_at": fetched,
                },
            },
        )
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="grok-snapshot",
        updated_at="2026-11-04T09:00:00Z",
        login="helperbot",
        payload={
            "id": "grok-snapshot",
            "type": "usage.snapshot",
            "payload": {"provider": "grok", "account_email": "agent@example.test", "used_percent": 12},
        },
    )

    response = agent_hub.get("/api/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["lanes"] == [
        {
            "login": "helperbot",
            "session_id": "session-usage",
            "provider": "anthropic",
            "model": "model-a",
            "runs": 2,
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 0,
            "last_at": "2026-11-04T11:00:00Z",
        }
    ]
    assert len(body["accounts"]) == 1
    assert body["accounts"][0]["usage_usd"] == 9.5
    assert body["grok"][0]["id"] == "grok-snapshot"
    assert body["grok"][0]["payload"]["used_percent"] == 12
    assert body["grok"][0]["_github_login"] == "helperbot"


def test_usage_omits_all_sources_outside_a_self_only_viewer(agent_hub: TestClient) -> None:
    for row_id, payload in (
        (
            "hidden-lane",
            {
                "id": "hidden-lane",
                "type": "lane.run",
                "session_id": "hidden-session",
                "payload": {"vendor": "codex", "model": "hidden-model"},
            },
        ),
        (
            "hidden-openrouter",
            {
                "id": "hidden-openrouter",
                "type": "usage.snapshot",
                "payload": {"vendor": "openrouter", "account": "hidden-account"},
            },
        ),
        (
            "hidden-grok",
            {
                "id": "hidden-grok",
                "type": "usage.snapshot",
                "payload": {"vendor": "grok", "account_email": "hidden@example.test"},
            },
        ),
    ):
        _insert_replica(
            agent_hub,
            table="activity",
            row_id=row_id,
            updated_at="2026-11-05T10:00:00Z",
            login="alice",
            payload=payload,
        )

    sign_in(agent_hub, "code-dave")
    response = agent_hub.get("/api/usage")

    assert response.status_code == 200
    assert response.json() == {"lanes": [], "accounts": [], "grok": []}


def test_usage_arrays_are_newest_first_with_row_id_tiebreak(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    for suffix, updated_at in (
        ("old", "2026-11-04T08:00:00Z"),
        ("tie-a", "2026-11-04T09:00:00Z"),
        ("tie-b", "2026-11-04T09:00:00Z"),
    ):
        _insert_replica(
            agent_hub,
            table="activity",
            row_id=f"lane-{suffix}",
            updated_at=updated_at,
            login="helperbot",
            payload={
                "id": f"lane-{suffix}",
                "type": "lane.run",
                "session_id": f"session-{suffix}",
                "payload": {"vendor": "anthropic", "model": "model-a"},
            },
        )
        _insert_replica(
            agent_hub,
            table="activity",
            row_id=f"openrouter-{suffix}",
            updated_at=updated_at,
            login="helperbot",
            payload={
                "id": f"openrouter-{suffix}",
                "type": "usage.snapshot",
                "payload": {"vendor": "openrouter", "account": f"account-{suffix}"},
            },
        )
        _insert_replica(
            agent_hub,
            table="activity",
            row_id=f"grok-{suffix}",
            updated_at=updated_at,
            login="helperbot",
            payload={
                "id": f"grok-{suffix}",
                "type": "usage.snapshot",
                "payload": {"vendor": "grok", "account_email": f"{suffix}@example.test"},
            },
        )

    response = agent_hub.get("/api/usage")
    assert response.status_code == 200
    body = response.json()
    assert [row["session_id"] for row in body["lanes"]] == [
        "session-tie-b",
        "session-tie-a",
        "session-old",
    ]
    assert [row["account"] for row in body["accounts"]] == [
        "account-tie-b",
        "account-tie-a",
        "account-old",
    ]
    assert [row["id"] for row in body["grok"]] == [
        "grok-tie-b",
        "grok-tie-a",
        "grok-old",
    ]


def test_usage_group_tiebreak_uses_row_id_before_business_fields(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")
    updated_at = "2026-11-05T10:00:00Z"
    for row_id, session_id in (
        ("lane-z", "session-a"),
        ("lane-a", "session-z"),
    ):
        _insert_replica(
            agent_hub,
            table="activity",
            row_id=row_id,
            updated_at=updated_at,
            login="helperbot",
            payload={
                "id": row_id,
                "type": "lane.run",
                "session_id": session_id,
                "payload": {"vendor": "anthropic", "model": "model-a"},
            },
        )
    for row_id, account in (
        ("account-z", "account-a"),
        ("account-a", "account-z"),
    ):
        _insert_replica(
            agent_hub,
            table="activity",
            row_id=row_id,
            updated_at=updated_at,
            login="helperbot",
            payload={
                "id": row_id,
                "type": "usage.snapshot",
                "payload": {"vendor": "openrouter", "account": account},
            },
        )

    body = agent_hub.get("/api/usage").json()

    assert [row["session_id"] for row in body["lanes"]] == [
        "session-a",
        "session-z",
    ]
    assert [row["account"] for row in body["accounts"]] == [
        "account-a",
        "account-z",
    ]


def test_usage_order_uses_replica_change_not_payload_time(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    for suffix, updated_at, payload_at in (
        ("older-payload", "2026-11-05T12:00:00Z", "2026-11-04T08:00:00Z"),
        ("newer-payload", "2026-11-05T11:00:00Z", "2026-11-04T09:00:00Z"),
    ):
        _insert_replica(
            agent_hub,
            table="activity",
            row_id=f"lane-change-{suffix}",
            updated_at=updated_at,
            login="helperbot",
            payload={
                "id": f"lane-change-{suffix}",
                "type": "lane.run",
                "session_id": f"session-{suffix}",
                "ran_at": payload_at,
                "payload": {"vendor": "anthropic", "model": "model-a"},
            },
        )
        _insert_replica(
            agent_hub,
            table="activity",
            row_id=f"account-change-{suffix}",
            updated_at=updated_at,
            login="helperbot",
            payload={
                "id": f"account-change-{suffix}",
                "type": "usage.snapshot",
                "payload": {
                    "vendor": "openrouter",
                    "account": f"account-{suffix}",
                    "fetched_at": payload_at,
                },
            },
        )

    body = agent_hub.get("/api/usage").json()

    assert [row["session_id"] for row in body["lanes"]] == [
        "session-older-payload",
        "session-newer-payload",
    ]
    assert [row["account"] for row in body["accounts"]] == [
        "account-older-payload",
        "account-newer-payload",
    ]


def test_timeline_order_relations_and_visibility(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    _insert_replica(
        agent_hub,
        table="session",
        row_id="timeline-session",
        updated_at="2026-11-04T10:00:00Z",
        login="helperbot",
        payload={"id": "timeline-session", "created_at": "2026-11-04T10:00:00Z"},
    )
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="timeline-lane",
        updated_at="2026-11-04T10:01:00Z",
        login="helperbot",
        payload={
            "id": "timeline-lane",
            "type": "lane.run",
            "session_id": "timeline-session",
            "ran_at": "2026-11-04T10:01:00Z",
            "payload": {
                "role": "reviewer",
                "vendor": "codex",
                "model": "gpt-test",
                "status": "ok",
                "usage": {"input_tokens": 12, "output_tokens": 3},
                "tools": [
                    {"tool": "Read", "target": "src/agent_core/app.py", "error": False}
                ],
                "tools_truncated": True,
            },
        },
    )
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="timeline-assignment-created",
        updated_at="2026-11-04T10:01:30Z",
        login="alice",
        origin="web:alice",
        payload={
            "id": "timeline-assignment-created",
            "type": "assignment.created",
            "created_at": "2026-11-04T10:01:30Z",
            "payload": {
                "agent": "helperbot",
                "actor": "alice",
                "repo": "Acme/Widget",
                "number": 12,
                "url": "https://github.com/Acme/Widget/issues/12",
            },
        },
    )
    _insert_replica(
        agent_hub,
        table="task",
        row_id="timeline-task",
        updated_at="2026-11-04T10:02:00Z",
        login="helperbot",
        payload={"id": "", "session_id": "timeline-session", "started_at": "2026-11-04T10:02:00Z", "state": "working"},
    )
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="timeline-assignment-reply",
        updated_at="2026-11-04T10:02:30Z",
        login="alice",
        origin="web:alice",
        payload={
            "id": "timeline-assignment-reply",
            "type": "assignment.reply",
            "created_at": "2026-11-04T10:02:30Z",
            "payload": {
                "agent": "helperbot",
                "agents": ["helperbot"],
                "actor": "alice",
                "repo": "Acme/Widget",
                "number": 12,
                "url": "https://github.com/Acme/Widget/issues/12#issuecomment-1",
            },
        },
    )
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="timeline-other-assignment",
        updated_at="2026-11-04T10:02:45Z",
        login="alice",
        origin="web:alice",
        payload={
            "id": "timeline-other-assignment",
            "type": "assignment.created",
            "created_at": "2026-11-04T10:02:45Z",
            "payload": {
                "agent": "someone-else",
                "actor": "alice",
                "repo": "Acme/Other",
                "number": 1,
            },
        },
    )
    for row_id, origin, actor in (
        ("timeline-device-assignment", "device-alice", "alice"),
        ("timeline-actor-mismatch", "web:alice", "bob"),
    ):
        _insert_replica(
            agent_hub,
            table="activity",
            row_id=row_id,
            updated_at="2026-11-04T10:02:50Z",
            login="alice",
            origin=origin,
            payload={
                "id": row_id,
                "type": "assignment.created",
                "created_at": "2026-11-04T10:02:50Z",
                "session_id": "timeline-session",
                "payload": {"agent": "helperbot", "actor": actor},
            },
        )
    _insert_replica(
        agent_hub,
        table="local_check",
        row_id="timeline-check",
        updated_at="2026-11-04T10:03:00Z",
        login="helperbot",
        payload={"id": "timeline-check", "task_id": "timeline-task", "ran_at": "2026-11-04T10:03:00Z", "result": "passed"},
    )
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="empty-task-relation",
        updated_at="2026-11-04T10:04:00Z",
        login="helperbot",
        payload={"id": "empty-task-relation", "task_id": "", "created_at": "2026-11-04T10:04:00Z"},
    )
    _insert_replica(
        agent_hub,
        table="local_check",
        row_id="non-string-task-relation",
        updated_at="2026-11-04T10:05:00Z",
        login="helperbot",
        payload={"id": "non-string-task-relation", "task_id": 7, "ran_at": "2026-11-04T10:05:00Z"},
    )
    for table, relation in (
        ("activity", {"session_id": "timeline-session"}),
        ("task", {"session_id": "timeline-session"}),
        ("task_round", {"session_id": "timeline-session"}),
        ("agent", {"task_id": "timeline-task"}),
        ("checklist_item", {"task_id": "timeline-task"}),
        ("local_check", {"task_id": "timeline-task"}),
        ("review_gate", {"task_id": "timeline-task"}),
    ):
        row_id = f"teammate-{table}"
        _insert_replica(
            agent_hub,
            table=table,
            row_id=row_id,
            updated_at="2026-11-04T10:06:00Z",
            login="bob",
            payload={"id": row_id, **relation},
        )
    response = agent_hub.get(
        "/api/session-timeline", params={"id": "timeline-session"}
    )
    assert response.status_code == 200
    body = response.json()
    assert [row["row_id"] for row in body["timeline"]] == [
        "timeline-session",
        "timeline-lane",
        "timeline-assignment-created",
        "timeline-task",
        "timeline-assignment-reply",
        "timeline-check",
    ]
    assert [row["at"] for row in body["timeline"]] == sorted(row["at"] for row in body["timeline"])
    lane = next(row for row in body["timeline"] if row["row_id"] == "timeline-lane")
    assert lane["payload"]["payload"]["tools"] == [
        {"tool": "Read", "target": "src/agent_core/app.py", "error": False}
    ]
    assert lane["payload"]["payload"]["tools_truncated"] is True
    assert body["truncated"] is False
    assert agent_hub.get("/session?id=timeline-session").status_code == 200

    sign_in(agent_hub, "code-dave")
    assert agent_hub.get("/api/session-timeline?id=timeline-session").status_code == 404
    assert agent_hub.get("/session?id=timeline-session").status_code == 200


def test_timeline_equal_times_sort_by_row_id_then_table(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")
    _insert_replica(
        agent_hub,
        table="session",
        row_id="tie-session",
        updated_at="2026-11-05T09:00:00Z",
        login="helperbot",
        payload={"id": "tie-session", "created_at": "2026-11-05T09:00:00Z"},
    )
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="same-row",
        updated_at="2026-11-05T10:00:00Z",
        login="helperbot",
        payload={
            "id": "same-row",
            "session_id": "tie-session",
            "created_at": "2026-11-05T10:00:00Z",
        },
    )
    _insert_replica(
        agent_hub,
        table="task",
        row_id="same-row",
        updated_at="2026-11-05T10:00:00Z",
        login="helperbot",
        payload={
            "id": "same-row",
            "session_id": "tie-session",
            "created_at": "2026-11-05T10:00:00Z",
        },
    )

    timeline = agent_hub.get("/api/session-timeline?id=tie-session").json()[
        "timeline"
    ]

    assert [(row["table"], row["row_id"]) for row in timeline] == [
        ("session", "tie-session"),
        ("activity", "same-row"),
        ("task", "same-row"),
    ]


def test_timeline_truncates_at_5000(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    store = agent_hub.app.state.hub.store
    store.execute(
        "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "session",
            "long-session",
            "origin-long",
            "helperbot",
            dumps({"id": "long-session", "created_at": "2026-01-01T00:00:00Z"}),
            "2026-01-01T00:00:00Z",
        ),
    )
    for index in range(5001):
        stamp = f"2026-01-02T{index // 3600:02d}:{(index // 60) % 60:02d}:{index % 60:02d}Z"
        store.execute(
            "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "activity",
                f"long-activity-{index:04d}",
                "origin-long",
                "helperbot",
                dumps({"id": f"long-activity-{index:04d}", "session_id": "long-session", "created_at": stamp}),
                stamp,
            ),
        )
    body = agent_hub.get("/api/session-timeline?id=long-session").json()
    assert body["truncated"] is True
    assert len(body["timeline"]) == 5000
    assert body["timeline"][0]["row_id"] == "long-activity-0001"
    assert body["timeline"][-1]["row_id"] == "long-activity-5000"
    assert all(row["table"] != "session" for row in body["timeline"])


def test_session_page_renders_only_the_hub_tool_view() -> None:
    html = (
        Path(__file__).resolve().parents[1] / "src" / "agent_core" / "static" / "session.html"
    ).read_text(encoding="utf-8")

    assert "toolCalls(row.tools)" in html
    assert "tool_calls" not in html
    assert "Tool list truncated." in html


def test_timeline_preserves_the_tool_renderer_contract(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    _insert_replica(
        agent_hub,
        table="session",
        row_id="tool-contract-session",
        updated_at="2026-11-05T09:00:00Z",
        login="helperbot",
        payload={"id": "tool-contract-session", "created_at": "2026-11-05T09:00:00Z"},
    )
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="tool-contract-primary",
        updated_at="2026-11-05T09:01:00Z",
        login="helperbot",
        payload={
            "id": "tool-contract-primary",
            "type": "lane.run",
            "session_id": "tool-contract-session",
            "payload": {
                "tools": [{"tool": "Read", "target": "README.md", "error": "denied"}],
                "tool_calls": [{"name": "Legacy", "path": "ignored"}],
            },
        },
    )
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="tool-contract-truncated",
        updated_at="2026-11-05T09:02:00Z",
        login="helperbot",
        payload={
            "id": "tool-contract-truncated",
            "type": "lane.run",
            "session_id": "tool-contract-session",
            "payload": {
                "tools": [],
                "tool_calls": [{"name": "Legacy", "path": "ignored"}],
                "tools_truncated": True,
            },
        },
    )

    response = agent_hub.get(
        "/api/session-timeline?id=tool-contract-session"
    )
    assert response.status_code == 200
    views = {
        row["row_id"]: row["tools"]
        for row in response.json()["timeline"]
        if row["table"] == "activity"
    }
    # inner tools wins over legacy tool_calls
    assert views["tool-contract-primary"] == {
        "calls": [{"tool": "Read", "target": "README.md", "error": "denied"}],
        "truncated": False,
    }
    # an empty tools list still wins, and the truncation flag survives
    assert views["tool-contract-truncated"] == {"calls": [], "truncated": True}


def test_session_page_handles_ids_and_signed_out_timeline_fetches() -> None:
    html = (
        Path(__file__).resolve().parents[1] / "src" / "agent_core" / "static" / "session.html"
    ).read_text(encoding="utf-8")
    load_fn = html[html.index("async function load()") :]
    assert 'const sessionId = new URLSearchParams(location.search).get("id");' in load_fn
    assert "location.pathname" not in load_fn
    assert "decodeURIComponent(" not in html
    assert "encodeURIComponent(sessionId)" in html
    assert "if (response.status === 401)" in html
    assert 'link.href = "/"' in html
    assert 'notice.replaceChildren("Signed out — ", link)' in html


@pytest.mark.parametrize(
    ("inner", "expected"),
    [
        (
            {"tool_calls": [{"name": "Bash", "command": "ls", "status": "error"}]},
            {"calls": [{"tool": "Bash", "target": "ls", "error": "error"}], "truncated": False},
        ),
        (
            {"tool_calls": [{"name": "Edit", "path": "a.py", "status": "ok"}]},
            {"calls": [{"tool": "Edit", "target": "a.py", "error": ""}], "truncated": False},
        ),
        (
            {"tools": [{"tool": "Read", "target": "b.py", "error": True}, "junk", {}]},
            {
                "calls": [
                    {"tool": "Read", "target": "b.py", "error": "error"},
                    {"tool": "tool", "target": "", "error": ""},
                ],
                "truncated": False,
            },
        ),
        (
            {"tools": [{"tool": "Grep", "target": "x", "error": False}], "tools_truncated": "yes"},
            {"calls": [{"tool": "Grep", "target": "x", "error": ""}], "truncated": False},
        ),
        (
            {
                "tools": [{"tool": "Write", "target": "x", "status": "error"}],
                "tool_calls": [
                    {"name": "Legacy", "command": "ignored", "status": "error"}
                ],
            },
            {"calls": [{"tool": "Write", "target": "x", "error": ""}], "truncated": False},
        ),
        ({}, {"calls": [], "truncated": False}),
    ],
)
def test_timeline_tool_view_maps_fields_and_legacy_fallback(
    agent_hub: TestClient, inner: dict, expected: dict
) -> None:
    sign_in(agent_hub, "code-alice")
    _insert_replica(
        agent_hub,
        table="session",
        row_id="tool-view-session",
        updated_at="2026-11-05T09:00:00Z",
        login="helperbot",
        payload={"id": "tool-view-session", "created_at": "2026-11-05T09:00:00Z"},
    )
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="tool-view-run",
        updated_at="2026-11-05T09:01:00Z",
        login="helperbot",
        payload={
            "id": "tool-view-run",
            "type": "lane.run",
            "session_id": "tool-view-session",
            "payload": inner,
        },
    )
    timeline = agent_hub.get(
        "/api/session-timeline?id=tool-view-session"
    ).json()["timeline"]
    run = next(row for row in timeline if row["row_id"] == "tool-view-run")
    assert run["tools"] == expected
    assert all("tools" not in row for row in timeline if row["row_id"] != "tool-view-run")
