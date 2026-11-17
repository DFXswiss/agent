"""Complete coverage of the coordinator lane functions the account-pool change touched.

Covers ``_read_reviewer_spool``, ``_finish_captured_lane``, ``launch_lane`` and
``_prepare_pr_review_agent`` in ``coordinator_lanes`` and
``_execution_binding_fields`` in ``coordinator_runtime``. Every test runs the
real function against the real PostgreSQL store. Only the model provider (the
injected lane runner), the git checkout check, and a store write that has to
fail are replaced.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_cli import coordinator_lanes
from agent_cli.account_pool import LaneAccountChoice
from agent_cli.ai_accounts import LANE_EXECUTION_ERROR, load_ai_accounts
from agent_cli.coordinator_common import (
    CoordinatorError,
    LaneFailedError,
    MalformedSurfaceCheckpoint,
    coord,
    save_task,
    strip_row,
)
from agent_cli.coordinator_lanes import (
    REVIEWER_SPOOL_WRITE_ERROR,
    _prepare_pr_review_agent,
    launch_lane,
)
from agent_cli.coordinator_runtime import _execution_binding_fields
from agent_cli.model_slots import TASK_MODELS_NOT_CONFIGURED
from agent_cli.runtime import Completed
from agent_cli.store import Store, StoreError
from driver_fixtures import accounts_document
from test_coordinator_support import (
    event_stream_output,
    make_session,
    make_worker,
    write_accounts,
    write_check_lane,
)
from test_lane_process import (
    TID,
    _placeholder_lane_env,
    _pool_lane_env,
    _result,
    _write_lane_capture,
    _write_spool,
    seed_lane_task,
    working_row,
)

HEAD = "a" * 40
APPROVED = "STATUS: complete\nRESULT: approved"


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **seed: Any):
    """Store, worker and a seeded implement task with an existing worktree."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    task = seed_lane_task(store, worker, **seed)
    monkeypatch.setattr(
        coordinator_lanes, "verify_checkout_identity", lambda *a, **k: {}
    )
    return store, worker, task


def _drop_lane_execution(home: Path) -> None:
    path = home / "ai-accounts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["lane_execution"]
    path.write_text(json.dumps(data), encoding="utf-8")


def _task_agents(store: Store) -> list[dict[str, Any]]:
    return [row for row in store.rows("agent") if row.get("task_id") == TID]


def _lane_runs(store: Store, agent_id: str) -> list[dict[str, Any]]:
    return [
        row["payload"]
        for row in store.rows("activity")
        if row.get("type") == "lane.run"
        and (row.get("payload") or {}).get("agent_id") == agent_id
    ]


def _no_lane(_argv: list[str], _stdin: str | None) -> Completed:
    raise AssertionError("the lane must not start")


def _launch(
    store: Store,
    worker,
    task: dict[str, Any],
    *,
    role: str,
    lane_runner,
    vendor: str = "driver-a",
    round_num: int | None = 1,
):
    return launch_lane(
        store,
        worker,
        task,
        role=role,
        vendor=vendor,
        round_num=round_num,
        spec_body="Do the step.",
        runner=lambda argv: Completed(0, "", ""),
        lane_runner=lane_runner,
    )


def _stored_c(store: Store) -> dict[str, Any]:
    saved = store.row("task", TID)
    assert saved is not None
    return coord(saved)


# --- _read_reviewer_spool ---------------------------------------------------


def test_reviewer_spool_is_refused_for_every_mismatching_field(tmp_path: Path) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    worker = make_worker(tmp_path)
    agent_id = "agent-spool"
    task = {"id": TID}
    agent = {"id": agent_id, "role": "pr-reviewer-quality", "vendor": "driver-a"}
    identity = {
        "round_num": None,
        "head": HEAD,
        "dimension": "quality",
        "stage": "review-1",
    }
    path = coordinator_lanes._write_reviewer_spool(
        store,
        worker,
        task,
        agent,
        _result("pr-reviewer-quality", "driver-a"),
        output_file=None,
        **identity,
    )
    original = json.loads(path.read_text(encoding="utf-8"))
    owned_agent = {
        **agent,
        "reviewer_output": coordinator_lanes._reviewer_output_checkpoint(agent_id),
    }
    owned_path = coordinator_lanes._owned_reviewer_output(
        store, worker, TID, owned_agent
    )
    assert owned_path is not None

    def read(row: dict[str, Any] = agent):
        return coordinator_lanes._read_reviewer_spool(
            store, worker, task, row, **identity
        )

    def changed(mutate, row: dict[str, Any] = agent):
        payload = copy.deepcopy(original)
        mutate(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return read(row)

    recovered = read()
    assert recovered is not None
    result, output_file, _excerpt = recovered
    assert (result.role, result.vendor, result.protocol_result) == (
        "pr-reviewer-quality",
        "driver-a",
        "approved",
    )
    assert output_file is None

    # A spool that is not a JSON object.
    path.write_text(json.dumps([original]), encoding="utf-8")
    assert read() is None
    # A spool of another step or an unknown version.
    assert changed(lambda p: p.update(head="b" * 40)) is None
    assert changed(lambda p: p.update(version=1)) is None
    # A saved result that is not an object, or whose fields have the wrong type.
    assert changed(lambda p: p.update(result="approved")) is None
    assert changed(lambda p: p["result"].update(stdout=7)) is None
    assert changed(lambda p: p["result"].update(returncode=True)) is None
    # A saved result of another role or vendor than the agent row.
    assert changed(lambda p: p["result"].update(role="pr-reviewer-logic")) is None
    assert changed(lambda p: p["result"].update(vendor="driver-b")) is None
    # An output file name that is not a string.
    assert changed(lambda p: p.update(output_file=7)) is None
    # An output file for an agent that owns none.
    assert changed(lambda p: p.update(output_file=f"{agent_id}.output")) is None
    # An agent that owns an output file: only its own name is accepted.
    assert changed(lambda p: p.update(output_file="other.output"), owned_agent) is None
    assert changed(lambda p: p.update(output_file=None), owned_agent) is None
    # The legacy version stored the full path, the current one the file name.
    assert (
        changed(
            lambda p: p.update(version=2, output_file=Path(owned_path).name),
            owned_agent,
        )
        is None
    )
    legacy = changed(
        lambda p: p.update(version=2, output_file=owned_path), owned_agent
    )
    assert legacy is not None
    assert legacy[1] == owned_path
    assert legacy[0].protocol_result == "approved"
    current = changed(
        lambda p: p.update(output_file=Path(owned_path).name), owned_agent
    )
    assert current is not None
    assert current[1] == owned_path


# --- _execution_binding_fields ----------------------------------------------


def test_execution_binding_refuses_an_owner_that_is_no_longer_configured(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    worker = replace(make_worker(tmp_path), repository_owners=("example",))
    ai = load_ai_accounts(store.home)
    config = worker.repositories["example/project"]

    with pytest.raises(CoordinatorError) as excinfo:
        _execution_binding_fields(
            store,
            worker,
            "other-owner/project",
            ai,
            repository_config=config,
            owner_scoped=True,
        )
    assert str(excinfo.value) == (
        "repository owner other-owner is no longer configured"
    )

    with pytest.raises(CoordinatorError) as excinfo:
        _execution_binding_fields(
            store,
            worker,
            "project",
            ai,
            repository_config=config,
            owner_scoped=True,
        )
    assert str(excinfo.value) == "repository owner ? is no longer configured"

    # Without the owner requirement the same owner is bound, not refused.
    fields = _execution_binding_fields(
        store,
        worker,
        "other-owner/project",
        ai,
        repository_config=config,
        owner_scoped=True,
        require_owner=False,
    )
    assert fields["repository_owner"] == "other-owner"


# --- _prepare_pr_review_agent -----------------------------------------------


def _prepare(store: Store, worker, task: dict[str, Any], **overrides: Any):
    arguments = {
        "role": "pr-reviewer-quality",
        "vendor": "driver-a",
        "stage": "review-1",
        "head": HEAD,
        "spec_body": "Review this.",
    }
    arguments.update(overrides)
    return _prepare_pr_review_agent(store, worker, task, **arguments)


def test_pr_review_preparation_requires_a_task_model_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    task["models"] = None
    with pytest.raises(CoordinatorError) as excinfo:
        _prepare(store, worker, task)
    assert str(excinfo.value) == TASK_MODELS_NOT_CONFIGURED
    assert _task_agents(store) == []


def test_pr_review_preparation_requires_the_pinned_vendor_of_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    with pytest.raises(CoordinatorError) as excinfo:
        _prepare(store, worker, task, vendor="driver-b")
    assert str(excinfo.value) == (
        "stage review-1 requires vendor driver-a (model slot pinned on the task)"
    )
    assert _task_agents(store) == []


def test_pr_review_preparation_refuses_an_open_lane_of_an_earlier_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    working_row(
        store,
        worker,
        "agent-open",
        role="pr-reviewer-quality",
        round=None,
        head_sha=HEAD,
        dimension="quality",
        stage="review-1",
    )
    with pytest.raises(LaneFailedError) as excinfo:
        _prepare(store, worker, task)
    assert str(excinfo.value) == "a lane record of an earlier tick is still open"
    assert [row["id"] for row in _task_agents(store)] == ["agent-open"]


def test_pr_review_preparation_reports_a_pool_without_a_usable_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logins = tmp_path / "logins"
    config_dirs = {seat: str(logins / seat) for seat in ("seat-1", "seat-2")}
    store, worker, task = _pool_lane_env(tmp_path, monkeypatch, config_dirs)
    with pytest.raises(CoordinatorError) as excinfo:
        _prepare(store, worker, task, vendor="driver-c")
    assert str(excinfo.value) == (
        "Role pooled-reader has no usable pool account "
        "(seat-1: no login; seat-2: no login)"
    )
    assert _task_agents(store) == []


def test_pr_review_preparation_refuses_a_chosen_role_that_is_not_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ai.for_lane already refuses a writable binding for a PR reviewer kind.
    # Only an account choice whose role is no longer read-only reaches this
    # guard, which refuses it before any agent row is written.
    store, worker, task = _env(tmp_path, monkeypatch)
    resolved: list[Any] = []

    def writable_choice(
        _store: Store, _ai: Any, role: Any, *, now: Any
    ) -> LaneAccountChoice:
        resolved.append(role)
        return LaneAccountChoice(
            role=replace(role, access="workspace-write"),
            load=None,
            running_lanes=None,
        )

    monkeypatch.setattr(coordinator_lanes, "select_lane_account", writable_choice)
    with pytest.raises(CoordinatorError) as excinfo:
        _prepare(store, worker, task)
    assert str(excinfo.value) == "PR review lane requires read-only access"
    (role,) = resolved
    assert role.access == "read-only"
    assert _task_agents(store) == []


def test_pr_review_preparation_refuses_a_worktree_inside_the_key_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = accounts_document()
    accounts["accounts"]["driver-d-account"]["config_dir"] = str(tmp_path)
    store, worker, task = _placeholder_lane_env(
        tmp_path, monkeypatch, "driver-d", accounts=accounts
    )
    with pytest.raises(
        CoordinatorError, match="driver-d worktree overlaps account config_dir"
    ):
        _prepare(store, worker, task, vendor="driver-d")
    assert _task_agents(store) == []


def test_pr_review_preparation_requires_lane_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    _drop_lane_execution(store.home)
    with pytest.raises(CoordinatorError) as excinfo:
        _prepare(store, worker, task)
    assert str(excinfo.value) == LANE_EXECUTION_ERROR
    assert _task_agents(store) == []


def test_pr_review_preparation_without_an_output_file_reraises_a_build_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)

    def broken_template(*_args: Any, **_kwargs: Any) -> list[str]:
        raise ValueError("lane argv template is broken")

    monkeypatch.setattr(coordinator_lanes, "lane_argv", broken_template)
    with pytest.raises(ValueError, match="lane argv template is broken"):
        _prepare(store, worker, task)
    assert _task_agents(store) == []
    # driver-a has no output file, so no reviewer output was ever created.
    assert not (store.home / "coordinator-reviewer-spools").exists()


# --- launch_lane -------------------------------------------------------------


def test_launch_requires_a_task_model_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    task["models"] = None
    with pytest.raises(CoordinatorError) as excinfo:
        _launch(store, worker, task, role="implementer", lane_runner=_no_lane)
    assert str(excinfo.value) == TASK_MODELS_NOT_CONFIGURED
    assert _task_agents(store) == []


def test_launch_refuses_a_missing_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    coord(task)["worktree"] = str(tmp_path / "missing-worktree")
    with pytest.raises(CoordinatorError) as excinfo:
        _launch(store, worker, task, role="implementer", lane_runner=_no_lane)
    assert str(excinfo.value) == "worktree missing; cannot launch lane"
    assert _task_agents(store) == []


def test_launch_requires_lane_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    _drop_lane_execution(store.home)
    with pytest.raises(CoordinatorError) as excinfo:
        _launch(store, worker, task, role="implementer", lane_runner=_no_lane)
    assert str(excinfo.value) == LANE_EXECUTION_ERROR
    assert _task_agents(store) == []
    assert "lane" not in _stored_c(store)


def _open_reviewer(store: Store, worker, *, owned_output: bool) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "round": 1,
        "head_sha": HEAD,
        "dimension": "inner",
        "stage": "inner-review",
    }
    if owned_output:
        fields["reviewer_output"] = coordinator_lanes._reviewer_output_checkpoint(
            "agent-open"
        )
    return working_row(
        store, worker, "agent-open", role="reviewer", vendor="driver-a", **fields
    )


def test_launch_returns_the_saved_reviewer_result_when_its_checkpoint_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    existing = _open_reviewer(store, worker, owned_output=False)
    spool = _write_spool(
        store,
        worker,
        task,
        existing,
        round_num=1,
        head=HEAD,
        dimension="inner",
        stage="inner-review",
    )

    def failing_audit(*_args: Any, **_kwargs: Any) -> str:
        raise StoreError("audit write failed")

    monkeypatch.setattr(coordinator_lanes, "record_lane_run", failing_audit)
    agent, result = _launch(store, worker, task, role="reviewer", lane_runner=_no_lane)
    assert agent["id"] == "agent-open"
    assert result.protocol_result == "approved"
    assert result.status == "complete"
    # The checkpoint rolled back: the row stays open and the spool stays for a retry.
    assert store.row("agent", "agent-open")["status"] == "working"
    assert spool.exists()
    assert "lane_outcome" not in _stored_c(store)


def test_launch_discards_an_unreadable_reviewer_spool_after_a_failed_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    existing = _open_reviewer(store, worker, owned_output=True)
    output = coordinator_lanes._owned_reviewer_output(store, worker, TID, existing)
    assert output is not None
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(f"{APPROVED}\n", encoding="utf-8")
    spool = coordinator_lanes._write_reviewer_spool(
        store,
        worker,
        task,
        existing,
        _result("reviewer", "driver-a"),
        round_num=1,
        head=HEAD,
        dimension="inner",
        stage="inner-review",
        output_file=output,
    )

    def corrupt_then_fail(*_args: Any, **_kwargs: Any) -> str:
        # The spool became unreadable before the store write failed.
        spool.write_text("not JSON", encoding="utf-8")
        raise StoreError("audit write failed")

    monkeypatch.setattr(coordinator_lanes, "record_lane_run", corrupt_then_fail)
    with pytest.raises(StoreError) as excinfo:
        _launch(store, worker, task, role="reviewer", lane_runner=_no_lane)
    assert str(excinfo.value) == "audit write failed"
    assert not spool.exists()
    assert not Path(output).exists()
    assert store.row("agent", "agent-open")["status"] == "working"


def test_launch_without_a_saved_reviewer_result_discards_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    existing = _open_reviewer(store, worker, owned_output=True)
    output = coordinator_lanes._owned_reviewer_output(store, worker, TID, existing)
    assert output is not None
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text("partial output\n", encoding="utf-8")
    with pytest.raises(LaneFailedError) as excinfo:
        _launch(store, worker, task, role="reviewer", lane_runner=_no_lane)
    assert str(excinfo.value) == "a lane record of an earlier tick is still open"
    assert not Path(output).exists()
    assert [row["id"] for row in _task_agents(store)] == ["agent-open"]
    assert store.row("agent", "agent-open")["status"] == "working"


def test_interrupted_reviewer_lane_keeps_a_row_another_step_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    closed: list[str] = []

    def provider_crash(_argv: list[str], _stdin: str | None) -> Completed:
        (row,) = [
            row for row in _task_agents(store) if row.get("status") == "working"
        ]
        done = copy.deepcopy(row)
        done["status"] = "done"
        done["note"] = "closed by another step"
        store.write("agent", "update", str(row["id"]), strip_row(done))
        closed.append(str(row["id"]))
        raise RuntimeError("provider crashed")

    with pytest.raises(LaneFailedError) as excinfo:
        _launch(store, worker, task, role="reviewer", lane_runner=provider_crash)
    assert str(excinfo.value) == "lane interrupted: provider crashed"
    (aid,) = closed
    saved = store.row("agent", aid)
    assert saved["status"] == "done"
    # The row another step closed is not rewritten as interrupted.
    assert saved["note"] == "closed by another step"
    assert _stored_c(store)["lane"] == {"agent_id": aid, "state": "failed"}
    assert coord(task)["lane"] == {"agent_id": aid, "state": "failed"}
    assert not coordinator_lanes._reviewer_spool_path(store, worker, TID, aid).exists()


def test_coordinator_stop_during_a_lane_leaves_it_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    forgotten: list[tuple[str, str]] = []

    def stopped(_argv: list[str], _stdin: str | None) -> Completed:
        raise SystemExit("coordinator stopped")

    def recording_forget(_worker, task_id: str, agent_id: str) -> None:
        forgotten.append((task_id, agent_id))

    monkeypatch.setattr(coordinator_lanes, "forget_lane_process", recording_forget)
    with pytest.raises(SystemExit) as excinfo:
        _launch(store, worker, task, role="implementer", lane_runner=stopped)
    assert excinfo.type is SystemExit
    assert str(excinfo.value) == "coordinator stopped"
    # The lane process is not forgotten: a later coordinator adopts it.
    assert forgotten == []
    (row,) = _task_agents(store)
    # A later coordinator adopts the lane: its row and running marker stay.
    assert row["status"] == "working"
    assert _stored_c(store)["lane"] == {
        "agent_id": row["id"],
        "role": "implementer",
        "vendor": "driver-a",
        "state": "running",
    }


def test_launch_failing_before_the_lane_starts_releases_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # driver-b writes its result to an output file, which an implementer lane
    # creates before its argv is built. An error that is no SystemExit still
    # forgets the lane process and removes that file.
    store, worker, task = _placeholder_lane_env(tmp_path, monkeypatch, "driver-a")
    outputs: list[Path] = []
    forgotten: list[tuple[str, str]] = []
    real_forget = coordinator_lanes.forget_lane_process

    def broken_template(*_args: Any, **kwargs: Any) -> list[str]:
        output = Path(kwargs["output_file"])
        assert output.is_file()
        outputs.append(output)
        raise ValueError("lane argv template is broken")

    def recording_forget(worker_arg, task_id: str, agent_id: str) -> None:
        forgotten.append((task_id, agent_id))
        real_forget(worker_arg, task_id, agent_id)

    monkeypatch.setattr(coordinator_lanes, "lane_argv", broken_template)
    monkeypatch.setattr(coordinator_lanes, "forget_lane_process", recording_forget)
    with pytest.raises(ValueError) as excinfo:
        _launch(
            store,
            worker,
            task,
            role="implementer",
            vendor="driver-b",
            lane_runner=_no_lane,
        )
    assert str(excinfo.value) == "lane argv template is broken"
    (output,) = outputs
    assert not output.exists()
    (released,) = forgotten
    assert released[0] == TID
    assert output == coordinator_lanes._lane_capture_paths(
        worker, TID, released[1]
    )["provider"]
    assert _task_agents(store) == []
    assert "lane" not in _stored_c(store)


def test_coordinator_stop_while_spooling_a_reviewer_result_is_not_reworded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    seen: list[str] = []

    def stopped(store_arg, worker_arg, task_arg, agent_arg, _result, **_kwargs):
        spool = coordinator_lanes._reviewer_spool_path(
            store_arg, worker_arg, str(task_arg["id"]), str(agent_arg["id"])
        )
        spool.parent.mkdir(parents=True, exist_ok=True)
        spool.with_suffix(".tmp").write_text("{}", encoding="utf-8")
        seen.append(str(agent_arg["id"]))
        raise SystemExit("coordinator stopped")

    monkeypatch.setattr(coordinator_lanes, "_write_reviewer_spool", stopped)
    with pytest.raises(SystemExit) as excinfo:
        _launch(
            store,
            worker,
            task,
            role="reviewer",
            lane_runner=lambda _argv, _stdin: Completed(
                0, event_stream_output(APPROVED), ""
            ),
        )
    assert excinfo.type is SystemExit
    assert str(excinfo.value) == "coordinator stopped"
    (aid,) = seen
    spool = coordinator_lanes._reviewer_spool_path(store, worker, TID, aid)
    assert not spool.with_suffix(".tmp").exists()
    assert not spool.exists()
    assert store.row("agent", aid)["status"] == "working"
    assert "lane_outcome" not in _stored_c(store)


def test_launch_of_another_lane_role_records_a_finished_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    agent, result = _launch(
        store,
        worker,
        task,
        role="pr-reviewer-quality",
        round_num=None,
        lane_runner=lambda _argv, _stdin: Completed(
            0, event_stream_output(APPROVED), ""
        ),
    )
    assert (result.status, result.protocol_result) == ("complete", "approved")
    saved = store.row("agent", agent["id"])
    assert saved["status"] == "done"
    assert saved["finished_at"] is not None
    assert "RESULT: approved" in saved["note"]
    assert agent["status"] == "done"
    expected_lane = {
        "agent_id": agent["id"],
        "role": "pr-reviewer-quality",
        "vendor": "driver-a",
        "state": "finished",
        "status": "complete",
        "result": "approved",
    }
    assert _stored_c(store)["lane"] == expected_lane
    assert coord(task)["lane"] == expected_lane
    assert "lane_outcome" not in _stored_c(store)
    runs = _lane_runs(store, agent["id"])
    assert len(runs) == 1
    assert runs[0]["role"] == "pr-reviewer-quality"


# --- _finish_captured_lane ---------------------------------------------------


def _finish(store: Store, worker, task: dict[str, Any], row: dict[str, Any]):
    return coordinator_lanes._finish_captured_lane(
        store, worker, task, row, returncode=0, timed_out=False
    )


def test_captured_lane_without_a_working_row_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    before = copy.deepcopy(_stored_c(store))
    assert _finish(store, worker, task, {"id": "agent-gone"}) is None
    row = working_row(store, worker, "agent-done", status="done")
    assert _finish(store, worker, task, row) is None
    assert store.row("agent", "agent-done")["status"] == "done"
    # No lane marker or outcome was checkpointed on the task.
    assert _stored_c(store) == before
    assert _lane_runs(store, "agent-done") == []


def test_captured_lane_of_an_unknown_role_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    row = working_row(store, worker, "agent-check", role="check")
    with pytest.raises(CoordinatorError) as excinfo:
        _finish(store, worker, task, row)
    assert str(excinfo.value) == "captured lane role is unknown"
    assert store.row("agent", "agent-check")["status"] == "working"


def test_captured_lane_without_its_worktree_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    row = working_row(store, worker, "agent-1")
    coord(task)["worktree"] = str(tmp_path / "missing-worktree")
    with pytest.raises(CoordinatorError) as excinfo:
        _finish(store, worker, task, row)
    assert str(excinfo.value) == "worktree missing; cannot launch lane"
    assert store.row("agent", "agent-1")["status"] == "working"


def test_captured_lane_without_its_output_streams_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    # No capture file was ever written for this lane.
    row = working_row(store, worker, "agent-1")
    _finish(store, worker, task, row)
    saved = store.row("agent", "agent-1")
    assert saved["status"] == "done"
    assert saved["note"] == "lane output file is unavailable"
    outcome = _stored_c(store)["lane_outcome"]
    assert outcome["status"] == "unavailable"
    assert outcome["stdout"] == ""
    assert outcome["applied"] is False
    runs = _lane_runs(store, "agent-1")
    assert len(runs) == 1
    assert runs[0]["status"] == "unavailable"


def _driver_b_implementer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A captured ``driver-b`` implementer, whose driver writes an output file."""
    store, worker, task = _placeholder_lane_env(tmp_path, monkeypatch, "driver-a")
    row = working_row(store, worker, "agent-1", vendor="driver-b")
    _write_lane_capture(worker, "agent-1", "")
    provider = coordinator_lanes._lane_capture_paths(worker, TID, "agent-1")[
        "provider"
    ]
    return store, worker, task, row, provider


def test_captured_lane_reads_its_provider_output_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task, row, provider = _driver_b_implementer(tmp_path, monkeypatch)
    provider.write_text("STATUS: complete\nRESULT: done\n", encoding="utf-8")
    _finish(store, worker, task, row)
    assert store.row("agent", "agent-1")["status"] == "done"
    outcome = _stored_c(store)["lane_outcome"]
    assert (outcome["vendor"], outcome["status"], outcome["result"]) == (
        "driver-b",
        "complete",
        "done",
    )
    assert "RESULT: done" in outcome["stdout"]
    # The provider output belongs to the capture and is removed with it.
    assert not provider.exists()


def test_captured_lane_with_an_unreadable_provider_output_file_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task, row, provider = _driver_b_implementer(tmp_path, monkeypatch)
    assert not provider.exists()
    _finish(store, worker, task, row)
    saved = store.row("agent", "agent-1")
    assert saved["status"] == "done"
    assert saved["note"] == "lane output file is unavailable"
    outcome = _stored_c(store)["lane_outcome"]
    assert (outcome["status"], outcome["result"], outcome["stdout"]) == (
        "unavailable",
        "",
        "",
    )


_MALFORMED_SURFACE = "STATUS: complete\nRESULT: done\nREUSED: none — nothing reused\n"


def _assert_checkpoint_failed(store: Store, agent_id: str) -> dict[str, Any]:
    saved = store.row("agent", agent_id)
    assert saved["status"] == "done"
    assert saved["note"] == "implementer checkpoint redaction failed"
    c = _stored_c(store)
    assert c["lane"] == {
        "agent_id": agent_id,
        "role": "implementer",
        "vendor": "driver-a",
        "state": "checkpoint-failed",
    }
    assert "lane_outcome" not in c
    return c


def test_captured_malformed_surface_of_an_earlier_round_opens_no_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch, current_round=1)
    row = working_row(store, worker, "agent-1", round=2)
    _write_lane_capture(worker, "agent-1", event_stream_output(_MALFORMED_SURFACE))
    with pytest.raises(
        MalformedSurfaceCheckpoint, match="implementer surface checkpoint is malformed"
    ):
        _finish(store, worker, task, row)
    c = _assert_checkpoint_failed(store, "agent-1")
    assert "surface_repair" not in c
    assert store.row("task", TID)["current_round"] == 1
    paths = coordinator_lanes._lane_capture_paths(worker, TID, "agent-1")
    assert not paths["stdout"].exists()


def test_captured_malformed_surface_without_repair_budget_opens_no_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch, current_round=1)
    spent = {"round": 1, "attempts": 2, "rule": "earlier", "line": 1}
    coord(task)["surface_repair"] = spent
    save_task(store, task)
    row = working_row(store, worker, "agent-1", round=1)
    _write_lane_capture(worker, "agent-1", event_stream_output(_MALFORMED_SURFACE))
    with pytest.raises(
        MalformedSurfaceCheckpoint, match="implementer surface checkpoint is malformed"
    ):
        _finish(store, worker, task, row)
    c = _assert_checkpoint_failed(store, "agent-1")
    # The spent repair state is kept; no next round is opened.
    assert c["surface_repair"] == spent
    assert store.row("task", TID)["current_round"] == 1


def test_captured_implementer_whose_checkpoint_redaction_fails_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _env(tmp_path, monkeypatch)
    row = working_row(store, worker, "agent-1")
    _write_lane_capture(
        worker, "agent-1", event_stream_output("STATUS: complete\nRESULT: done")
    )

    def not_schema_safe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise CoordinatorError("implementer checkpoint redaction is not schema-safe")

    monkeypatch.setattr(
        coordinator_lanes, "implementer_replay_fields", not_schema_safe
    )
    with pytest.raises(CoordinatorError) as excinfo:
        _finish(store, worker, task, row)
    assert str(excinfo.value) == "implementer checkpoint redaction is not schema-safe"
    _assert_checkpoint_failed(store, "agent-1")
    paths = coordinator_lanes._lane_capture_paths(worker, TID, "agent-1")
    assert not paths["stdout"].exists()


def _captured_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, head: str = HEAD
):
    store, worker, task = _env(tmp_path, monkeypatch, head=head)
    row = working_row(
        store,
        worker,
        "agent-review",
        role="pr-reviewer-quality",
        round=None,
        head_sha=head,
        dimension="quality",
        stage="review-1",
    )
    _write_lane_capture(worker, "agent-review", event_stream_output(APPROVED))
    return store, worker, task, row


def test_captured_reviewer_whose_spool_write_fails_names_the_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without a head the spool identity is invalid, so the spool write fails.
    store, worker, task, row = _captured_reviewer(tmp_path, monkeypatch, head="")
    with pytest.raises(CoordinatorError) as excinfo:
        _finish(store, worker, task, row)
    assert str(excinfo.value) == REVIEWER_SPOOL_WRITE_ERROR
    assert str(excinfo.value.__cause__) == "reviewer spool head is invalid"
    assert store.row("agent", "agent-review")["status"] == "working"
    spool = coordinator_lanes._reviewer_spool_path(store, worker, TID, "agent-review")
    assert not spool.exists()


@pytest.mark.parametrize(
    "stop",
    [KeyboardInterrupt(), SystemExit("coordinator stopped")],
    ids=["keyboard-interrupt", "coordinator-stop"],
)
def test_captured_reviewer_stop_while_spooling_is_not_reworded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: BaseException
) -> None:
    store, worker, task, row = _captured_reviewer(tmp_path, monkeypatch)
    spool = coordinator_lanes._reviewer_spool_path(store, worker, TID, "agent-review")

    def stopped(*_args: Any, **_kwargs: Any) -> Path:
        spool.parent.mkdir(parents=True, exist_ok=True)
        spool.with_suffix(".tmp").write_text("{}", encoding="utf-8")
        raise stop

    monkeypatch.setattr(coordinator_lanes, "_write_reviewer_spool", stopped)
    with pytest.raises(type(stop)) as excinfo:
        _finish(store, worker, task, row)
    assert excinfo.value is stop
    assert not spool.with_suffix(".tmp").exists()
    assert not spool.exists()
    assert store.row("agent", "agent-review")["status"] == "working"


def test_captured_reviewer_keeps_its_spool_when_the_checkpoint_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task, row = _captured_reviewer(tmp_path, monkeypatch)

    def failing_audit(*_args: Any, **_kwargs: Any) -> str:
        raise StoreError("audit write failed")

    monkeypatch.setattr(coordinator_lanes, "record_lane_run", failing_audit)
    assert _finish(store, worker, task, row) is None
    assert store.row("agent", "agent-review")["status"] == "working"
    assert "lane_outcome" not in _stored_c(store)
    # The saved result stays for the next step, and so do the capture files.
    recovered = coordinator_lanes._read_reviewer_spool(
        store,
        worker,
        task,
        store.row("agent", "agent-review"),
        round_num=None,
        head=HEAD,
        dimension="quality",
        stage="review-1",
    )
    assert recovered is not None
    assert recovered[0].protocol_result == "approved"
    paths = coordinator_lanes._lane_capture_paths(worker, TID, "agent-review")
    assert paths["stdout"].exists()


def test_captured_lane_of_a_role_without_a_capture_handler_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every role in LANE_ROLES has a handler today. A role added to the table
    # without one reaches the final refusal instead of being recorded silently.
    store, worker, task = _env(tmp_path, monkeypatch)
    write_check_lane(store.home)
    monkeypatch.setattr(
        coordinator_lanes, "LANE_ROLES", (*coordinator_lanes.LANE_ROLES, "check")
    )
    row = working_row(store, worker, "agent-check", role="check")
    _write_lane_capture(worker, "agent-check", event_stream_output(APPROVED))
    with pytest.raises(CoordinatorError) as excinfo:
        _finish(store, worker, task, row)
    assert str(excinfo.value) == "captured lane role is unknown"
    assert store.row("agent", "agent-check")["status"] == "working"
    assert "lane" not in _stored_c(store)
