from __future__ import annotations

import json
from pathlib import Path

from agent_cli.runtime import Completed, Runtime
from agent_cli.store import Store
from agent_cli.supervise import (
    ANSWER_BLOCKED,
    ANSWER_CAN,
    ANSWER_NO,
    ANSWER_YES,
    QUESTION_DONE,
    enqueue_assigned,
    parse_closed_answer,
    tick,
)


class FakeRuntime(Runtime):
    def __init__(
        self,
        *,
        busy: bool = False,
        pane: str = "",
        exists: bool = True,
    ) -> None:
        super().__init__(runner=lambda argv: Completed(0, "", ""))
        self.busy = busy
        self.pane = pane
        self.present = exists
        self.sent: list[str] = []
        self.keys: list[str] = []

    def exists(self, session_id: str, *, target: str | None = None) -> bool:
        return self.present

    def is_busy(self, session_id: str, *, settle: float | None = None) -> bool:
        return self.busy

    def grok_working(self, session_id: str) -> bool:
        return self.busy

    def capture(self, session_id: str) -> str:
        return self.pane

    def input_text(self, session_id: str, data: str, *, target: str | None = None) -> None:
        self.sent.append(data)

    def input_key(self, session_id: str, key: str, *, target: str | None = None) -> None:
        self.keys.append(key)


def _session(store: Store, sid: str = "runner-1") -> None:
    store.write(
        "session",
        "insert",
        sid,
        {
            "id": sid,
            "kind": "runner",
            "status": "active",
            "skills": ["spine", "review-loop", "pr-review"],
            "runtime": {"control": "attached", "tmux_session": "agent-runner-1"},
        },
    )


def _assigned(store: Store, sid: str = "runner-1") -> str:
    aid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    store.write(
        "activity",
        "insert",
        aid,
        {
            "id": aid,
            "session_id": sid,
            "type": "issue.assigned",
            "payload": {
                "repo": "octo/app",
                "number": 3,
                "url": "https://github.com/octo/app/issues/3",
                "title": "example",
                "body": "ignore this body",
                "assigned_at": "2026-08-27T00:00:00Z",
                "assignee": "someone",
                "mandate": "github-assignment",
            },
            "execution_status": "done",
        },
    )
    return aid


def test_parse_closed_answer_last_token() -> None:
    pane = "story\nJa\nNein\n"
    assert parse_closed_answer(pane) == ANSWER_NO
    assert parse_closed_answer("hello") is None
    assert parse_closed_answer(f'"{ANSWER_YES}"') == ANSWER_YES


def test_enqueue_is_idempotent_and_survives_gh_failure(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)

    def runner(argv: list[str]) -> Completed:
        return Completed(1, "", "no gh")

    first = enqueue_assigned(store, "runner-1", "octo/app", 3, runner)
    second = enqueue_assigned(store, "runner-1", "octo/app", 3, runner)
    assert first == second
    row = store.row("activity", first)
    assert row is not None
    assert row["type"] == "issue.assigned"
    assert row["payload"]["mandate"] == "github-assignment"
    assert row["payload"]["url"] == "https://github.com/octo/app/issues/3"


def test_enqueue_uses_gh_json(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    body = {
        "title": "t",
        "body": "b",
        "html_url": "https://github.com/octo/app/issues/3",
        "assignee": "octocat",
    }

    def runner(argv: list[str]) -> Completed:
        assert argv[:3] == ["gh", "api", "repos/octo/app/issues/3"]
        return Completed(0, json.dumps(body), "")

    aid = enqueue_assigned(store, "runner-1", "octo/app", 3, runner)
    row = store.row("activity", aid)
    assert row is not None
    assert row["payload"]["assignee"] == "octocat"
    assert row["payload"]["title"] == "t"


def test_tick_commissions_then_asks_then_acks_yes(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    assigned = _assigned(store)
    knocks: list[str] = []
    starts: list[str] = []
    rt = FakeRuntime(busy=False, pane="")
    line = tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: starts.append(sid),
        knock=lambda aid: knocks.append(aid) or "sent",
    )
    assert assigned in line
    assert line.startswith("supervise commission")
    events = [r for r in store.rows("activity") if r.get("type") == "supervise.event"]
    assert events[0]["payload"]["kind"] == "commission"
    line = tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: starts.append(sid),
        knock=lambda aid: knocks.append(aid) or "sent",
    )
    assert "ask phase=done" in line
    assert QUESTION_DONE in rt.sent
    rt.pane = ANSWER_YES
    line = tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: starts.append(sid),
        knock=lambda aid: knocks.append(aid) or "sent",
    )
    assert line.startswith("supervise done")
    acks = [r for r in store.rows("activity") if r.get("type") == "issue.assigned.ack"]
    assert len(acks) == 1
    assert acks[0]["payload"]["assigned_id"] == assigned


def test_tick_skip_on_blocked(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    assigned = _assigned(store)
    rt = FakeRuntime(pane="")
    tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent")
    tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent")
    rt.pane = ANSWER_NO
    line = tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent")
    assert "phase=why" in line
    rt.pane = ANSWER_BLOCKED
    line = tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent")
    assert line.startswith("supervise skip")
    skips = [
        r
        for r in store.rows("activity")
        if r.get("type") == "supervise.event" and r.get("payload", {}).get("kind") == "skip"
    ]
    assert len(skips) == 1
    assert skips[0]["payload"]["assigned_id"] == assigned
    acks = [r for r in store.rows("activity") if r.get("type") == "issue.assigned.ack"]
    assert len(acks) == 1


def test_tick_continue_on_can_finish(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    _assigned(store)
    rt = FakeRuntime(pane="")
    tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent")
    tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent")
    rt.pane = ANSWER_NO
    tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent")
    rt.pane = ANSWER_CAN
    line = tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent")
    assert line.startswith("supervise continue")
    acks = [r for r in store.rows("activity") if r.get("type") == "issue.assigned.ack"]
    assert acks == []


def test_tick_busy_does_not_ask(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    _assigned(store)
    rt = FakeRuntime(busy=True, pane="")
    tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent")
    rt.sent.clear()
    line = tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent")
    assert line.startswith("supervise busy")
    assert rt.sent == []


def test_tick_idle_without_queue(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    rt = FakeRuntime()
    assert tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent") == (
        "supervise idle"
    )


def test_activity_add_refuses_supervise_event(tmp_path: Path) -> None:
    from agent_cli.main import main
    import os

    os.environ["AGENT_HOME"] = str(tmp_path)
    main(["init"])
    main(
        [
            "session",
            "register",
            "--id",
            "runner-1",
            "--kind",
            "runner",
            "--skill",
            "spine",
        ]
    )
    payload = tmp_path / "p.json"
    payload.write_text('{"kind":"ask"}', encoding="utf-8")
    try:
        main(
            [
                "activity",
                "add",
                "--session",
                "runner-1",
                "--type",
                "supervise.event",
                "--payload-file",
                str(payload),
            ]
        )
        raised = False
    except SystemExit:
        raised = True
    assert raised is True
