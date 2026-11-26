"""U-HUB-tasks (captured tasks, E-41, ADR-100): `POST /v1/tasks` with only the `task.meta`, the ticket fields of
`PUT /v1/tasks/{task}/fields`, the filters of `GET /v1/workgroups/{wg}/tasks`, the status on start, close and reopen,
the `topic.profile` kind, the main agents that skip a task without an agent, its attention record (`done/idle`: "No
agent" of the overview of the open tasks) and the overview filter and counters of the task dashboard (ADR-101)."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.attention import jobs as attention_jobs
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.events.support import Json
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.tasks.support import Desk, audit_rows, frames, hints


def _topic(desk: Desk, author: Actor | None = None, ref: str | None = None) -> str:
    """A topic of the organization: a `topic.profile` event in its org scope."""
    topic = ref or new_id()
    event = desk.scene.org_event(author or desk.scene.writer, "topic.profile", ref=topic)
    response = desk.scene.post(author or desk.scene.writer, event)
    assert response.status_code == 200, response.text
    return topic


def _meta(desk: Desk, actor: Actor, task: str) -> Json:
    return desk.scene.wg_event(actor, "task.meta", task=task, scope_id=desk.wg, wg=desk.wg)


def _capture(desk: Desk, *, actor: Actor | None = None, task: str | None = None, **body: Any) -> Any:
    who = actor or desk.scene.writer
    task_id = task or new_id()
    return desk.create(who, task_id, events=[_meta(desk, who, task_id)], json_extra=body)


@pytest.fixture(autouse=True)
def _json_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Desk.create` with extra body fields (`json_extra`)."""
    original = Desk.create

    def create(self: Desk, actor: Actor, task: str | None = None, *, json_extra: Any = None, **options: Any) -> Any:
        if not json_extra:
            return original(self, actor, task, **options)
        task_id = task or new_id()
        events = options.pop("events", None) or self.task_events(actor, task_id)
        body = {"task_id": task_id, "wg_id": options.pop("wg", None) or self.wg, "events": events, **json_extra}
        return actor.browser.post("/v1/tasks", json=body, **options)

    monkeypatch.setattr(Desk, "create", create)


def test_capturing_starts_no_agent(desk: Desk, published: MemoryPublisher) -> None:
    topic = _topic(desk)
    published.clear()
    response = _capture(desk, urgency="high", assignee_id=desk.scene.reader.id, topic_id=topic)
    assert response.status_code == 200, response.text
    body = response.json()
    task = body["task"]
    assert body["session"] is None
    assert len(body["events"]) == 1
    assert (task["state"], task["status"], task["main_session_id"]) == ("idle", "todo", None)
    assert (task["urgency"], task["assignee_id"], task["topic_id"]) == ("high", desk.scene.reader.id, topic)
    assert desk.rows("SELECT count(*) FROM hub.session WHERE task_id = %s", task["task_id"]) == [(0,)]
    assert desk.rows("SELECT state FROM hub.task_transition WHERE task_id = %s", task["task_id"]) == [("idle",)]
    assert hints(published) == []
    assert frames(published, "task")[-1]["status"] == "todo"
    assert audit_rows(desk, "task.create")[-1][3] == {"origin": "browser", "captured": True}
    # its attention record says that no agent works on it ("No agent" of the start page, ADR-101); the main agents
    # know no agent of it
    record = "SELECT category, reason FROM hub.task_attention WHERE task_id = %s"
    assert desk.rows(record, task["task_id"]) == [("done", "idle")]
    assert attention_jobs.backfill(desk.hub) == 0
    # a captured task of before ADR-101 (no record) gets it from the backfill
    desk.execute("DELETE FROM hub.task_attention WHERE task_id = %s", task["task_id"])
    assert attention_jobs.backfill(desk.hub) == 1
    assert desk.rows(record, task["task_id"]) == [("done", "idle")]
    people = desk.scene.writer.browser.get(f"/v1/orgs/{desk.org}/people/agents")
    assert people.status_code == 200, people.text
    assert all(row["owner_id"] != desk.scene.writer.id for row in people.json()["people"])
    mine = desk.scene.writer.browser.get(f"/v1/orgs/{desk.org}/main-agents?owner={desk.scene.writer.id}")
    assert (mine.status_code, mine.json()["items"]) == (200, [])
    # other events under the known task ID: a conflict
    again = _capture(desk, task=task["task_id"])
    assert (again.status_code, again.json()) == (409, {"error": "id_conflict"})


def test_a_repeated_capture_answers_the_task(desk: Desk) -> None:
    writer = desk.scene.writer
    task = new_id()
    events = [_meta(desk, writer, task)]
    first = desk.create(writer, task, events=events, json_extra={"urgency": "low"})
    assert first.status_code == 200, first.text
    again = desk.create(writer, task, events=events, json_extra={"urgency": "low"})
    assert again.status_code == 200
    assert again.json()["replayed"] is True
    assert again.json()["session"] is None
    other = desk.create(writer, task, events=events, json_extra={"urgency": "urgent"})
    assert (other.status_code, other.json()) == (409, {"error": "id_conflict"})


@pytest.mark.parametrize(
    ("extra", "pointer"),
    [
        ({"urgency": "critical"}, "/urgency"),
        ({"assignee_id": "not-a-uuid"}, "/assignee_id"),
        ({"topic_id": "not-a-uuid"}, "/topic_id"),
        ({"analysis": {"scope": "wg"}}, "/analysis"),
    ],
)
def test_capture_refuses_malformed_fields(desk: Desk, extra: dict[str, Any], pointer: str) -> None:
    task = new_id()
    who = desk.scene.writer
    body = {"task_id": task, "wg_id": desk.wg, "events": [_meta(desk, who, task)], **extra}
    response = who.browser.post("/v1/tasks", json=body, extensions={"contract": False})
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["pointers"] == [pointer]


def test_capture_refuses_unknown_people_and_topics(desk: Desk) -> None:
    cases: list[tuple[str, str]] = [
        ("assignee_id", desk.scene.stranger.id),
        ("assignee_id", desk.scene.device.id),
        ("topic_id", new_id()),
    ]
    for name, value in cases:
        pointer = f"/{name}"
        field: dict[str, Any] = {name: value}
        response = _capture(desk, urgency="normal", **field)
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["pointers"] == [pointer]


def test_an_agent_task_carries_the_same_fields(desk: Desk) -> None:
    topic = _topic(desk, desk.scene.reader)
    response = desk.create(desk.scene.writer, json_extra={"urgency": "urgent", "topic_id": topic})
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert (task["status"], task["urgency"], task["topic_id"], task["assignee_id"]) == (
        "inProgress",
        "urgent",
        topic,
        None,
    )
    assert response.json()["session"] is not None


def test_the_first_prompt_starts_the_agent(desk: Desk, published: MemoryPublisher) -> None:
    response = _capture(desk)
    task = response.json()["task"]["task_id"]
    published.clear()
    started = desk.prompt(desk.scene.writer, task)
    assert started.status_code == 200, started.text
    row = desk.task(task)
    assert row["status"] == "inProgress"
    assert row["main_session_id"] == started.json()["session_id"]
    assert row["state"] in ("scheduling", "starting")
    assert hints(published, kind="session.start")
    assert desk.rows("SELECT count(*) FROM hub.task_attention WHERE task_id = %s", task) == [(1,)]


def test_fields_change_and_are_audited(desk: Desk, published: MemoryPublisher) -> None:
    task = _capture(desk).json()["task"]["task_id"]
    topic = _topic(desk)
    published.clear()
    path = f"/v1/tasks/{task}/fields"
    body = {"urgency": "urgent", "assignee_id": desk.scene.admin.id, "topic_id": topic, "status": "inProgress"}
    response = desk.scene.writer.browser.put(path, json=body)
    assert response.status_code == 200, response.text
    row = response.json()["task"]
    assert (row["urgency"], row["assignee_id"], row["topic_id"], row["status"], row["state"]) == (
        "urgent",
        desk.scene.admin.id,
        topic,
        "inProgress",
        "idle",
    )
    assert frames(published, "task")[-1]["urgency"] == "urgent"
    assert audit_rows(desk, "task.fields")[-1][3] == {
        "urgency": "urgent",
        "assignee_id": desk.scene.admin.id,
        "topic_id": topic,
        "status": "inProgress",
    }
    # the same values again change nothing; clearing person and topic does
    published.clear()
    same = desk.scene.writer.browser.put(path, json={"urgency": "urgent"})
    assert same.status_code == 200
    assert frames(published, "task") == []
    cleared = desk.scene.writer.browser.put(path, json={"assignee_id": None, "topic_id": None})
    assert (cleared.json()["task"]["assignee_id"], cleared.json()["task"]["topic_id"]) == (None, None)
    assert audit_rows(desk, "task.fields")[-1][3] == {"assignee_id": None, "topic_id": None}


def test_completed_closes_and_todo_reopens(desk: Desk, published: MemoryPublisher) -> None:
    task, main = desk.started()
    desk.walk(main, "prompt_delivering", "working")
    published.clear()
    path = f"/v1/tasks/{task}/fields"
    done = desk.scene.writer.browser.put(path, json={"status": "completed"})
    assert done.status_code == 200, done.text
    assert (done.json()["task"]["state"], done.json()["task"]["status"]) == ("closed", "completed")
    assert desk.session(main)["state"] == "stopping"
    assert [item["session_id"] for item in hints(published, kind="session.control")] == [main]
    assert audit_rows(desk, "task.close")[-1][3] == {"by": "user"}
    reopened = desk.scene.writer.browser.put(path, json={"status": "todo"})
    row = reopened.json()["task"]
    assert (row["state"], row["status"], row["closed_at"]) == ("idle", "todo", None)
    # reopened by its status: its record leaves done/closed (no agent works on it until the next prompt)
    assert desk.rows("SELECT category, reason FROM hub.task_attention WHERE task_id = %s", task) == [("done", "idle")]
    # a follow-up prompt starts a new main session: inProgress again
    desk.transition(main, "stopped")
    assert desk.prompt(desk.scene.writer, task).status_code == 200
    assert desk.task(task)["status"] == "inProgress"


def test_a_captured_task_completes_without_an_agent(desk: Desk) -> None:
    task = _capture(desk).json()["task"]["task_id"]
    done = desk.scene.writer.browser.put(f"/v1/tasks/{task}/fields", json={"status": "completed"})
    assert (done.json()["task"]["state"], done.json()["task"]["status"]) == ("closed", "completed")
    record = "SELECT category, reason FROM hub.task_attention WHERE task_id = %s"
    assert desk.rows(record, task) == [("done", "closed")]
    back = desk.scene.writer.browser.put(f"/v1/tasks/{task}/fields", json={"status": "inProgress"})
    assert (back.json()["task"]["state"], back.json()["task"]["status"]) == ("idle", "inProgress")
    # reopened without an agent: open again, and no agent works on it
    assert desk.rows(record, task) == [("done", "idle")]


def test_close_and_reopen_keep_the_status(desk: Desk) -> None:
    """The trigger of migration 0040: every path that closes or reopens keeps closed ⇔ completed."""
    task, main = desk.started()
    closed = desk.scene.writer.browser.post(f"/v1/tasks/{task}/close", json={})
    assert closed.json()["task"]["status"] == "completed"
    desk.transition(main, "stopped")
    assert desk.prompt(desk.scene.writer, task).status_code == 200
    assert desk.task(task)["status"] == "inProgress"
    # a write that names a state only (the previous hub code)
    desk.execute("UPDATE hub.task SET state = 'closed', closed_at = now() WHERE id = %s", task)
    assert desk.rows("SELECT status FROM hub.task WHERE id = %s", task) == [("completed",)]
    desk.execute("UPDATE hub.task SET state = 'idle', closed_at = NULL WHERE id = %s", task)
    assert desk.rows("SELECT status FROM hub.task WHERE id = %s", task) == [("inProgress",)]


def test_fields_rights_and_form(desk: Desk) -> None:
    task = _capture(desk).json()["task"]["task_id"]
    path = f"/v1/tasks/{task}/fields"
    for actor in (desk.scene.reader, desk.scene.stranger):
        response = actor.browser.put(path, json={"urgency": "low"})
        assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    assert desk.scene.writer.browser.put(f"/v1/tasks/{new_id()}/fields", json={"urgency": "low"}).status_code == 404
    for body, pointer in (
        ({}, ""),
        ({"status": "done"}, "/status"),
        ({"urgency": "critical"}, "/urgency"),
        ({"assignee_id": "x"}, "/assignee_id"),
        ({"topic_id": new_id()}, "/topic_id"),
        ({"assignee_id": desk.scene.stranger.id}, "/assignee_id"),
    ):
        response = desk.scene.writer.browser.put(path, json=body, extensions={"contract": False})
        assert response.status_code == 400, (body, response.text)
        assert response.json()["detail"]["pointers"] == [pointer]


def test_the_list_filters_by_the_fields(desk: Desk) -> None:
    topic = _topic(desk)
    first = _capture(desk, urgency="high", assignee_id=desk.scene.reader.id, topic_id=topic).json()["task"]["task_id"]
    second = _capture(desk).json()["task"]["task_id"]
    third, _ = desk.started()
    base = f"/v1/workgroups/{desk.wg}/tasks?order=activity"

    def listed(query: str) -> list[str]:
        response = desk.scene.reader.browser.get(f"{base}&{query}")
        assert response.status_code == 200, response.text
        return [row["task_id"] for row in response.json()["tasks"]]

    assert listed("status=todo") == [second, first]
    assert listed("status=inProgress") == [third]
    assert listed("urgency=high") == [first]
    assert listed(f"assignee={desk.scene.reader.id}") == [first]
    assert listed("assignee=none") == [third, second]
    assert listed(f"topic={topic}") == [first]
    assert listed("topic=none&status=todo") == [second]
    assert listed("state=idle&urgency=normal") == [second]
    # the overview of the open tasks (the start page's three groups, ADR-101): captured tasks have no agent
    assert listed("overview=no_agent") == [second, first]
    assert listed("overview=working") == [third]
    assert listed("overview=waiting") == []
    assert listed("overview=no_agent&urgency=high") == [first]
    for query, pointer in (
        ("status=done", "/status"),
        ("urgency=x", "/urgency"),
        ("assignee=x", "/assignee"),
        ("topic=x", "/topic"),
        ("overview=no-agent", "/overview"),
    ):
        response = desk.scene.reader.browser.get(f"{base}&{query}", extensions={"contract": False})
        assert response.status_code == 400, query
        assert response.json()["detail"]["pointers"] == [pointer]


def _dashboard(desk: Desk) -> dict[str, str]:
    """Four tasks a second apart: a captured urgent task without a person, a captured normal one of the reader, an
    agent task (inProgress) and a completed captured one."""
    found: dict[str, str] = {}
    tasks: list[tuple[str, dict[str, Any]]] = [
        ("urgent", {"urgency": "urgent"}),
        ("mine", {"assignee_id": desk.scene.reader.id}),
        ("done", {}),
    ]
    for name, extra in tasks:
        desk.hub.clock.advance(1)
        found[name] = _capture(desk, **extra).json()["task"]["task_id"]
    desk.hub.clock.advance(1)
    found["agent"], _ = desk.started()
    desk.hub.clock.advance(1)
    response = desk.scene.writer.browser.put(f"/v1/tasks/{found['done']}/fields", json={"status": "completed"})
    assert response.status_code == 200, response.text
    return found


def test_the_dashboard_counters(desk: Desk) -> None:
    """ADR-101 section 1: per status the tasks matching every filter but the status, per urgency every filter
    but the urgency, per overview of the open tasks every filter but the overview, total all of them."""
    found = _dashboard(desk)
    base = f"/v1/orgs/{desk.org}/tasks/counts"

    def counted(query: str = "") -> dict[str, Any]:
        response = desk.scene.reader.browser.get(f"{base}?{query}" if query else base)
        assert response.status_code == 200, response.text
        return dict(response.json())

    everything = counted()
    assert everything == {
        "by_status": {"todo": 2, "inProgress": 1, "completed": 1},
        "by_urgency": {"low": 0, "normal": 3, "high": 0, "urgent": 1},
        # the agent task works; both open captured tasks have no agent; the completed one is not open
        "by_overview": {"working": 1, "waiting": 0, "no_agent": 2},
        "total": 4,
    }
    no_agent = counted("overview=no_agent")
    assert (no_agent["by_overview"], no_agent["by_status"], no_agent["total"]) == (
        everything["by_overview"],
        {"todo": 2, "inProgress": 0, "completed": 0},
        2,
    )
    assert counted("urgency=urgent")["by_overview"] == {"working": 0, "waiting": 0, "no_agent": 1}
    assert counted("overview=waiting")["total"] == 0
    # a task without a record yet is in no overview (the backfill derives it)
    desk.execute("DELETE FROM hub.task_attention WHERE task_id = %s", found["agent"])
    assert counted()["by_overview"] == {"working": 0, "waiting": 0, "no_agent": 2}
    assert attention_jobs.backfill(desk.hub) == 1
    todo = counted("status=todo")
    assert (todo["by_status"], todo["by_urgency"]["normal"], todo["by_urgency"]["urgent"], todo["total"]) == (
        everything["by_status"],
        1,
        1,
        2,
    )
    urgent = counted("urgency=urgent")
    assert (urgent["by_status"]["todo"], urgent["by_status"]["inProgress"], urgent["total"]) == (1, 0, 1)
    assert urgent["by_urgency"] == everything["by_urgency"]
    assert counted(f"assignee={desk.scene.reader.id}")["total"] == 1
    assert counted("assignee=none&topic=none")["total"] == 3
    assert counted("state=closed")["by_status"] == {"todo": 0, "inProgress": 0, "completed": 1}
    assert counted(f"wg={desk.wg}") == everything
    # a workgroup the caller does not read, another organization, a stranger
    assert desk.scene.reader.browser.get(f"{base}?wg={new_id()}").status_code == 404
    assert desk.scene.stranger.browser.get(base).status_code in (403, 404)
    for query, pointer in (
        ("status=done", "/status"),
        ("urgency=x", "/urgency"),
        ("assignee=x", "/assignee"),
        ("topic=x", "/topic"),
        ("wg=x", "/wg"),
        ("state=x", "/state"),
        ("overview=idle", "/overview"),
    ):
        response = desk.scene.reader.browser.get(f"{base}?{query}", extensions={"contract": False})
        assert response.status_code == 400, query
        assert response.json()["detail"]["pointers"] == [pointer]


def test_the_list_by_urgency(desk: Desk) -> None:
    """ADR-101 section 2: `order=urgency`, the most urgent first, then the latest activity, paged."""
    found = _dashboard(desk)
    base = f"/v1/workgroups/{desk.wg}/tasks?order=urgency&limit=1"
    seen: list[str] = []
    target = base
    while True:
        response = desk.scene.reader.browser.get(target)
        assert response.status_code == 200, response.text
        seen += [row["task_id"] for row in response.json()["tasks"]]
        cursor = response.json()["next_before"]
        if cursor is None:
            break
        target = f"{base}&before={cursor}"
    assert seen == [found["urgent"], found["done"], found["agent"], found["mine"]]
    for cursor in ("x", "e30", "eyJhdCI6MCwiaWQiOiIwMCIsInJhbmsiOjl9"):
        response = desk.scene.reader.browser.get(f"{base}&before={cursor}", extensions={"contract": False})
        assert (response.status_code, response.json()["detail"]["pointers"]) == (400, ["/before"])
    response = desk.scene.reader.browser.get(
        f"/v1/workgroups/{desk.wg}/tasks?order=title", extensions={"contract": False}
    )
    assert response.status_code == 400


def test_topics_are_written_by_every_member(desk: Desk) -> None:
    topic = new_id()
    _topic(desk, desk.scene.reader, topic)
    renamed = _topic(desk, desk.scene.admin, topic)
    assert renamed == topic
    found = desk.scene.reader.browser.get(f"/v1/scopes/{desk.org}/events?kinds=topic.profile")
    assert found.status_code == 200
    assert [item["event"]["header"]["ref"] for item in found.json()["events"]] == [topic, topic]
    stranger = desk.scene.org_event(desk.scene.stranger, "topic.profile", ref=new_id())
    assert desk.scene.post(desk.scene.stranger, stranger).status_code in (400, 403)
