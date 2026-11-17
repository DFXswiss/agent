"""Lane process records and recovery of abandoned working rows."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from agent_cli import coordinator_exec, coordinator_lanes
from agent_cli.coordinator_common import CoordinatorError, control_dir, coord
from agent_cli.coordinator_exec import process_scope, run_bounded
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
from agent_cli.drivers import LaneIO
from agent_cli.lane import LaneResult
from agent_cli.runtime import Completed
from agent_cli.coordinator_orders import consume_order_intents
from agent_cli.store import Store
from test_coordinator_support import (
    event_stream_output,
    insert_order_intent,
    make_order_worker,
    make_session,
    make_worker,
    seed_order_task,
    write_accounts,
)
from driver_fixtures import (
    accounts_document,
    write_accounts as write_placeholder_accounts,
    write_drivers,
)

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
    models: dict | None = None,
) -> dict:
    worktree = worker.workspace_root / "lane-process-task"
    worktree.mkdir(parents=True, exist_ok=True)
    task = {
        "id": TID,
        "session_id": worker.session_id,
        "workflow": "implement",
        "state": "implementing",
        "current_round": current_round,
        "models": models or {"first": "driver-a", "second": "driver-b"},
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
    vendor: str = "driver-a",
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
            "vendor": "driver-a",
            "io": LaneIO("event-stream", output_file=False, stdin=False, audit_stderr_when_empty=False),
            "argv": [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write(sys.argv[1])",
                event_stream_output("STATUS: complete\nRESULT: approved"),
            ],
            "stdin_text": None,
            "output_file": None,
            "model": "driver-a-model",
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
            "vendor": "driver-a",
            "io": LaneIO("event-stream", output_file=False, stdin=False, audit_stderr_when_empty=False),
            "argv": [sys.executable, "-c", "print(1)"],
            "stdin_text": None,
            "output_file": None,
            "model": "driver-a-model",
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
        vendor="driver-a",
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
sys.stdout.write(sys.argv[2] if seen else sys.argv[3])
"""
    seen_stream = event_stream_output("RECORD-SEEN\nSTATUS: complete\nRESULT: done")
    missing_stream = event_stream_output("RECORD-MISSING\nSTATUS: partial")

    def provider(*_args, **_kwargs) -> list[str]:
        return [
            sys.executable,
            "-c",
            script,
            str(control_dir(worker, TID) / "lanes"),
            seen_stream,
            missing_stream,
        ]

    monkeypatch.setattr(coordinator_lanes, "lane_argv", provider)
    agent, result = launch_lane(
        store,
        worker,
        task,
        role="implementer",
        vendor="driver-a",
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
        vendor="driver-a",
        round_num=1,
        spec_body="Implement.",
        runner=lambda argv: Completed(0, "", ""),
        lane_runner=lambda argv, stdin: Completed(
            0, event_stream_output("STATUS: complete\nRESULT: done\n"), ""
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
    task = seed_lane_task(store, worker, phase="pr_gates_second")
    row = working_row(
        store,
        worker,
        "agent-1",
        role="pr-reviewer-quality",
        vendor="driver-b",
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
    coord(task)["phase"] = "pr_gates_first"
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


def _lane_runs(store: Store) -> list[dict]:
    return [row for row in store.rows("activity") if row.get("type") == "lane.run"]


def _implementer_stream() -> str:
    text = (
        "STATUS: complete\n"
        "RESULT: done\n"
        "SUMMARY_EN: The lane kept running and its result was recorded once.\n"
        "SUMMARY_DE: Die Lane lief weiter und ihr Ergebnis wurde einmal gespeichert.\n"
        "REUSED: none — the process record already identifies the lane.\n"
        "ADDED: none — the captured output is the lane result.\n"
    )
    return event_stream_output(text)


def _prepared_store(tmp_path: Path):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    task = seed_lane_task(store, worker)
    return store, worker, task


def test_coordinator_stop_adopts_a_running_lane(tmp_path: Path) -> None:
    store, worker, task = _prepared_store(tmp_path)
    agent_id = "agent-restart"
    working_row(store, worker, agent_id)
    paths = coordinator_lanes._lane_capture_paths(worker, TID, agent_id)
    marker = tmp_path / "release-lane"
    program = (
        "import sys\n"
        "from pathlib import Path\n"
        "from agent_cli.coordinator_config import WorkerConfig\n"
        "from agent_cli.coordinator_exec import process_scope, run_bounded\n"
        "from agent_cli.coordinator_lanes import lane_start_recorder\n"
        "workspace, stdout_path, stderr_path, status_path, pid_path, task_id, agent_id, marker, stream = sys.argv[1:]\n"
        "worker = WorkerConfig(\n"
        " session_id='worker-session', review_session=None, workspace_root=Path(workspace),\n"
        " repositories={}, reply_logins=(), poll_seconds=30, lane_timeout=60, check_timeout=30,\n"
        ")\n"
        "script = (\n"
        " 'import sys, time\\n'\n"
        " 'from pathlib import Path\\n'\n"
        " 'sys.stdout.write(\"PARTIAL\\\\n\")\\n'\n"
        " 'sys.stdout.flush()\\n'\n"
        " 'sys.stderr.write(\"ERR\\\\n\")\\n'\n"
        " 'sys.stderr.flush()\\n'\n"
        " 'deadline = time.time() + 20\\n'\n"
        " 'while time.time() < deadline and not Path(sys.argv[1]).exists():\\n'\n"
        " ' time.sleep(0.05)\\n'\n"
        " 'sys.stdout.write(sys.argv[2])\\n'\n"
        " 'sys.stdout.flush()\\n'\n"
        ")\n"
        "with process_scope():\n"
        " run_bounded([sys.executable, '-u', '-c', script, marker, stream], timeout=40,\n"
        "  on_start=lane_start_recorder(worker, task_id, agent_id, capture=True),\n"
        "  survive_stop=True, stdout_path=stdout_path, stderr_path=stderr_path,\n"
        "  status_path=status_path, pid_path=pid_path)\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(Path(__file__).resolve().parents[1] / "src")
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )
    owner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            program,
            str(worker.workspace_root),
            str(paths["stdout"]),
            str(paths["stderr"]),
            str(paths["status"]),
            str(paths["pid"]),
            TID,
            agent_id,
            str(marker),
            _implementer_stream(),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )
    command_pid = None
    try:
        record = lane_process_path(worker, TID, agent_id)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and owner.poll() is None:
            if (
                paths["pid"].is_file()
                and record.is_file()
                and paths["stdout"].is_file()
                and paths["stderr"].is_file()
                and "PARTIAL" in paths["stdout"].read_text(encoding="utf-8")
                and "ERR" in paths["stderr"].read_text(encoding="utf-8")
            ):
                command_pid = int(paths["pid"].read_text(encoding="utf-8").strip())
                saved_process = json.loads(record.read_text(encoding="utf-8"))
                if (
                    saved_process.get("capture") is True
                    and saved_process.get("pid") == command_pid
                ):
                    break
                command_pid = None
            time.sleep(0.05)
        assert command_pid is not None, owner.communicate(timeout=5)
        os.kill(command_pid, 0)
        owner.send_signal(signal.SIGTERM)
        owner.communicate(timeout=5)
        assert owner.returncode == 128 + signal.SIGTERM
        os.kill(command_pid, 0)
        marker.write_text("go", encoding="utf-8")
        assert recover_working_lanes(store, worker, task) == "none"
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.communicate(timeout=5)
        if command_pid is not None:
            try:
                os.killpg(command_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    saved = store.row("agent", agent_id)
    assert saved["status"] == "done"
    assert saved["note"] != "recovered: no live process"
    outcome = coord(store.row("task", TID))["lane_outcome"]
    assert outcome["agent_id"] == agent_id
    assert outcome["status"] == "complete"
    assert outcome["result"] == "done"
    assert outcome["applied"] is False
    assert len(_lane_runs(store)) == 1
    assert "lane-failed" not in json.dumps(coord(store.row("task", TID)))
    assert recover_working_lanes(store, worker, task) == "none"
    assert len(_lane_runs(store)) == 1


def test_finished_lane_is_collected_once_after_downtime(tmp_path: Path) -> None:
    store, worker, task = _prepared_store(tmp_path)
    agent_id = "agent-finished"
    working_row(store, worker, agent_id)
    paths = coordinator_lanes._lane_capture_paths(worker, TID, agent_id)
    completed = run_bounded(
        [
            sys.executable,
            "-u",
            "-c",
            "import sys; sys.stdout.write(sys.argv[1])",
            _implementer_stream(),
        ],
        timeout=30,
        on_start=lane_start_recorder(worker, TID, agent_id, capture=True),
        survive_stop=True,
        stdout_path=str(paths["stdout"]),
        stderr_path=str(paths["stderr"]),
        status_path=str(paths["status"]),
        pid_path=str(paths["pid"]),
    )
    assert completed.returncode == 0
    assert recover_working_lanes(store, worker, task) == "none"
    saved = store.row("agent", agent_id)
    assert saved["status"] == "done"
    assert saved["note"] != "recovered: no live process"
    outcome = coord(store.row("task", TID))["lane_outcome"]
    assert outcome["result"] == "done"
    assert outcome["returncode"] == 0
    assert len(_lane_runs(store)) == 1
    assert recover_working_lanes(store, worker, task) == "none"
    assert len(_lane_runs(store)) == 1
    assert not paths["status"].exists()


def test_lane_timeout_across_a_restart_kills_and_records_once(tmp_path: Path) -> None:
    store, worker, task = _prepared_store(tmp_path)
    agent_id = "agent-timeout"
    working_row(store, worker, agent_id)
    paths = coordinator_lanes._lane_capture_paths(worker, TID, agent_id)
    paths["stdout"].parent.mkdir(parents=True, exist_ok=True)
    paths["stdout"].write_text("", encoding="utf-8")
    paths["stderr"].write_text("", encoding="utf-8")
    with sleeping_process() as proc:
        lane_start_recorder(worker, TID, agent_id, capture=True)(proc.pid)
        record = lane_process_path(worker, TID, agent_id)
        data = json.loads(record.read_text(encoding="utf-8"))
        data["started_at"] = "2020-01-01T00:00:00Z"
        record.write_text(json.dumps(data), encoding="utf-8")
        assert lane_process_state(worker, TID, agent_id) == "expired"
        assert recover_working_lanes(store, worker, task) == "none"
        proc.wait(timeout=5)
        assert proc.returncode is not None and proc.returncode < 0
    saved = store.row("agent", agent_id)
    assert saved["status"] == "done"
    assert saved["note"] != "recovered: no live process"
    outcome = coord(store.row("task", TID))["lane_outcome"]
    assert outcome["status"] == "timeout"
    assert outcome["returncode"] == 124
    assert outcome["result"] == ""
    assert len(_lane_runs(store)) == 1
    assert recover_working_lanes(store, worker, task) == "none"
    assert len(_lane_runs(store)) == 1
    assert store.row("agent", agent_id)["note"] != "recovered: no live process"


def _capture_files(root: Path) -> dict[str, str]:
    return {
        "stdout_path": str(root / "lane.stdout"),
        "stderr_path": str(root / "lane.stderr"),
        "status_path": str(root / "lane.status"),
        "pid_path": str(root / "lane.pid"),
    }


def _wait_until_dead(pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.killpg(pid, signal.SIGKILL)
    raise AssertionError(f"lane {pid} still alive")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _stopped_descendant_pids(root: int) -> list[int]:
    """Stopped descendants of ``root``. A published lane pid is not required."""
    completed = subprocess.run(
        ["ps", "-ax", "-o", "pid=,ppid=,stat="],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    children: dict[int, list[int]] = {}
    stats: dict[int, str] = {}
    for line in (completed.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        pid = int(parts[0])
        ppid = int(parts[1])
        children.setdefault(ppid, []).append(pid)
        stats[pid] = parts[2]
    stopped: list[int] = []
    stack = list(children.get(root, []))
    while stack:
        pid = stack.pop()
        stack.extend(children.get(pid, []))
        if "T" in stats.get(pid, ""):
            stopped.append(pid)
    return stopped


def _durable_partial_lanes(directory: Path) -> list[dict]:
    """Capture records whose command is alive and has already written output."""
    ready: list[dict] = []
    if not directory.is_dir():
        return ready
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid_text = path.with_suffix(".pid").read_text(encoding="utf-8").strip()
            stdout = path.with_suffix(".stdout").read_text(encoding="utf-8")
            pid = int(pid_text)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
        if not path.with_suffix(".stderr").is_file():
            continue
        if (
            isinstance(data, dict)
            and data.get("capture") is True
            and data.get("pid") == pid
            and "PARTIAL" in stdout
            and _pid_alive(pid)
        ):
            ready.append({"pid": pid, "record": path})
    return ready


@pytest.mark.no_pg
def test_surviving_lane_creates_a_missing_capture_directory(tmp_path: Path) -> None:
    root = tmp_path / "missing" / "lanes"
    completed = run_bounded(
        [sys.executable, "-c", "print('ok')"],
        timeout=10,
        survive_stop=True,
        clear_ambient_github=False,
        **_capture_files(root),
    )
    assert completed.returncode == 0
    assert (root / "lane.stdout").read_text(encoding="utf-8") == "ok\n"


@pytest.mark.no_pg
def test_stop_before_the_capture_record_kills_the_lane(tmp_path: Path) -> None:
    seen: dict[str, int] = {}

    def stop(pid: int) -> None:
        seen["pid"] = pid
        raise SystemExit(128 + signal.SIGTERM)

    with pytest.raises(SystemExit):
        run_bounded(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=30,
            on_start=stop,
            survive_stop=True,
            clear_ambient_github=False,
            **_capture_files(tmp_path / "lanes"),
        )
    _wait_until_dead(seen["pid"])


@pytest.mark.no_pg
def test_stop_while_polling_the_pid_file_kills_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _capture_files(tmp_path / "lanes")
    pid_path = Path(files["pid_path"])
    real_sleep = time.sleep
    real_is_file = Path.is_file
    sent = {"done": False}
    hidden = {"used": False}

    def hide_first_published(self: Path) -> bool:
        found = real_is_file(self)
        if found and str(self) == str(pid_path) and not hidden["used"]:
            hidden["used"] = True
            return False
        return found

    def sleep_until_published(seconds: float) -> None:
        if not sent["done"] and real_is_file(pid_path):
            try:
                text = pid_path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                text = ""
            if text:
                sent["done"] = True
                os.kill(os.getpid(), signal.SIGTERM)
                return
        real_sleep(seconds)

    monkeypatch.setattr(Path, "is_file", hide_first_published)
    monkeypatch.setattr(coordinator_exec.time, "sleep", sleep_until_published)
    command_pid: int | None = None
    try:
        with pytest.raises(SystemExit) as raised:
            with process_scope():
                run_bounded(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    timeout=30,
                    survive_stop=True,
                    clear_ambient_github=False,
                    **files,
                )
        assert raised.value.code == 128 + signal.SIGTERM
        assert sent["done"] is True
        command_pid = int(pid_path.read_text(encoding="utf-8").strip())
        _wait_until_dead(command_pid)
    finally:
        if command_pid is None and real_is_file(pid_path):
            try:
                command_pid = int(pid_path.read_text(encoding="utf-8").strip())
            except ValueError:
                command_pid = None
        if command_pid is not None:
            try:
                os.killpg(command_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.no_pg
def test_stop_before_pid_publish_kills_the_stopped_starter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pid file is published only after SIGSTOP. The starter is already in
    # its own session, so SIGKILL of the wrapper would leave it stopped.
    source = coordinator_exec._SURVIVE_WRAPPER
    old_import = "import json,os,signal,subprocess,sys\n"
    old_publish = "publish(pid_path, str(proc.pid))\ndata = sys.stdin.buffer.read()\n"
    assert source.count(old_import) == 1
    assert source.count(old_publish) == 1
    monkeypatch.setattr(
        coordinator_exec,
        "_SURVIVE_WRAPPER",
        source.replace(old_import, "import json,os,signal,subprocess,sys,time\n", 1).replace(
            old_publish, "time.sleep(30)\n" + old_publish, 1
        ),
    )
    files = _capture_files(tmp_path / "lanes")
    pid_path = Path(files["pid_path"])
    seen: dict[str, list[int]] = {"pids": []}

    def poke() -> None:
        # A second thread must not accept SIGTERM; the main thread owns the handler.
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not pid_path.exists():
                found = _stopped_descendant_pids(os.getpid())
                if found:
                    seen["pids"] = found
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    thread = threading.Thread(target=poke)
    try:
        with pytest.raises(SystemExit) as raised:
            with process_scope():
                thread.start()
                run_bounded(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    timeout=10,
                    survive_stop=True,
                    clear_ambient_github=False,
                    **files,
                )
        assert raised.value.code == 128 + signal.SIGTERM
        assert seen["pids"]
        assert not pid_path.exists()
        for pid in seen["pids"]:
            _wait_until_dead(pid)
    finally:
        if thread.ident is not None:
            thread.join(timeout=6)
        for pid in seen["pids"]:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.no_pg
def test_capture_record_write_failure_does_not_leave_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = make_worker(tmp_path)
    agent_id = "agent-unwritable"
    paths = coordinator_lanes._lane_capture_paths(worker, TID, agent_id)
    real_replace = Path.replace

    def fail_replace(self: Path, target: Path) -> Path:
        if self.name.endswith(".tmp") and target.name.endswith(".json"):
            raise OSError("read-only record")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    seen: dict[str, int] = {}
    record = lane_start_recorder(worker, TID, agent_id, capture=True)

    def remember(pid: int) -> None:
        seen["pid"] = pid
        record(pid)

    completed = run_bounded(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        timeout=30,
        on_start=remember,
        survive_stop=True,
        clear_ambient_github=False,
        stdout_path=str(paths["stdout"]),
        stderr_path=str(paths["stderr"]),
        status_path=str(paths["status"]),
        pid_path=str(paths["pid"]),
    )
    assert completed.returncode != 124
    assert not lane_process_path(worker, TID, agent_id).exists()
    _wait_until_dead(seen["pid"])


@pytest.mark.no_pg
def test_capture_without_a_start_time_kills_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = make_worker(tmp_path)
    agent_id = "agent-no-lstart"
    paths = coordinator_lanes._lane_capture_paths(worker, TID, agent_id)

    def no_start_time(_pid: int, _configured: object) -> str:
        return ""

    monkeypatch.setattr(coordinator_lanes, "process_start_time", no_start_time)
    seen: dict[str, int] = {}
    record = lane_start_recorder(worker, TID, agent_id, capture=True)

    def remember(pid: int) -> None:
        seen["pid"] = pid
        record(pid)

    completed = run_bounded(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=30,
        on_start=remember,
        survive_stop=True,
        clear_ambient_github=False,
        stdout_path=str(paths["stdout"]),
        stderr_path=str(paths["stderr"]),
        status_path=str(paths["status"]),
        pid_path=str(paths["pid"]),
    )
    assert completed.returncode != 124
    assert not lane_process_path(worker, TID, agent_id).exists()
    _wait_until_dead(seen["pid"])


@pytest.mark.no_pg
def test_stop_during_process_start_keeps_the_recorded_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = make_worker(tmp_path)
    agent_id = "agent-stop-during-ps"
    paths = coordinator_lanes._lane_capture_paths(worker, TID, agent_id)
    started = threading.Event()
    release = threading.Event()
    real_start = coordinator_lanes.process_start_time

    def slow_start(pid: int, configured) -> str:
        started.set()
        if not release.wait(timeout=5):
            raise AssertionError("stop was not sent")
        return real_start(pid, configured)

    monkeypatch.setattr(coordinator_lanes, "process_start_time", slow_start)

    def send_stop() -> None:
        # Keep the signal pending for the masked main thread. A second thread
        # that accepts SIGTERM would run the handler before the record exists.
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
        if not started.wait(timeout=5):
            return
        os.kill(os.getpid(), signal.SIGTERM)
        release.set()

    thread = threading.Thread(target=send_stop)
    command_pid: int | None = None
    wrapper_pid: int | None = None
    thread.start()
    try:
        with pytest.raises(SystemExit) as raised:
            with process_scope():
                run_bounded(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    timeout=30,
                    on_start=lane_start_recorder(
                        worker, TID, agent_id, capture=True
                    ),
                    survive_stop=True,
                    clear_ambient_github=False,
                    stdout_path=str(paths["stdout"]),
                    stderr_path=str(paths["stderr"]),
                    status_path=str(paths["status"]),
                    pid_path=str(paths["pid"]),
                )
        assert raised.value.code == 128 + signal.SIGTERM
        saved = json.loads(
            lane_process_path(worker, TID, agent_id).read_text(encoding="utf-8")
        )
        assert saved.get("capture") is True
        assert str(saved.get("lstart") or "")
        command_pid = int(saved["pid"])
        os.kill(command_pid, 0)
        parent = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(command_pid)],
            check=False,
            capture_output=True,
            text=True,
        )
        wrapper_text = (parent.stdout or "").strip()
        if wrapper_text:
            wrapper_pid = int(wrapper_text)
    finally:
        thread.join(timeout=5)
        if command_pid is not None:
            try:
                os.killpg(command_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if wrapper_pid is not None:
            try:
                os.kill(wrapper_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_stop_during_worker_thread_start_does_not_leave_an_unmarked_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _prepared_store(tmp_path)
    agent_id = "agent-worker-stop"
    working_row(store, worker, agent_id)
    paths = coordinator_lanes._lane_capture_paths(worker, TID, agent_id)
    started = threading.Event()
    release = threading.Event()
    real_start = coordinator_lanes.process_start_time
    holder: dict[str, int] = {}

    def blocked_start(pid: int, configured) -> str:
        holder["pid"] = pid
        started.set()
        if not release.wait(timeout=5):
            raise AssertionError("stop was not sent")
        return real_start(pid, configured)

    monkeypatch.setattr(coordinator_lanes, "process_start_time", blocked_start)
    outcome: dict[str, object] = {}

    def worker_main() -> None:
        try:
            outcome["completed"] = run_bounded(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=30,
                on_start=lane_start_recorder(
                    worker, TID, agent_id, capture=True
                ),
                survive_stop=True,
                clear_ambient_github=False,
                stdout_path=str(paths["stdout"]),
                stderr_path=str(paths["stderr"]),
                status_path=str(paths["status"]),
                pid_path=str(paths["pid"]),
            )
        except BaseException as exc:  # noqa: BLE001 — a stop can surface here
            outcome["error"] = exc

    thread = threading.Thread(target=worker_main)
    command_pid: int | None = None
    try:
        with process_scope():
            thread.start()
            assert started.wait(timeout=5)
            command_pid = holder["pid"]
            with pytest.raises(SystemExit) as raised:
                os.kill(os.getpid(), signal.SIGTERM)
            assert raised.value.code == 128 + signal.SIGTERM
            # Still inside process_start_time: the recorder has not returned.
            # The handler must have killed the command, or a durable record
            # with a start time must already be visible.
            assert command_pid is not None
            record_path = lane_process_path(worker, TID, agent_id)
            deadline = time.monotonic() + 2
            while _pid_alive(command_pid) and time.monotonic() < deadline:
                if record_path.is_file():
                    early = json.loads(record_path.read_text(encoding="utf-8"))
                    if early.get("capture") is True and str(early.get("lstart") or ""):
                        break
                time.sleep(0.05)
            if _pid_alive(command_pid):
                early = json.loads(record_path.read_text(encoding="utf-8"))
                assert early.get("capture") is True
                assert str(early.get("lstart") or "")
        release.set()
        thread.join(timeout=10)
        error = outcome.get("error")
        assert error is None or isinstance(error, SystemExit)
        assert command_pid is not None
        record_path = lane_process_path(worker, TID, agent_id)
        if _pid_alive(command_pid):
            saved = json.loads(record_path.read_text(encoding="utf-8"))
            assert saved.get("capture") is True
            assert str(saved.get("lstart") or "")
            recover_working_lanes(store, replace(worker, lane_timeout=2), task)
            assert (
                store.row("agent", agent_id)["note"] != "recovered: no live process"
            )
        elif record_path.is_file():
            saved = json.loads(record_path.read_text(encoding="utf-8"))
            if saved.get("capture") is True:
                assert str(saved.get("lstart") or "")
    finally:
        release.set()
        thread.join(timeout=10)
        if command_pid is not None:
            try:
                os.killpg(command_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_pr_gate_stop_adopts_both_review_lanes(tmp_path: Path) -> None:
    # Quality and logic run on non-daemon threads. SIGTERM goes to the
    # coordinator process only; both lanes are in other sessions and stay up.
    store, worker, task = _prepared_store(tmp_path)
    head = "c" * 40
    coord(task)["phase"] = "pr_gates_first"
    coord(task)["head_sha"] = head
    coord(task)["base_sha"] = "b" * 40
    store.write(
        "task",
        "update",
        TID,
        {key: value for key, value in task.items() if not str(key).startswith("_")},
    )
    marker = tmp_path / "release-reviews"
    script = tmp_path / "fake_lane.py"
    script.write_text(
        "\n".join(
            [
                "import sys",
                "import time",
                "from pathlib import Path",
                "sys.stdout.write('PARTIAL\\n')",
                "sys.stdout.flush()",
                "deadline = time.time() + 30",
                "while time.time() < deadline and not Path(sys.argv[1]).exists():",
                "    time.sleep(0.05)",
                "sys.stdout.write(Path(sys.argv[2]).read_text(encoding='utf-8'))",
                "sys.stdout.flush()",
                "",
            ]
        ),
        encoding="utf-8",
    )
    stream = tmp_path / "review-stream.txt"
    stream.write_text(
        event_stream_output("STATUS: complete\nRESULT: approved\n"),
        encoding="utf-8",
    )
    child = tmp_path / "pr-gate-stop.py"
    child.write_text(
        "\n".join(
            [
                "import sys",
                "from pathlib import Path",
                "from agent_cli import coordinator_lanes",
                "from agent_cli.coordinator_config import WorkerConfig",
                "from agent_cli.coordinator_exec import process_scope",
                "from agent_cli.store import Store",
                "home, workspace, marker, script, stream_path, head, task_id = sys.argv[1:]",
                "def identity(*_args, **_kwargs):",
                "    return {}",
                "def signed_head(*_args, **_kwargs):",
                "    return head",
                "def review_diff(_store, _worker, task, _runner, *, head):",
                "    directory = Path(workspace) / '.coordinator-control' / str(task['id'])",
                "    directory.mkdir(parents=True, exist_ok=True)",
                "    path = directory / ('review-diff-' + head[:12] + '.patch')",
                "    path.write_text('diff\\n', encoding='utf-8')",
                "    return path",
                "def review_context(*_args, **_kwargs):",
                "    return ''",
                "def provider(*_args, **_kwargs):",
                "    return [sys.executable, '-u', script, marker, stream_path]",
                "coordinator_lanes.verify_checkout_identity = identity",
                "coordinator_lanes.verify_signed_clean_head = signed_head",
                "coordinator_lanes.write_review_diff = review_diff",
                "coordinator_lanes.write_review_context = review_context",
                "coordinator_lanes.lane_argv = provider",
                "store = Store(Path(home))",
                "task = store.row('task', task_id)",
                "assert task is not None",
                "worker = WorkerConfig(",
                "    session_id='worker-session',",
                "    review_session='review-session',",
                "    workspace_root=Path(workspace),",
                "    repositories={},",
                "    reply_logins=(),",
                "    poll_seconds=30,",
                "    lane_timeout=60,",
                "    check_timeout=30,",
                ")",
                "def runner(*_args, **_kwargs):",
                "    raise AssertionError('phase_pr_gates must not call the runner')",
                "with process_scope():",
                "    coordinator_lanes.phase_pr_gates(",
                "        store, worker, task, runner, None, vendor='driver-a', stage='review-1',",
                "    )",
                "",
            ]
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(Path(__file__).resolve().parents[1] / "src")
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )
    owner = subprocess.Popen(
        [
            sys.executable,
            str(child),
            str(store.home),
            str(worker.workspace_root),
            str(marker),
            str(script),
            str(stream),
            head,
            TID,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    command_pids: list[int] = []
    lanes_dir = control_dir(worker, TID) / "lanes"
    try:
        ready: list[dict] = []
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and owner.poll() is None:
            ready = _durable_partial_lanes(lanes_dir)
            if len(ready) == 2:
                break
            time.sleep(0.05)
        if len(ready) != 2 or owner.poll() is not None:
            output, error = owner.communicate(timeout=5)
            raise AssertionError(
                f"lanes={ready!r} rc={owner.returncode} out={output!r} err={error!r}"
            )
        command_pids = [item["pid"] for item in ready]
        assert len(set(command_pids)) == 2
        assert not marker.exists()
        owner.send_signal(signal.SIGTERM)
        output, error = owner.communicate(timeout=5)
        assert owner.returncode == 128 + signal.SIGTERM, (output, error)
        assert not marker.exists()
        for item in ready:
            record = item["record"]
            pid = item["pid"]
            saved_process = json.loads(record.read_text(encoding="utf-8"))
            assert saved_process.get("capture") is True
            assert saved_process.get("pid") == pid
            assert record.with_suffix(".stdout").is_file()
            assert record.with_suffix(".stderr").is_file()
            assert record.with_suffix(".pid").is_file()
            assert _pid_alive(pid)
        pending = [
            row
            for row in store.rows("agent")
            if row.get("task_id") == TID and row.get("status") == "working"
        ]
        assert len(pending) == 2
        assert {row["role"] for row in pending} == {
            "pr-reviewer-quality",
            "pr-reviewer-logic",
        }
        marker.write_text("go", encoding="utf-8")
        current = store.row("task", TID)
        assert current is not None
        assert recover_working_lanes(store, worker, current) == "none"
        saved_agents = [
            row for row in store.rows("agent") if row.get("task_id") == TID
        ]
        assert len(saved_agents) == 2
        for row in saved_agents:
            assert row["status"] == "done"
            assert row["note"] != "recovered: no live process"
        outcome = coord(current)["lane_outcome"]
        assert outcome["role"] == "reviewer-results"
        recorded = outcome["outcomes"]
        assert {item["dimension"] for item in recorded} == {"quality", "logic"}
        for item in recorded:
            assert item["status"] == "complete"
            assert item["result"] == "approved"
            assert item["stage"] == "review-1"
        assert len(_lane_runs(store)) == 2
        assert recover_working_lanes(store, worker, current) == "none"
        assert len(_lane_runs(store)) == 2
        assert "lane-failed" not in json.dumps(coord(current))
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.communicate(timeout=5)
        for pid in command_pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_cancel_still_kills_a_recorded_lane(tmp_path: Path) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_order_worker(tmp_path)
    task = seed_order_task(store, worker, TID, phase="implement")
    working_row(store, worker, "agent-cancel")
    intent_id = insert_order_intent(store, worker, TID, "cancel", reason="stop")

    def runner(*_args, **_kwargs):
        raise AssertionError("cancel must not call the runner")

    with sleeping_process() as proc:
        lane_start_recorder(worker, TID, "agent-cancel")(proc.pid)
        assert lane_process_state(worker, TID, "agent-cancel") == "alive"
        lines = consume_order_intents(
            store, worker, task, runner, [store.row("activity", intent_id)]
        )
        assert lines == [f"order cancelled {TID}"]
        proc.wait(timeout=5)
        assert proc.returncode is not None and proc.returncode < 0
    saved = store.row("task", TID)
    assert saved["state"] == "failed"
    assert coord(saved)["failed_kind"] == "cancelled"
    assert store.row("agent", "agent-cancel")["status"] == "working"
    assert _lane_runs(store) == []


def _result_event(text: str) -> str:
    """A one-event stream whose terminal result is ``text``."""
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text})


def _placeholder_lane_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    *,
    accounts: dict | None = None,
):
    store = Store(tmp_path)
    write_accounts(store.home)
    write_placeholder_accounts(store.home, accounts)
    write_drivers(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    task = seed_lane_task(store, worker, models={"first": first, "second": "driver-b"})
    monkeypatch.setattr(
        coordinator_lanes, "verify_checkout_identity", lambda *a, **k: {}
    )
    return store, worker, task


@pytest.mark.parametrize(
    ("driver", "tail"),
    [
        ("driver-a", ["--edit", "--cwd", "WORKTREE", "--no-shell", "--deny", "Shell"]),
        ("driver-c", ["--tools", "Read,Edit", "--mode", "edit"]),
    ],
)
def test_implementer_lane_appends_the_configured_write_hardening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver: str, tail: list[str]
) -> None:
    store, worker, task = _placeholder_lane_env(tmp_path, monkeypatch, driver)
    seen: list[tuple[list[str], str | None]] = []

    def lane(argv: list[str], stdin: str | None) -> Completed:
        seen.append((list(argv), stdin))
        return Completed(0, _result_event("STATUS: complete\nRESULT: done"), "")

    _agent, result = launch_lane(
        store,
        worker,
        task,
        role="implementer",
        vendor=driver,
        round_num=1,
        spec_body="Implement.",
        runner=lambda argv: Completed(0, "", ""),
        lane_runner=lane,
    )
    assert result.status == "complete"
    argv, stdin = seen[0]
    worktree = str(coord(task)["worktree"])
    assert argv[-len(tail) :] == [worktree if item == "WORKTREE" else item for item in tail]
    # A file-argument driver reads its spec from the file; a stdin driver gets the text.
    assert (stdin is None) == (driver == "driver-a")


def test_lane_refuses_a_worktree_inside_the_key_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = accounts_document()
    accounts["accounts"]["driver-d-account"]["config_dir"] = str(tmp_path)
    store, worker, task = _placeholder_lane_env(
        tmp_path, monkeypatch, "driver-d", accounts=accounts
    )
    with pytest.raises(CoordinatorError, match="driver-d worktree overlaps account config_dir"):
        launch_lane(
            store,
            worker,
            task,
            role="implementer",
            vendor="driver-d",
            round_num=1,
            spec_body="Implement.",
            runner=lambda argv: Completed(0, "", ""),
            lane_runner=lambda argv, stdin: Completed(0, "", ""),
        )
    assert not [row for row in store.rows("agent") if row.get("task_id") == TID]
