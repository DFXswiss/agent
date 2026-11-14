"""Lane process records and recovery of abandoned working rows."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_cli import coordinator_lanes
from agent_cli.coordinator_common import control_dir, coord
from agent_cli.coordinator_exec import run_bounded
from agent_cli.coordinator_lanes import (
    _prepare_pr_review_agent,
    _run_prepared,
    forget_lane_process,
    lane_process_path,
    lane_process_state,
    lane_start_recorder,
    launch_lane,
    recover_working_lanes,
)
from agent_cli.lane import LaneResult
from agent_cli.runtime import Completed
from agent_cli.store import Store
from test_coordinator_support import make_session, make_worker, write_accounts


TID = "aa000001-0000-4000-8000-000000000601"


@contextmanager
def sleeping_process():
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=5)


def seed_lane_task(
    store: Store,
    worker,
    *,
    phase: str = "implement",
    head: str = "a" * 40,
    current_round: int = 1,
) -> dict:
    worktree = worker.workspace_root / "lane-process-task"
    worktree.mkdir(parents=True, exist_ok=True)
    task = {
        "id": TID,
        "session_id": worker.session_id,
        "workflow": "implement",
        "state": "implementing",
        "current_round": current_round,
        "models": {"first": "grok", "second": "codex"},
        "payload": {
            "coordinator": {
                "phase": phase,
                "head_sha": head,
                "worktree": str(worktree),
            }
        },
    }
    store.write("task", "insert", TID, task)
    saved = store.row("task", TID)
    assert saved is not None
    return saved


def working_row(
    store: Store,
    worker,
    agent_id: str,
    *,
    role: str = "implementer",
    vendor: str = "grok",
    **fields,
) -> dict:
    row = {
        "id": agent_id,
        "task_id": TID,
        "session_id": worker.session_id,
        "round": 1,
        "role": role,
        "vendor": vendor,
        "provider": vendor,
        "model": f"{vendor}-model",
        "status": "working",
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": None,
        "note": None,
    }
    row.update(fields)
    store.write("agent", "insert", agent_id, row)
    saved = store.row("agent", agent_id)
    assert saved is not None
    return saved


def _result(role: str, vendor: str) -> LaneResult:
    return LaneResult(
        role=role,
        vendor=vendor,
        model=f"{vendor}-model",
        status="complete",
        argv=[],
        returncode=0,
        stdout="STATUS: complete\nRESULT: approved\n",
        stderr="",
        protocol_result="approved",
    )


def _write_spool(
    store: Store,
    worker,
    task: dict,
    row: dict,
    *,
    round_num: int | None,
    head: str,
    dimension: str,
    stage: str,
) -> Path:
    return coordinator_lanes._write_reviewer_spool(
        store,
        worker,
        task,
        {"id": row["id"]},
        _result(str(row["role"]), str(row["vendor"])),
        round_num=round_num,
        head=head,
        dimension=dimension,
        stage=stage,
        output_file=None,
    )


@pytest.mark.no_pg
def test_run_bounded_reports_the_child_pid() -> None:
    seen: list[int] = []
    completed = run_bounded(
        [sys.executable, "-c", "print(1)"], timeout=10, on_start=seen.append
    )
    assert completed.returncode == 0
    assert len(seen) == 1
    assert seen[0] > 0


@pytest.mark.no_pg
def test_on_start_failure_does_not_break_the_run() -> None:
    def fail(_pid: int) -> None:
        raise RuntimeError("bookkeeping failed")

    completed = run_bounded(
        [sys.executable, "-c", "print(1)"], timeout=10, on_start=fail
    )
    assert completed.returncode == 0
    assert completed.stdout == "1\n"


@pytest.mark.no_pg
def test_state_without_a_record_is_dead(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    path = lane_process_path(worker, TID, "agent-1")
    assert lane_process_state(worker, TID, "agent-1") == "dead"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not JSON", encoding="utf-8")
    assert lane_process_state(worker, TID, "agent-1") == "dead"
    for pid in (0, True, "123", 2**31):
        path.write_text(json.dumps({"pid": pid}), encoding="utf-8")
        assert lane_process_state(worker, TID, "agent-1") == "dead"


@pytest.mark.no_pg
def test_state_of_a_live_process(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    with sleeping_process() as proc:
        lane_start_recorder(worker, TID, "agent-1")(proc.pid)
        data = json.loads(
            lane_process_path(worker, TID, "agent-1").read_text(encoding="utf-8")
        )
        assert set(data) == {"pid", "lstart", "started_at"}
        assert lane_process_state(worker, TID, "agent-1") == "alive"
        proc.kill()
        proc.wait(timeout=5)
        assert lane_process_state(worker, TID, "agent-1") == "dead"


@pytest.mark.no_pg
def test_reused_process_id_reads_as_dead(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    with sleeping_process() as proc:
        path = lane_process_path(worker, TID, "agent-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "pid": proc.pid,
                    "lstart": "Thu Jan  1 00:00:00 1970",
                    "started_at": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        assert lane_process_state(worker, TID, "agent-1") == "dead"


@pytest.mark.no_pg
def test_old_start_is_expired(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    with sleeping_process() as proc:
        path = lane_process_path(worker, TID, "agent-1")
        lane_start_recorder(worker, TID, "agent-1")(proc.pid)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["started_at"] = "2020-01-01T00:00:00Z"
        path.write_text(json.dumps(data), encoding="utf-8")
        assert lane_process_state(worker, TID, "agent-1") == "expired"


@pytest.mark.no_pg
def test_forget_removes_the_record(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    path = lane_process_path(worker, TID, "agent-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    forget_lane_process(worker, TID, "agent-1")
    assert not path.exists()
    forget_lane_process(worker, TID, "agent-1")


@pytest.mark.no_pg
def test_run_prepared_reports_and_forgets_a_real_process(tmp_path: Path) -> None:
    seen: list[int] = []
    forgotten: list[bool] = []
    agent_id, result = _run_prepared(
        {
            "agent_id": "agent-1",
            "role": "pr-reviewer-quality",
            "vendor": "grok",
            "argv": [
                sys.executable,
                "-c",
                "print('STATUS: complete'); print('RESULT: approved')",
            ],
            "stdin_text": None,
            "output_file": None,
            "model": "grok-model",
            "worktree": str(tmp_path),
            "on_start": seen.append,
            "forget": lambda: forgotten.append(True),
        },
        timeout=30,
        lane_runner=None,
    )
    assert agent_id == "agent-1"
    assert result.status == "complete"
    assert result.protocol_result == "approved"
    assert len(seen) == 1 and seen[0] > 0
    assert forgotten == [True]


@pytest.mark.no_pg
def test_run_prepared_forgets_when_the_runner_raises(tmp_path: Path) -> None:
    seen: list[int] = []
    forgotten: list[bool] = []

    def fail(_argv: list[str], _stdin: str | None) -> Completed:
        raise RuntimeError("lane failed")

    _agent_id, result = _run_prepared(
        {
            "agent_id": "agent-1",
            "role": "pr-reviewer-quality",
            "vendor": "grok",
            "argv": [sys.executable, "-c", "print(1)"],
            "stdin_text": None,
            "output_file": None,
            "model": "grok-model",
            "worktree": str(tmp_path),
            "on_start": seen.append,
            "forget": lambda: forgotten.append(True),
        },
        timeout=30,
        lane_runner=fail,
    )
    assert isinstance(result, RuntimeError)
    assert forgotten == [True]
    assert seen == []


def test_prepared_review_agent_carries_the_process_callbacks(tmp_path: Path) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    task = seed_lane_task(store, worker)
    prep = _prepare_pr_review_agent(
        store,
        worker,
        task,
        role="pr-reviewer-quality",
        vendor="grok",
        stage="review-1",
        head="a" * 40,
        spec_body="Review this.",
    )
    prep["on_start"](os.getpid())
    path = lane_process_path(worker, str(task["id"]), prep["agent_id"])
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    prep["forget"]()
    assert not path.exists()


def test_real_lane_process_is_recorded_while_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    task = seed_lane_task(store, worker)
    monkeypatch.setattr(
        coordinator_lanes, "verify_checkout_identity", lambda *a, **k: {}
    )
    script = """
import json
import os
import pathlib
import sys
import time

directory = pathlib.Path(sys.argv[1])
seen = False
deadline = time.time() + 10
while time.time() < deadline:
    for path in directory.glob("*.json") if directory.exists() else ():
        try:
            if json.loads(path.read_text())["pid"] == os.getpid():
                seen = True
                break
        except (OSError, ValueError, KeyError):
            pass
    if seen:
        break
    time.sleep(0.05)
print("RECORD-SEEN" if seen else "RECORD-MISSING")
print("STATUS: complete")
print("RESULT: done")
"""

    def provider(_vendor: str, **_kwargs) -> list[str]:
        return [
            sys.executable,
            "-c",
            script,
            str(control_dir(worker, TID) / "lanes"),
        ]

    monkeypatch.setattr(coordinator_lanes, "provider_argv", provider)
    agent, result = launch_lane(
        store,
        worker,
        task,
        role="implementer",
        vendor="grok",
        round_num=1,
        spec_body="Implement.",
        runner=lambda argv: Completed(0, "", ""),
        lane_runner=None,
    )
    assert "RECORD-SEEN" in result.stdout
    assert result.status == "complete"
    assert store.row("agent", agent["id"])["status"] == "done"
    assert not list((control_dir(worker, TID) / "lanes").glob("*.json"))


def test_injected_lane_runner_writes_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    task = seed_lane_task(store, worker)
    monkeypatch.setattr(
        coordinator_lanes, "verify_checkout_identity", lambda *a, **k: {}
    )
    _agent, result = launch_lane(
        store,
        worker,
        task,
        role="implementer",
        vendor="grok",
        round_num=1,
        spec_body="Implement.",
        runner=lambda argv: Completed(0, "", ""),
        lane_runner=lambda argv, stdin: Completed(
            0, "STATUS: complete\nRESULT: done\n", ""
        ),
    )
    assert result.status == "complete"
    assert not (control_dir(worker, TID) / "lanes").exists()


def test_recover_without_working_rows_is_none(tmp_path: Path) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    task = seed_lane_task(store, worker)
    working_row(
        store,
        worker,
        "agent-1",
        status="done",
        finished_at="2026-01-01T00:01:00Z",
    )
    assert recover_working_lanes(store, worker, task) == "none"
    assert store.row("agent", "agent-1")["status"] == "done"
    assert "lane_wait" not in coord(store.row("task", TID))


def test_recover_closes_a_row_without_a_live_process(tmp_path: Path) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    task = seed_lane_task(store, worker)
    working_row(store, worker, "agent-1")
    assert recover_working_lanes(store, worker, task) == "recovered"
    saved = store.row("agent", "agent-1")
    assert saved["status"] == "done"
    assert saved["note"] == "recovered: no live process"
    assert saved["finished_at"]
    assert recover_working_lanes(store, worker, task) == "none"


def test_recover_closes_a_check_lane_row_without_reporting_a_recovery(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    task = seed_lane_task(store, worker)
    working_row(
        store, worker, "agent-1", role="check", round=None, check_key="duplicates_checked"
    )
    assert recover_working_lanes(store, worker, task) == "none"
    saved = store.row("agent", "agent-1")
    assert saved["status"] == "done"
    assert saved["note"] == "recovered: no live process"
    assert saved["finished_at"]
    store.close()


def test_recover_waits_for_a_live_process(tmp_path: Path) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    task = seed_lane_task(store, worker)
    working_row(store, worker, "agent-1")
    original_c = coord(task)
    with sleeping_process() as proc:
        lane_start_recorder(worker, TID, "agent-1")(proc.pid)
        assert recover_working_lanes(store, worker, task) == "waiting"
        assert coord(task) is original_c
        assert coord(task)["lane_wait"] is True
        assert coord(store.row("task", TID))["lane_wait"] is True
        assert store.row("agent", "agent-1")["status"] == "working"
        proc.kill()
        proc.wait(timeout=5)
        assert recover_working_lanes(store, worker, task) == "recovered"
        assert "lane_wait" not in coord(task)
        assert "lane_wait" not in coord(store.row("task", TID))


def test_recover_kills_an_expired_process(tmp_path: Path) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    task = seed_lane_task(store, worker)
    working_row(store, worker, "agent-1")
    with sleeping_process() as proc:
        path = lane_process_path(worker, TID, "agent-1")
        lane_start_recorder(worker, TID, "agent-1")(proc.pid)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["started_at"] = "2020-01-01T00:00:00Z"
        path.write_text(json.dumps(data), encoding="utf-8")
        assert recover_working_lanes(store, worker, task) == "recovered"
        proc.wait(timeout=5)
        assert store.row("agent", "agent-1")["status"] == "done"
        assert not path.exists()


def test_recover_leaves_an_inner_reviewer_row_with_a_saved_result(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    task = seed_lane_task(store, worker, phase="inner_review")
    row = working_row(
        store,
        worker,
        "agent-1",
        role="reviewer",
        head_sha="a" * 40,
        dimension="inner",
        stage="inner-review",
    )
    spool = _write_spool(
        store,
        worker,
        task,
        row,
        round_num=1,
        head="a" * 40,
        dimension="inner",
        stage="inner-review",
    )
    assert recover_working_lanes(store, worker, task) == "none"
    assert store.row("agent", "agent-1")["status"] == "working"
    assert spool.exists()


@pytest.mark.parametrize(
    ("phase", "head", "current_round"),
    [
        ("implement", "a" * 40, 1),
        ("inner_review", "b" * 40, 1),
        ("inner_review", "a" * 40, 2),
    ],
)
def test_recover_closes_a_reviewer_row_whose_saved_result_is_for_another_step(
    tmp_path: Path, phase: str, head: str, current_round: int
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    task = seed_lane_task(
        store, worker, phase=phase, head=head, current_round=current_round
    )
    row = working_row(
        store,
        worker,
        "agent-1",
        role="reviewer",
        head_sha="a" * 40,
        dimension="inner",
        stage="inner-review",
    )
    _write_spool(
        store,
        worker,
        task,
        row,
        round_num=1,
        head="a" * 40,
        dimension="inner",
        stage="inner-review",
    )
    assert recover_working_lanes(store, worker, task) == "recovered"
    assert store.row("agent", "agent-1")["status"] == "done"


def test_recover_leaves_a_pull_request_reviewer_row_with_a_saved_result(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    task = seed_lane_task(store, worker, phase="pr_gates_codex")
    row = working_row(
        store,
        worker,
        "agent-1",
        role="pr-reviewer-quality",
        vendor="codex",
        round=None,
        head_sha="a" * 40,
        dimension="quality",
        stage="review-2",
    )
    _write_spool(
        store,
        worker,
        task,
        row,
        round_num=None,
        head="a" * 40,
        dimension="quality",
        stage="review-2",
    )
    assert recover_working_lanes(store, worker, task) == "none"
    assert store.row("agent", "agent-1")["status"] == "working"
    coord(task)["phase"] = "pr_gates_grok"
    assert recover_working_lanes(store, worker, task) == "recovered"
    assert store.row("agent", "agent-1")["status"] == "done"


def test_recover_closes_a_dead_row_next_to_a_live_one(tmp_path: Path) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    task = seed_lane_task(store, worker)
    working_row(store, worker, "agent-live")
    working_row(store, worker, "agent-dead")
    with sleeping_process() as proc:
        lane_start_recorder(worker, TID, "agent-live")(proc.pid)
        assert recover_working_lanes(store, worker, task) == "waiting"
        assert store.row("agent", "agent-live")["status"] == "working"
        assert store.row("agent", "agent-dead")["status"] == "done"
