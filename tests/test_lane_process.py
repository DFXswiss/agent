"""Lane process records and recovery of abandoned working rows."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
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
from agent_cli.coordinator_orders import consume_order_intents
from agent_cli.store import Store
from test_coordinator_support import (
    grok_messages_stream,
    insert_order_intent,
    make_order_worker,
    make_session,
    make_worker,
    seed_order_task,
    write_accounts,
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
                "import sys; sys.stdout.write(sys.argv[1])",
                grok_messages_stream("STATUS: complete\nRESULT: approved"),
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
sys.stdout.write(sys.argv[2] if seen else sys.argv[3])
"""
    seen_stream = grok_messages_stream("RECORD-SEEN\nSTATUS: complete\nRESULT: done")
    missing_stream = grok_messages_stream("RECORD-MISSING\nSTATUS: partial")

    def provider(_vendor: str, **_kwargs) -> list[str]:
        return [
            sys.executable,
            "-c",
            script,
            str(control_dir(worker, TID) / "lanes"),
            seen_stream,
            missing_stream,
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
            0, grok_messages_stream("STATUS: complete\nRESULT: done\n"), ""
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
    return grok_messages_stream(text)


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
