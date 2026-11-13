"""Round-limit pinning and grant behavior for coordinator tasks."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent_cli.coordinator import tick
from agent_cli.coordinator_checks import (
    CheckContext,
    next_round,
    registry_row_id,
    resolve_round_budget,
)
from agent_cli.coordinator_common import coord
from agent_cli.coordinator_git import control_dir
from agent_cli.coordinator_runtime import _create_implement_task
from agent_cli.preconditions import CHECKS
from agent_cli.store import Store
from test_coordinator import patch_account_runners, patch_run_bounded
from test_coordinator_support import (
    FakeGh,
    lane_runner,
    make_session,
    make_worker,
    write_accounts,
)


def _setup(tmp_path, monkeypatch):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_run_bounded(monkeypatch, fake)
    return store, worker, fake


def _task(store, worker, task_id="22222222-2222-4222-8222-222222222222", **c):
    checkpoint = {
        "phase": "implement",
        "source": {"repo": "example/project", "number": 7, "base": "develop"},
        **c,
    }
    row = {
        "id": task_id,
        "session_id": worker.session_id,
        "workflow": "implement",
        "title": "Fix widget",
        "repo": "example/project",
        "ref": None,
        "payload": {"coordinator": checkpoint},
        "state": "open",
        "current_round": 0,
        "models": {"first": "grok", "second": "codex"},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    store.write("task", "insert", task_id, row)
    return store.row("task", task_id)


def _round(store, task_id, number, verdict=None):
    row_id = f"round-{number}"
    store.write(
        "task_round",
        "insert",
        row_id,
        {
            "id": row_id,
            "task_id": task_id,
            "round": number,
            "implementer_verdict": verdict,
            "reviewer_verdict": None,
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": None,
        },
    )


def _phase(store: Store) -> str | None:
    tasks = store.rows("task")
    if not tasks:
        return None
    return str(coord(tasks[0]).get("phase") or "")


def _tick_until_phase(store, worker, fake, target: str) -> None:
    lane = lane_runner(fake)
    for _ in range(8):
        phase = _phase(store)
        if phase == target:
            return
        if phase == "implement":
            fake.dirty = True
        if phase == "publish_draft":
            fake.commits_ahead = True
        tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == target


def _finish_rejected_round(store, worker, fake) -> None:
    assert _phase(store) == "implement"
    fake.dirty = True
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    _tick_until_phase(store, worker, fake, "implement")


def _reach_round_limit_question(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch)
    worker = replace(worker, max_rounds=2)
    fake.model_outputs["reviewer"] = (
        "STATUS: complete\nRESULT: rejected\nfix the widget\n"
    )
    _tick_until_phase(store, worker, fake, "implement")
    _finish_rejected_round(store, worker, fake)
    _finish_rejected_round(store, worker, fake)
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.rows("task")[0]
    assert coord(task)["phase"] == "ask"
    return store, worker, fake, task, lines


def _grant_more_rounds(tmp_path, monkeypatch):
    store, worker, fake, task, _lines = _reach_round_limit_question(
        tmp_path, monkeypatch
    )
    c = coord(task)
    qid = str(c["question_activity_id"])
    assert any(
        f"<!-- agent-coordinator:question:v1:{qid} -->" in str(comment.get("body"))
        for comment in fake.comments
    )
    fake.comments.append(
        {
            "id": max(int(comment["id"]) for comment in fake.comments) + 1,
            "body": "Please try the focused fix.",
            "user": {"login": "human-owner"},
        }
    )
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    assert _phase(store) == "implement"
    fake.dirty = True
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", str(task["id"]))
    return store, worker, fake, task


def test_next_round(tmp_path, monkeypatch):
    store, worker, _fake = _setup(tmp_path, monkeypatch)
    task = _task(store, worker)
    assert next_round(store, task) == 1

    task["current_round"] = 1
    store.write(
        "task",
        "update",
        task["id"],
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    _round(store, task["id"], 1)
    assert next_round(store, task) == 1
    round_row = store.row("task_round", "round-1")
    round_row["implementer_verdict"] = "done"
    store.write(
        "task_round",
        "update",
        "round-1",
        {key: value for key, value in round_row.items() if not key.startswith("_")},
    )
    assert next_round(store, task) == 2


def test_created_task_pins_the_limit_and_its_origin(tmp_path, monkeypatch):
    store, base, _fake = _setup(tmp_path, monkeypatch)
    cases = [
        (
            replace(
                base,
                max_rounds_by_model={"grok-model": 3},
                max_rounds=7,
            ),
            7,
            {"max_rounds": 3, "origin": "model", "model": "grok-model", "limit": 3},
        ),
        (
            replace(base, max_rounds=7),
            8,
            {"max_rounds": 7, "origin": "worker", "model": "grok-model", "limit": 7},
        ),
        (
            base,
            9,
            {"max_rounds": 10, "origin": "default", "model": "grok-model", "limit": 10},
        ),
    ]
    for worker, number, expected in cases:
        task = _create_implement_task(
            store,
            worker,
            repo="example/project",
            number=number,
            title="Fix widget",
            assigned_id=f"assigned-{number}",
            repository_config=worker.repositories["example/project"],
            body="Fix the widget",
        )
        assert task is not None
        assert coord(task)["round_limit"] == expected


def test_task_without_a_pin_is_pinned_at_first_evaluation(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch)
    task = _task(store, worker)
    value = resolve_round_budget(
        CheckContext(
            store,
            worker,
            task,
            fake,
            None,
            CHECKS["round_budget"],
            "implement",
        )
    )
    assert value.status == "ja"
    assert coord(task)["round_limit"] == {
        "max_rounds": 10,
        "origin": "default",
        "model": "grok-model",
        "limit": 10,
    }


@pytest.mark.parametrize(
    "max_rounds, expected",
    [
        (5, {"max_rounds": 5, "origin": "worker", "model": "", "limit": 5}),
        (
            None,
            {"max_rounds": 10, "origin": "default", "model": "", "limit": 10},
        ),
    ],
)
def test_task_without_a_model_pin_is_pinned_without_an_error(
    tmp_path, monkeypatch, max_rounds, expected
):
    store, base, fake = _setup(tmp_path, monkeypatch)
    task = _task(store, base)
    task.pop("models")
    store.write(
        "task",
        "update",
        task["id"],
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    worker = replace(base, max_rounds=max_rounds)
    value = resolve_round_budget(
        CheckContext(
            store,
            worker,
            task,
            fake,
            None,
            CHECKS["round_budget"],
            "implement",
        )
    )
    assert value.status == "ja"
    assert coord(task)["round_limit"] == expected


def test_a_changed_configuration_does_not_change_a_pinned_task(
    tmp_path, monkeypatch
):
    store, base, fake = _setup(tmp_path, monkeypatch)
    pin = {"max_rounds": 2, "origin": "worker", "model": "grok-model", "limit": 2}
    task = _task(store, base, round_limit=pin)
    value = resolve_round_budget(
        CheckContext(
            store,
            replace(base, max_rounds=9),
            task,
            fake,
            None,
            CHECKS["round_budget"],
            "implement",
        )
    )
    assert value.status == "ja"
    assert coord(task)["round_limit"] == pin


def test_round_limit_raises_a_question(tmp_path, monkeypatch):
    store, _worker, fake, task, lines = _reach_round_limit_question(
        tmp_path, monkeypatch
    )
    c = coord(task)
    assert fake.launched.count("implementer") == 2
    assert fake.launched.count("reviewer") == 2
    assert any(line == "check round_budget=nein; question raised" for line in lines)
    assert c["phase"] == "ask"
    assert c["resume_phase"] == "implement"
    assert c["failed_check"] == "round_budget"
    comments = [
        row
        for row in store.rows("activity")
        if row.get("type") == "comment.post"
        and "Round limit reached" in str(row.get("payload"))
    ]
    assert len(comments) == 1
    body = str(comments[0]["payload"]["body"])
    assert (
        "Round limit reached for example/project#7: 2 implementation rounds"
        in body
    )
    assert "Limit: 2 rounds per grant (configured for the worker)." in body
    assert "fix the widget" in body
    assert "An authorized reply grants 2 more rounds" in body
    row = store.row(
        "checklist_item", registry_row_id(str(task["id"]), "round_budget")
    )
    assert row["status"] == "nein"
    assert row["evidence"] == (
        "round 3 exceeds the limit of 2 implementation rounds"
    )
    assert c["round_limit_question"] == {"limit": 2, "replies": 0}


def test_reply_grants_more_rounds_and_reaches_the_implementer(
    tmp_path, monkeypatch
):
    store, worker, fake, task = _grant_more_rounds(tmp_path, monkeypatch)
    c = coord(task)
    assert c["round_limit"]["limit"] == 4
    assert "round_limit_question" not in c
    assert "failed_check" not in c
    assert "question_text" not in c
    row = store.row(
        "checklist_item", registry_row_id(str(task["id"]), "round_budget")
    )
    assert row["status"] == "ja"
    assert row["evidence"] == "rounds within limit 4"
    prompt = control_dir(worker, str(task["id"])) / "implementer-grok.md"
    assert "Please try the focused fix." in prompt.read_text(encoding="utf-8")
    assert fake.launched.count("implementer") == 3


def test_grant_is_applied_once(tmp_path, monkeypatch):
    store, worker, fake, task = _grant_more_rounds(tmp_path, monkeypatch)
    context = CheckContext(
        store,
        worker,
        task,
        fake,
        None,
        CHECKS["round_budget"],
        "implement",
    )
    first = resolve_round_budget(context)
    second = resolve_round_budget(context)
    assert first.status == second.status == "ja"
    assert coord(task)["round_limit"]["limit"] == 4


def test_rounds_within_the_default_limit_never_ask(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch)
    fake.model_outputs["reviewer"] = (
        "STATUS: complete\nRESULT: rejected\nfix the widget\n"
    )
    _tick_until_phase(store, worker, fake, "implement")
    for _ in range(3):
        _finish_rejected_round(store, worker, fake)
    task = store.rows("task")[0]
    assert fake.launched.count("implementer") == 3
    assert fake.launched.count("reviewer") == 3
    assert coord(task)["round_limit"]["origin"] == "default"
    assert "round_limit_question" not in coord(task)
    assert not any(
        "Round limit reached" in str(comment.get("body"))
        for comment in fake.comments
    )
