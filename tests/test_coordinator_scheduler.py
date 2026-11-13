from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from agent_cli import coordinator_checks, coordinator_runtime
from agent_cli.coordinator import login_cached, tick
from agent_cli.coordinator_common import CoordinatorError
from agent_cli.runtime import Completed
from agent_cli.store import Store
from test_coordinator_support import (
    FakeGh,
    make_session,
    make_worker,
    patch_account_runners,
    write_accounts,
)


def _task(
    store: Store,
    tid: str,
    phase: str,
    created_at: str,
    *,
    state: str = "pr-review",
    session_id: str = "worker-session",
    **checkpoint: Any,
) -> None:
    store.write(
        "task",
        "insert",
        tid,
        {
            "id": tid,
            "session_id": session_id,
            "workflow": "implement",
            "title": "t",
            "repo": "example/project",
            "ref": None,
            "state": state,
            "current_round": 1,
            "created_at": created_at,
            "updated_at": created_at,
            "change_summary_en": None,
            "change_summary_de": None,
            "payload": {
                "coordinator": {
                    "phase": phase,
                    "source": {
                        "repo": "example/project",
                        "number": 7,
                        "assigned_id": "a",
                        "publication_repo": "example/project",
                        "base": "develop",
                        "title": "Fix",
                    },
                    **checkpoint,
                }
            },
        },
    )


@pytest.fixture
def quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        coordinator_runtime, "refresh_execution_binding", lambda *args: None
    )
    monkeypatch.setattr(
        coordinator_runtime, "_verify_source_assigned", lambda *args: None
    )


def _recording_runner(calls: list[list[str]]):
    def run(argv: list[str]) -> Completed:
        calls.append(argv)
        return Completed(1, "", "unexpected")

    return run


@pytest.mark.no_pg
def test_parked_phases(tmp_path) -> None:
    worker = make_worker(tmp_path)
    assert coordinator_runtime.ALWAYS_PARKED == frozenset(
        {"queued", "ask", "blocked", "await_merge", "await_ready", "await_second_review"}
    )
    for phase in coordinator_runtime.ALWAYS_PARKED:
        assert coordinator_runtime.parked(
            {"payload": {"coordinator": {"phase": phase}}}, worker
        )
    assert coordinator_runtime.parked(
        {"payload": {"coordinator": {"phase": "ci", "ci_wait": True}}}, worker
    )
    assert coordinator_runtime.parked(
        {
            "payload": {
                "coordinator": {"phase": "implement", "binding_blocker": "x"}
            }
        },
        worker,
    )
    assert coordinator_runtime.parked(
        {"payload": {"coordinator": {"phase": "implement", "lane_wait": True}}},
        worker,
    )
    for checkpoint in (
        {"phase": "implement"},
        {"phase": "accept"},
        {"phase": "readiness"},
        {"phase": "ci"},
        {"phase": "ci", "ci_wait": False},
        {},
    ):
        assert not coordinator_runtime.parked(
            {"payload": {"coordinator": checkpoint}}, worker
        )


@pytest.mark.no_pg
def test_seconds_since(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agent_cli.coordinator_common.utcnow", lambda: "2026-01-01T00:02:00Z"
    )
    assert coordinator_runtime.seconds_since("2026-01-01T00:00:00Z") == 120.0
    assert coordinator_runtime.seconds_since("2026-01-01T01:00:00+01:00") == 120.0
    for value in (None, 5, "", "yesterday", "2026-01-01T00:00:00"):
        assert coordinator_runtime.seconds_since(value) is None


def test_one_task_runs_one_handler_per_tick(
    tmp_path, monkeypatch: pytest.MonkeyPatch, quiet
) -> None:
    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    tid = "10000000-0000-0000-0000-000000000001"
    _task(store, tid, "ci", "2026-01-01T00:00:00Z")
    count = {"value": 0}

    def phase_ci(*args, **kwargs):
        count["value"] += 1
        return ["ci handler"]

    monkeypatch.setattr(coordinator_runtime, "phase_ci", phase_ci)
    calls: list[list[str]] = []
    assert coordinator_runtime.advance_one(
        store, worker, runner=_recording_runner(calls), lane_runner=None
    ) == ["ci handler"]
    assert count["value"] == 1
    assert calls == []
    store.close()


def test_unchanged_ci_phase_sets_ci_wait_and_a_phase_change_clears_it(
    tmp_path, monkeypatch: pytest.MonkeyPatch, quiet
) -> None:
    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    tid = "10000000-0000-0000-0000-000000000002"
    _task(store, tid, "ci", "2026-01-01T00:00:00Z")
    monkeypatch.setattr(
        coordinator_runtime, "phase_ci", lambda *args, **kwargs: ["ci handler"]
    )
    calls: list[list[str]] = []
    coordinator_runtime.advance_one(
        store, worker, runner=_recording_runner(calls), lane_runner=None
    )
    assert store.row("task", tid)["payload"]["coordinator"]["ci_wait"] is True

    def leave_ci(store_arg, worker_arg, task, runner_arg):
        task["payload"]["coordinator"]["phase"] = "readiness"
        coordinator_runtime.save_task(store_arg, task)
        return ["ci left"]

    monkeypatch.setattr(coordinator_runtime, "phase_ci", leave_ci)
    assert coordinator_runtime.advance_one(
        store, worker, runner=_recording_runner(calls), lane_runner=None
    ) == ["ci left"]
    checkpoint = store.row("task", tid)["payload"]["coordinator"]
    assert checkpoint["phase"] == "readiness"
    assert "ci_wait" not in checkpoint
    assert calls == []
    store.close()


def test_after_advance_saves_only_on_a_change(
    tmp_path, monkeypatch: pytest.MonkeyPatch, quiet
) -> None:
    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    _task(
        store,
        "10000000-0000-0000-0000-000000000003",
        "tests",
        "2026-01-01T00:00:00Z",
    )
    monkeypatch.setattr(
        coordinator_runtime, "phase_tests", lambda *args, **kwargs: ["tests handler"]
    )
    saved: list[str] = []
    original = coordinator_runtime.save_task

    def record(store_arg, task):
        saved.append(str(task["id"]))
        original(store_arg, task)

    monkeypatch.setattr(coordinator_runtime, "save_task", record)
    calls: list[list[str]] = []
    assert coordinator_runtime.advance_one(
        store, worker, runner=_recording_runner(calls), lane_runner=None
    ) == ["tests handler"]
    assert saved == []
    assert calls == []
    store.close()


def test_restored_binding_runs_no_handler_in_the_observing_tick(
    tmp_path, monkeypatch: pytest.MonkeyPatch, quiet
) -> None:
    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    older = "10000000-0000-0000-0000-000000000034"
    newer = "10000000-0000-0000-0000-000000000035"
    _task(
        store,
        older,
        "tests",
        "2026-01-01T00:00:00Z",
        binding_blocker="configuration changed",
        binding_blocker_activity_id="published-1",
    )
    _task(
        store,
        newer,
        "tests",
        "2026-01-02T00:00:00Z",
        binding_blocker="configuration changed",
        binding_blocker_activity_id="published-2",
    )
    seen: list[str] = []

    def phase_tests(store_arg, worker_arg, task, *args, **kwargs):
        seen.append(str(task["id"]))
        return ["tests handler"]

    monkeypatch.setattr(coordinator_runtime, "phase_tests", phase_tests)
    calls: list[list[str]] = []
    assert coordinator_runtime.advance_one(
        store, worker, runner=_recording_runner(calls), lane_runner=None
    ) == []
    assert seen == []
    for tid in (older, newer):
        checkpoint = store.row("task", tid)["payload"]["coordinator"]
        assert "binding_blocker" not in checkpoint
        assert "binding_blocker_activity_id" not in checkpoint
    assert coordinator_runtime.advance_one(
        store, worker, runner=_recording_runner(calls), lane_runner=None
    ) == ["tests handler"]
    assert seen == [older]
    assert calls == []
    store.close()


def test_parked_task_does_not_block_a_runnable_task(
    tmp_path, monkeypatch: pytest.MonkeyPatch, quiet
) -> None:
    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    _task(store, "10000000-0000-0000-0000-000000000004", "blocked", "2026-01-01T00:00:00Z")
    _task(store, "10000000-0000-0000-0000-000000000005", "ci", "2026-01-02T00:00:00Z")
    monkeypatch.setattr(
        coordinator_runtime, "phase_blocked", lambda *args, **kwargs: ["blocked handler"]
    )
    monkeypatch.setattr(
        coordinator_runtime, "phase_ci", lambda *args, **kwargs: ["ci handler"]
    )
    calls: list[list[str]] = []
    assert coordinator_runtime.advance_one(
        store, worker, runner=_recording_runner(calls), lane_runner=None
    ) == ["blocked handler", "ci handler"]
    assert calls == []
    store.close()


def test_step_two_takes_the_oldest_runnable_task(
    tmp_path, monkeypatch: pytest.MonkeyPatch, quiet
) -> None:
    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    _task(
        store,
        "10000000-0000-0000-0000-000000000006",
        "readiness",
        "2026-01-01T00:00:00Z",
        pr_number=7,
    )
    _task(store, "10000000-0000-0000-0000-000000000007", "ci", "2026-01-02T00:00:00Z")
    monkeypatch.setattr(
        coordinator_checks,
        "_fetch_pr_json",
        lambda *args, **kwargs: {"mergeable": "MERGEABLE"},
    )
    monkeypatch.setattr(
        coordinator_checks,
        "scoped",
        lambda store, session_id, runner, **kwargs: runner,
    )
    monkeypatch.setattr(
        coordinator_runtime,
        "phase_readiness",
        lambda *args, **kwargs: ["readiness handler"],
    )
    count = {"value": 0}

    def phase_ci(*args, **kwargs):
        count["value"] += 1
        return ["ci handler"]

    monkeypatch.setattr(coordinator_runtime, "phase_ci", phase_ci)
    calls: list[list[str]] = []
    assert coordinator_runtime.advance_one(
        store, worker, runner=_recording_runner(calls), lane_runner=None
    ) == ["readiness handler"]
    assert count["value"] == 0
    assert calls == []
    store.close()


def test_observed_task_is_not_advanced_twice_in_one_tick(
    tmp_path, monkeypatch: pytest.MonkeyPatch, quiet
) -> None:
    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    tid = "10000000-0000-0000-0000-000000000008"
    _task(store, tid, "blocked", "2026-01-01T00:00:00Z", resume_phase="ci")

    def resume(store_arg, worker_arg, task, runner_arg):
        checkpoint = task["payload"]["coordinator"]
        checkpoint["phase"] = "ci"
        checkpoint.pop("resume_phase", None)
        coordinator_runtime.save_task(store_arg, task)
        return ["resumed"]

    count = {"value": 0}

    def phase_ci(*args, **kwargs):
        count["value"] += 1
        return ["ci handler"]

    monkeypatch.setattr(coordinator_runtime, "phase_blocked", resume)
    monkeypatch.setattr(coordinator_runtime, "phase_ci", phase_ci)
    calls: list[list[str]] = []
    assert coordinator_runtime.advance_one(
        store, worker, runner=_recording_runner(calls), lane_runner=None
    ) == ["resumed"]
    assert count["value"] == 0
    assert coordinator_runtime.advance_one(
        store, worker, runner=_recording_runner(calls), lane_runner=None
    ) == ["ci handler"]
    assert count["value"] == 1
    assert calls == []
    store.close()


def test_parked_observation_is_rate_limited(
    tmp_path, monkeypatch: pytest.MonkeyPatch, quiet
) -> None:
    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(make_worker(tmp_path), park_poll_seconds=120)
    tid = "10000000-0000-0000-0000-000000000009"
    _task(store, tid, "ci", "2026-01-01T00:00:00Z")
    count = {"value": 0}

    def phase_ci(*args, **kwargs):
        count["value"] += 1
        return ["ci handler"]

    monkeypatch.setattr(coordinator_runtime, "phase_ci", phase_ci)
    calls: list[list[str]] = []
    runner = _recording_runner(calls)
    assert coordinator_runtime.advance_one(store, worker, runner=runner, lane_runner=None) == ["ci handler"]
    assert coordinator_runtime.advance_one(store, worker, runner=runner, lane_runner=None) == ["ci handler"]
    assert coordinator_runtime.advance_one(store, worker, runner=runner, lane_runner=None) == []
    assert count["value"] == 2
    store.sync_set("coordinator-park:" + tid, "2020-01-01T00:00:00Z")
    assert coordinator_runtime.advance_one(store, worker, runner=runner, lane_runner=None) == ["ci handler"]
    assert count["value"] == 3
    assert calls == []
    store.close()


def test_zero_park_poll_seconds_observes_on_every_tick(
    tmp_path, monkeypatch: pytest.MonkeyPatch, quiet
) -> None:
    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    tid = "10000000-0000-0000-0000-000000000010"
    _task(store, tid, "ci", "2026-01-01T00:00:00Z")
    count = {"value": 0}

    def phase_ci(*args, **kwargs):
        count["value"] += 1
        return ["ci handler"]

    monkeypatch.setattr(coordinator_runtime, "phase_ci", phase_ci)
    calls: list[list[str]] = []
    runner = _recording_runner(calls)
    for _ in range(3):
        coordinator_runtime.advance_one(store, worker, runner=runner, lane_runner=None)
    assert count["value"] == 3
    assert store.sync_get("coordinator-park:" + tid) is None
    assert calls == []
    store.close()


def test_one_failing_task_does_not_stop_the_tick(
    tmp_path, monkeypatch: pytest.MonkeyPatch, quiet
) -> None:
    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    older = "10000000-0000-0000-0000-000000000011"
    newer = "10000000-0000-0000-0000-000000000012"
    _task(store, older, "blocked", "2026-01-01T00:00:00Z")
    _task(store, newer, "ci", "2026-01-02T00:00:00Z")

    def fake(store_arg, worker_arg, task, **kwargs):
        if task["id"] == older:
            raise CoordinatorError("boom")
        return ["second"]

    monkeypatch.setattr(coordinator_runtime, "_advance_task", fake)
    calls: list[list[str]] = []
    assert coordinator_runtime.advance_one(
        store, worker, runner=_recording_runner(calls), lane_runner=None
    ) == ["blocked: boom", "second"]
    assert calls == []
    store.close()


def test_base_exception_is_not_swallowed(
    tmp_path, monkeypatch: pytest.MonkeyPatch, quiet
) -> None:
    class Stop(BaseException):
        pass

    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    _task(store, "10000000-0000-0000-0000-000000000013", "ci", "2026-01-01T00:00:00Z")

    def fail(store_arg, worker_arg, task, *, runner, lane_runner):
        raise Stop()

    monkeypatch.setattr(coordinator_runtime, "_advance_task", fail)
    with pytest.raises(Stop):
        coordinator_runtime.advance_one(
            store, worker, runner=_recording_runner([]), lane_runner=None
        )
    store.close()


def test_runnable_count(tmp_path) -> None:
    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "other-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    _task(store, "10000000-0000-0000-0000-000000000014", "ask", "2026-01-01T00:00:00Z")
    assert coordinator_runtime.runnable_count(store, worker) == 0
    assert not coordinator_runtime.has_runnable_task(store, worker)
    _task(store, "10000000-0000-0000-0000-000000000015", "implement", "2026-01-02T00:00:00Z")
    _task(store, "10000000-0000-0000-0000-000000000016", "queued", "2026-01-03T00:00:00Z")
    _task(store, "10000000-0000-0000-0000-000000000017", "ci", "2026-01-04T00:00:00Z", ci_wait=True)
    _task(
        store,
        "10000000-0000-0000-0000-000000000018",
        "implement",
        "2026-01-05T00:00:00Z",
        state="failed",
    )
    _task(
        store,
        "10000000-0000-0000-0000-000000000019",
        "implement",
        "2026-01-06T00:00:00Z",
        session_id="other-session",
    )
    store.write(
        "task",
        "insert",
        "10000000-0000-0000-0000-000000000020",
        {
            "id": "10000000-0000-0000-0000-000000000020",
            "session_id": "worker-session",
            "workflow": "implement",
            "title": "t",
            "repo": "example/project",
            "ref": None,
            "state": "pr-review",
            "current_round": 1,
            "created_at": "2026-01-07T00:00:00Z",
            "updated_at": "2026-01-07T00:00:00Z",
            "change_summary_en": None,
            "change_summary_de": None,
            "payload": {},
        },
    )
    assert coordinator_runtime.runnable_count(store, worker) == 2
    assert coordinator_runtime.has_runnable_task(store, worker)
    store.close()


def test_discovery_skipped_while_a_task_is_runnable(tmp_path) -> None:
    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    _task(store, "10000000-0000-0000-0000-000000000021", "ci", "2026-01-01T00:00:00Z")
    calls: list[list[str]] = []
    assert coordinator_runtime.discover_assignments(
        store, worker, _recording_runner(calls)
    ) == []
    assert calls == []
    store.close()


def test_discovery_runs_when_every_task_waits(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    _task(store, "10000000-0000-0000-0000-000000000022", "await_merge", "2026-01-01T00:00:00Z")
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(argv)
        return fake(argv)

    assert coordinator_runtime.discover_assignments(store, worker, runner) == []
    assert any("issues?assignee=" in " ".join(argv) for argv in calls)
    store.close()


@pytest.mark.no_pg
def test_login_cache_serves_the_second_identical_verification() -> None:
    calls: list[list[str]] = []
    result = Completed(0, "worker-bot", "")

    def base(argv: list[str]) -> Completed:
        calls.append(argv)
        return result

    cached = login_cached(base, max_age_seconds=30)
    first = ["env", "GH_CONFIG_DIR=/a", "gh", "api", "user", "--jq", ".login"]
    second = ["env", "GH_CONFIG_DIR=/b", "gh", "api", "user", "--jq", ".login"]
    other = ["env", "GH_CONFIG_DIR=/a", "gh", "api", "user"]
    assert cached(first) is result
    assert cached(first) is result
    assert len(calls) == 1
    assert cached(second) is result
    assert len(calls) == 2
    assert cached(other) is result
    assert cached(other) is result
    assert len(calls) == 4


@pytest.mark.no_pg
def test_login_cache_does_not_keep_a_failure() -> None:
    calls: list[list[str]] = []
    result = Completed(1, "", "failed")

    def base(argv: list[str]) -> Completed:
        calls.append(argv)
        return result

    cached = login_cached(base, max_age_seconds=30)
    argv = ["env", "GH_CONFIG_DIR=/a", "gh", "api", "user", "--jq", ".login"]
    assert cached(argv) is result
    assert cached(argv) is result
    assert len(calls) == 2


@pytest.mark.no_pg
def test_login_cache_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    clock = {"now": 0.0}
    monkeypatch.setattr("agent_cli.coordinator.time.monotonic", lambda: clock["now"])

    def base(argv: list[str]) -> Completed:
        calls.append(argv)
        return Completed(0, "worker-bot", "")

    cached = login_cached(base, max_age_seconds=30)
    argv = ["env", "GH_CONFIG_DIR=/a", "gh", "api", "user", "--jq", ".login"]
    cached(argv)
    clock["now"] = 30.0
    cached(argv)
    clock["now"] = 61.0
    cached(argv)
    assert len(calls) == 2


def test_tick_wraps_the_runner(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    worker = make_worker(tmp_path)
    calls: list[list[str]] = []
    login_argv = [
        "env",
        "GH_CONFIG_DIR=/a",
        "gh",
        "api",
        "user",
        "--jq",
        ".login",
    ]

    def base(argv: list[str]) -> Completed:
        calls.append(argv)
        return Completed(0, "worker-bot", "")

    def preflight(store_arg, worker_arg, runner):
        runner(login_argv)
        runner(login_argv)
        raise CoordinatorError("stop")

    monkeypatch.setattr("agent_cli.coordinator.preflight_worker", preflight)
    assert tick(store, worker, runner=base) == ["preflight blocked: stop"]
    assert len(calls) == 1
    store.close()
