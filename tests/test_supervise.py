from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.runtime import Completed, Runtime
from agent_cli.store import Store, StoreError
from agent_cli.supervise import (
    ANSWER_BLOCKED,
    ANSWER_CAN,
    ANSWER_NO,
    ANSWER_YES,
    LAST_WORKING_KEY,
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


def _assigned(store: Store, sid: str = "runner-1", assigned_by: str = "") -> str:
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
                "assigned_by": assigned_by,
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


def test_parse_closed_answer_ignores_ja_in_scrollback() -> None:
    pane = (
        "Ja\n"
        "Referral-Programm umsetzen, nächster Schritt.\n"
        "     Worked for 35m21s\n"
        "  Help improve Grok                                         [Opt out] [Opt in]\n"
        "  Shift+Tab:mode  │  Ctrl+x:shortcuts\n"
    )
    assert parse_closed_answer(pane) is None


def test_enqueue_is_idempotent_and_survives_gh_failure(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    store.set_meta("github_login", "alice")

    def runner(argv: list[str]) -> Completed:
        joined = " ".join(argv)
        if joined == "gh api user":
            return Completed(0, json.dumps({"login": "alice"}), "")
        return Completed(1, "", "no gh")

    first = enqueue_assigned(store, "runner-1", "octo/app", 3, runner)
    second = enqueue_assigned(store, "runner-1", "octo/app", 3, runner)
    assert first == second
    row = store.row("activity", first)
    assert row is not None
    assert row["type"] == "issue.assigned"
    assert row["payload"]["mandate"] == "github-assignment"
    assert row["payload"]["url"] == "https://github.com/octo/app/issues/3"
    assert row["payload"]["assigned_by"] == "alice"


def test_enqueue_broken_pairing_raises_without_writing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)

    def runner(argv: list[str]) -> Completed:
        return Completed(1, "", "no gh")

    with pytest.raises(StoreError):
        enqueue_assigned(store, "runner-1", "octo/app", 3, runner)
    assert [
        row for row in store.rows("activity") if row.get("type") == "issue.assigned"
    ] == []


def test_enqueue_broken_pairing_on_new_session_writes_no_session_row(
    tmp_path: Path,
) -> None:
    # Distinct from test_enqueue_broken_pairing_raises_without_writing: that
    # test pre-creates the session, so _ensure_assigned_session is a no-op
    # and can never catch a leak there. This one uses a session id that does
    # not exist yet, so a broken pairing must raise before ANY store write —
    # including the session row itself.
    store = Store(tmp_path)

    def runner(argv: list[str]) -> Completed:
        return Completed(1, "", "no gh")

    with pytest.raises(StoreError):
        enqueue_assigned(store, "brand-new", "octo/app", 3, runner)
    assert store.row("session", "brand-new") is None
    assert [
        row for row in store.rows("activity") if row.get("type") == "issue.assigned"
    ] == []


def test_enqueue_uses_gh_json(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    store.set_meta("github_login", "octocat")
    body = {
        "title": "t",
        "body": "b",
        "html_url": "https://github.com/octo/app/issues/3",
        "assignee": "octocat",
    }

    def runner(argv: list[str]) -> Completed:
        joined = " ".join(argv)
        if joined == "gh api user":
            return Completed(0, json.dumps({"login": "octocat"}), "")
        assert argv[:3] == ["gh", "api", "repos/octo/app/issues/3"]
        return Completed(0, json.dumps(body), "")

    aid = enqueue_assigned(store, "runner-1", "octo/app", 3, runner)
    row = store.row("activity", aid)
    assert row is not None
    assert row["payload"]["assignee"] == "octocat"
    assert row["payload"]["title"] == "t"
    assert row["payload"]["assigned_by"] == "octocat"


def test_enqueue_assigned_sets_assigned_by_from_paired_login(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    store.set_meta("github_login", "octocat")
    body = {
        "title": "t",
        "body": "b",
        "html_url": "https://github.com/octo/app/issues/3",
        "assignee": "octocat",
    }

    def runner(argv: list[str]) -> Completed:
        joined = " ".join(argv)
        if joined == "gh api user":
            return Completed(0, json.dumps({"login": "octocat"}), "")
        assert argv[:3] == ["gh", "api", "repos/octo/app/issues/3"]
        return Completed(0, json.dumps(body), "")

    aid = enqueue_assigned(store, "runner-1", "octo/app", 3, runner)
    row = store.row("activity", aid)
    assert row is not None
    assert row["payload"]["assigned_by"] == "octocat"


def test_tick_denies_and_does_not_mutate_when_policy_rejects(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    assigned = _assigned(store, assigned_by="mallory")
    (store.home / "policy.json").write_text(
        json.dumps(
            {
                "actors_allow": ["alice"],
                "repos_allow": ["octo/app"],
                "job_types_allow": ["implement"],
            }
        ),
        encoding="utf-8",
    )
    def runner(argv: list[str]) -> Completed:
        if ".private" in " ".join(argv):
            return Completed(0, "false", "")
        raise AssertionError(argv)

    rt = FakeRuntime(exists=False, pane="")
    line = tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: None,
        knock=lambda aid: "sent",
        runner=runner,
    )
    assert line == f"supervise denied assigned={assigned}"
    events = [r for r in store.rows("activity") if r.get("type") == "supervise.event"]
    assert events == []
    assert store.sync_get(LAST_WORKING_KEY) is None


def test_tick_denies_pane_up_and_does_not_mutate_when_policy_rejects(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    _session(store)
    assigned = _assigned(store, assigned_by="mallory")
    (store.home / "policy.json").write_text(
        json.dumps(
            {
                "actors_allow": ["alice"],
                "repos_allow": ["octo/app"],
                "job_types_allow": ["implement"],
            }
        ),
        encoding="utf-8",
    )
    def runner(argv: list[str]) -> Completed:
        if ".private" in " ".join(argv):
            return Completed(0, "false", "")
        raise AssertionError(argv)

    rt = FakeRuntime(exists=True, pane="")
    line = tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: None,
        knock=lambda aid: "sent",
        runner=runner,
    )
    assert line == f"supervise denied assigned={assigned}"
    events = [r for r in store.rows("activity") if r.get("type") == "supervise.event"]
    assert events == []
    assert store.sync_get(LAST_WORKING_KEY) is None


def test_tick_commissions_when_policy_admits_pane_up(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    assigned = _assigned(store, assigned_by="alice")
    (store.home / "policy.json").write_text(
        json.dumps(
            {
                "actors_allow": ["alice"],
                "repos_allow": ["octo/app"],
                "job_types_allow": ["implement"],
            }
        ),
        encoding="utf-8",
    )

    def runner(argv: list[str]) -> Completed:
        joined = " ".join(argv)
        if ".private" in joined:
            return Completed(0, "false", "")
        raise AssertionError(argv)

    rt = FakeRuntime(exists=True, pane="")
    line = tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: None,
        knock=lambda aid: "sent",
        runner=runner,
    )
    assert line == f"supervise commission assigned={assigned} dispatch=held"
    events = [r for r in store.rows("activity") if r.get("type") == "supervise.event"]
    assert len(events) == 1
    assert events[0]["payload"]["kind"] == "commission"
    assert store.sync_get(LAST_WORKING_KEY) is not None


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
        quiet_seconds=0,
        ask=True,
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
        quiet_seconds=0,
        ask=True,
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
    tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent", quiet_seconds=0, ask=True)
    tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent", quiet_seconds=0, ask=True)
    rt.pane = ANSWER_NO
    line = tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent", quiet_seconds=0, ask=True)
    assert "phase=why" in line
    rt.pane = ANSWER_BLOCKED
    line = tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent", quiet_seconds=0, ask=True)
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
    tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent", quiet_seconds=0, ask=True)
    tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent", quiet_seconds=0, ask=True)
    rt.pane = ANSWER_NO
    tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent", quiet_seconds=0, ask=True)
    rt.pane = ANSWER_CAN
    line = tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent", quiet_seconds=0, ask=True)
    assert line.startswith("supervise continue")
    acks = [r for r in store.rows("activity") if r.get("type") == "issue.assigned.ack"]
    assert acks == []


def test_tick_does_not_restart_when_pane_missing_after_commission(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    _session(store)
    assigned = _assigned(store)
    knocks: list[str] = []
    starts: list[str] = []
    rt = FakeRuntime(exists=True, pane="")
    tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: starts.append(sid),
        knock=lambda aid: knocks.append(aid) or "sent",
        ask=False,
    )
    rt.present = False
    line = tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: starts.append(sid),
        knock=lambda aid: knocks.append(aid) or "sent",
        ask=False,
    )
    assert line.startswith("supervise missing")
    assert assigned in line
    assert knocks == []
    assert starts == []


def test_tick_does_not_knock_again_while_busy(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    _assigned(store)
    knocks: list[str] = []
    rt = FakeRuntime(busy=True, pane="")
    first = tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: None,
        knock=lambda aid: knocks.append(aid) or "sent",
        quiet_seconds=0,
    )
    assert "dispatch=held" in first
    assert knocks == []
    tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: None,
        knock=lambda aid: knocks.append(aid) or "sent",
        quiet_seconds=0,
    )
    assert knocks == []


def test_tick_busy_does_not_ask(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    _assigned(store)
    rt = FakeRuntime(busy=True, pane="")
    tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent", quiet_seconds=0, ask=True)
    rt.sent.clear()
    line = tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent", quiet_seconds=0, ask=True)
    assert line.startswith("supervise busy")
    assert rt.sent == []


def test_tick_approves_permission_prompt_even_when_busy(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    assigned = _assigned(store)
    pane = (
        "  1 (\u25cf) Yes, and don't ask again for anything (always-approve mode)\n"
        "  1/3:select  \u2502  Tab:next option\n"
    )
    rt = FakeRuntime(busy=True, pane=pane)
    tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: None,
        knock=lambda aid: "sent",
        quiet_seconds=0,
    )
    rt.keys.clear()
    line = tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: None,
        knock=lambda aid: "sent",
        quiet_seconds=0,
    )
    assert line.startswith("supervise approve")
    assert assigned in line
    assert rt.keys == ["enter"]


def test_follow_does_not_ask_when_stalled(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    assigned = _assigned(store)
    rt = FakeRuntime(pane="")
    tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: None,
        knock=lambda aid: "sent",
        now=1_000.0,
    )
    rt.sent.clear()
    from agent_cli.telegram_act import TELEGRAM_IDLE_TICKS

    for i in range(1, TELEGRAM_IDLE_TICKS):
        line = tick(
            store,
            rt,
            "runner-1",
            start=lambda sid, cwd: None,
            knock=lambda aid: "sent",
            ask=False,
        )
        assert line.startswith("supervise quiet")
        assert f"streak={i}" in line
        assert rt.sent == []
    line = tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: None,
        knock=lambda aid: "sent",
        ask=False,
    )
    assert line.startswith("supervise stalled")
    assert assigned in line
    assert rt.sent == []


def test_tick_quiet_does_not_ask_for_ten_minutes(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    assigned = _assigned(store)
    rt = FakeRuntime(pane="")
    tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: None,
        knock=lambda aid: "sent",
        now=1_000.0,
    )
    rt.sent.clear()
    line = tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: None,
        knock=lambda aid: "sent",
        now=1_000.0 + 60,
    )
    assert line.startswith("supervise quiet")
    assert assigned in line
    assert rt.sent == []
    line = tick(
        store,
        rt,
        "runner-1",
        start=lambda sid, cwd: None,
        knock=lambda aid: "sent",
        now=1_000.0 + 600,
        quiet_seconds=600,
        ask=True,
    )
    assert "ask phase=done" in line
    assert QUESTION_DONE in rt.sent


def test_tick_idle_without_queue(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _session(store)
    rt = FakeRuntime()
    assert tick(store, rt, "runner-1", start=lambda sid, cwd: None, knock=lambda aid: "sent", quiet_seconds=0, ask=True) == (
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
