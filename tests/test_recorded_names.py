"""Names an earlier version stored: ``recorded_slots`` keeps them readable.

Earlier versions named the review stages, checklist keys and coordinator
phases after the drivers of model slots ``first`` and ``second``. These tests
use placeholder drivers; the registry starts empty in every test here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import coordinator_runtime
from agent_cli.ai_accounts import (
    AccountError,
    load_driver_configuration,
    load_recorded_names,
)
from agent_cli.allow import CHECKLIST_KEYS, evaluate_allow
from agent_cli.coordinator_checks import TaskFacts, current_phase
from agent_cli.coordinator_common import coord, upgrade_recorded_phases
from agent_cli.drivers import RecordedNames
from agent_cli.main import main, open_store
from agent_cli.model_slots import (
    ModelSlots,
    ModelSlotsError,
    PinnedModelSlotsError,
    UnrecordedSlotsError,
    canonical_checklist,
    canonical_key,
    canonical_phase,
    canonical_stage,
    implementer_from_snapshot,
    install_recorded_names,
    models_from_snapshot,
    recorded_names,
    unrecorded_name_hint,
    unrecorded_rows_hint,
)
from agent_cli.order_ops import event_view
from agent_cli.store import Store, utcnow
from driver_fixtures import driver_document, write_accounts, write_drivers
from test_coordinator_support import make_session, make_worker, seed_order_task
from test_coordinator_support import write_accounts as write_coordinator_accounts

RECORDED = RecordedNames("driver-a", "driver-b")
HINT = (
    "(written by an earlier version? configure recorded_slots in drivers.json, "
    "docs/ai-accounts.md#drivers)"
)


@pytest.mark.no_pg
def test_recorded_slots_alias_stored_stages_keys_and_phases() -> None:
    install_recorded_names(RECORDED)

    assert recorded_names() == RECORDED
    assert canonical_stage("driver-a-pr") == "review-1"
    assert canonical_stage("driver-b-pr") == "review-2"
    assert canonical_stage("driver-c-pr") == "driver-c-pr"
    assert canonical_key("driver-a_pr_quality") == "review_1_quality"
    assert canonical_key("driver-b_pr_logic") == "review_2_logic"
    assert canonical_key("driver-c_pr_logic") == "driver-c_pr_logic"
    assert canonical_phase("pr_gates_driver-a") == "pr_gates_first"
    assert canonical_phase("pr_gates_driver-b") == "pr_gates_second"
    assert canonical_phase("pr_gates_driver-c") == "pr_gates_driver-c"
    # An explicit canonical row wins over the stored name of the same key.
    assert canonical_checklist(
        {"driver-b_pr_logic": "ja", "review_2_logic": "nein", "driver-a_pr_quality": "n_a"}
    ) == {"review_2_logic": "nein", "review_1_quality": "n_a"}
    # The task pin governs; recorded_slots only renames the stored stage.
    assert ModelSlots("driver-c", "driver-d").for_stage("driver-b-pr") == "driver-d"
    with pytest.raises(ModelSlotsError) as unknown:
        ModelSlots("driver-c", "driver-d").for_stage("driver-c-pr")
    assert str(unknown.value) == "unknown gate stage driver-c-pr"
    # An unpinned task reads with the recorded slots.
    assert models_from_snapshot({}) == ModelSlots("driver-a", "driver-b")
    assert implementer_from_snapshot({}) == "driver-a"


@pytest.mark.no_pg
def test_without_recorded_slots_stored_names_stay_unknown() -> None:
    assert recorded_names() is None
    assert canonical_stage("driver-a-pr") == "driver-a-pr"
    assert canonical_key("driver-a_pr_quality") == "driver-a_pr_quality"
    assert canonical_phase("pr_gates_driver-a") == "pr_gates_driver-a"
    assert canonical_checklist({"driver-a_pr_quality": "ja"}) == {
        "driver-a_pr_quality": "ja"
    }
    with pytest.raises(ModelSlotsError) as stage:
        ModelSlots("driver-a", "driver-b").for_stage("driver-a-pr")
    assert str(stage.value) == f"unknown gate stage driver-a-pr {HINT}"
    # An unknown stage of no old shape gets no hint.
    with pytest.raises(ModelSlotsError) as other:
        ModelSlots("driver-a", "driver-b").for_stage("review-3")
    assert str(other.value) == "unknown gate stage review-3"


@pytest.mark.no_pg
def test_unrecorded_name_hint_names_only_recorded_shapes() -> None:
    hint = f" {HINT}"
    for name in (
        "driver-a-pr",
        "driver-c-pr",
        "driver-c_pr_quality",
        "driver-c_pr_logic",
        "pr_gates_driver-b",
    ):
        assert unrecorded_name_hint(name) == hint, name
    # Current names, reserved slot ids and other shapes get no hint.
    for name in (
        "review-1",
        "review_2_logic",
        "pr_gates_first",
        "pr_gates_second",
        "review-3",
        "-pr",
        "driver-c_pr_style",
        "spec_written",
    ):
        assert unrecorded_name_hint(name) == "", name
    assert unrecorded_rows_hint(["review-1", "driver-b_pr_logic"]) == hint
    assert unrecorded_rows_hint(["review-1", None]) == ""

    install_recorded_names(RECORDED)
    assert unrecorded_name_hint("driver-c-pr") == ""
    assert unrecorded_rows_hint(["driver-a-pr"]) == ""


@pytest.mark.no_pg
def test_allow_refusal_names_recorded_slots_for_old_rows() -> None:
    task = {
        "id": "t1",
        "session_id": "s",
        "workflow": "implement",
        "state": "pr-review",
        "models": {"first": "driver-a", "second": "driver-b"},
        "implementer": "driver-a",
        "checklist": {"driver-a_pr_quality": "ja"},
        "summaries": {"en": "Ship.", "de": "Liefern."},
        "gates": [
            {
                "stage": "driver-a-pr",
                "dimension": "quality",
                "vendor": "driver-a",
                "verdict": "approved",
                "head_sha": "abc123",
            }
        ],
        "local_checks": [],
    }

    def allow(action: str, *tasks: dict, task_id: str | None = "t1"):  # type: ignore[no-untyped-def]
        return evaluate_allow(
            action, session_id="s", task_id=task_id, session_tasks=list(tasks)
        )

    # Without recorded_slots the old rows read as absent; the refusal says why.
    done = allow("task-done", task)
    assert not done.allowed
    assert done.reason == f"task is not ready_for_done {HINT}"
    assert "t1:gate:review-1/quality=missing" in done.blocking
    assert "t1:review_1_quality=missing" in done.blocking
    claim = allow("claim-done", task, task_id=None)
    assert not claim.allowed
    assert claim.reason == f"open tasks are not ready_for_done {HINT}"
    plain = {**task, "checklist": {}, "gates": []}
    assert allow("task-done", plain).reason == "task is not ready_for_done"

    install_recorded_names(RECORDED)
    aliased = allow("task-done", task)
    assert aliased.reason == "task is not ready_for_done"
    assert "t1:gate:review-1/quality=missing" not in aliased.blocking
    assert "t1:review_1_quality=missing" not in aliased.blocking


@pytest.mark.no_pg
def test_unpinned_task_without_recorded_slots_fails_as_a_pin_error() -> None:
    with pytest.raises(UnrecordedSlotsError) as unpinned:
        models_from_snapshot({"id": "task-1"})
    assert isinstance(unpinned.value, PinnedModelSlotsError)
    assert str(unpinned.value) == (
        "task task-1 predates model pins; configure recorded_slots in "
        "drivers.json to read it (docs/ai-accounts.md#drivers)"
    )
    with pytest.raises(UnrecordedSlotsError, match="^the task predates model pins"):
        implementer_from_snapshot({})
    # A pinned task never needs recorded_slots.
    pinned = {"models": {"first": "driver-c", "second": None}, "implementer": "driver-e"}
    assert models_from_snapshot(pinned) == ModelSlots("driver-c", None)
    assert implementer_from_snapshot(pinned) == "driver-e"


@pytest.mark.no_pg
def test_load_recorded_names_reads_only_recorded_slots(tmp_path: Path) -> None:
    assert load_recorded_names(tmp_path) is None
    write_drivers(tmp_path)
    assert load_recorded_names(tmp_path) == RECORDED

    # An error elsewhere in drivers.json does not block reading history.
    document = driver_document()
    document["drivers"]["driver-a"]["unknown"] = True
    write_drivers(tmp_path, document)
    with pytest.raises(AccountError):
        load_driver_configuration(tmp_path)
    assert load_recorded_names(tmp_path) == RECORDED

    del document["recorded_slots"]
    write_drivers(tmp_path, document)
    assert load_recorded_names(tmp_path) is None

    document["recorded_slots"] = {"first": "driver-a", "second": "driver-a"}
    write_drivers(tmp_path, document)
    with pytest.raises(AccountError) as invalid:
        load_recorded_names(tmp_path)
    assert str(invalid.value) == (
        "recorded_slots.first and recorded_slots.second must differ "
        "(see drivers.json, docs/ai-accounts.md#drivers)"
    )

    (tmp_path / "drivers.json").write_text("[]", encoding="utf-8")
    with pytest.raises(AccountError, match="^drivers.json must be an object"):
        load_recorded_names(tmp_path)


def test_open_store_installs_recorded_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    write_drivers(tmp_path)
    open_store(create=True).close()
    assert recorded_names() == RECORDED

    document = driver_document()
    del document["recorded_slots"]
    write_drivers(tmp_path, document)
    open_store().close()
    assert recorded_names() is None

    document["recorded_slots"] = {"first": "first", "second": "driver-b"}
    write_drivers(tmp_path, document)
    with pytest.raises(SystemExit, match="recorded_slots.first must be a driver id"):
        open_store()


@pytest.mark.no_pg
def test_upgrade_recorded_phases_renames_every_stored_phase() -> None:
    def checkpoint() -> dict:
        return {
            "phase": "blocked",
            "resume_phase": "pr_gates_driver-b",
            "retry": {"phase": "pr_gates_driver-b", "attempt": 1},
            "gate_route": {"phase": "pr_gates_driver-a", "check": "review_1_quality"},
            "order": {
                "checkpoint": {
                    "event_id": "event-1",
                    "kind": "blocked",
                    "key": "blocked|pr_gates_driver-b||",
                }
            },
        }

    c = checkpoint()
    assert upgrade_recorded_phases(c) is False
    assert c == checkpoint()

    install_recorded_names(RECORDED)
    assert upgrade_recorded_phases(c) is True
    expected = checkpoint()
    expected["resume_phase"] = "pr_gates_second"
    expected["retry"]["phase"] = "pr_gates_second"
    expected["gate_route"]["phase"] = "pr_gates_first"
    expected["order"]["checkpoint"]["key"] = "blocked|pr_gates_second||"
    assert c == expected
    assert upgrade_recorded_phases(c) is False
    assert upgrade_recorded_phases({"phase": "ci", "retry": "malformed"}) is False


def test_tick_entry_upgrades_and_saves_stored_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_recorded_names(RECORDED)
    store = Store(tmp_path)
    worker = make_worker(tmp_path)
    tid = "aa000001-0000-4000-8000-0000000000f1"
    retry = {"phase": "pr_gates_driver-b", "attempt": 1, "at": "2026-01-01T00:00:00Z"}
    store.write(
        "task",
        "insert",
        tid,
        {
            "id": tid,
            "session_id": worker.session_id,
            "workflow": "implement",
            "state": "pr-review",
            "payload": {"coordinator": {"phase": "pr_gates_driver-b", "retry": retry}},
        },
    )
    seen: list[dict] = []

    def advance(store, worker, task, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(copy.deepcopy(coord(task)))
        return ["advanced"]

    monkeypatch.setattr(coordinator_runtime, "_advance_task", advance)
    task = store.row("task", tid)
    assert task is not None

    lines = coordinator_runtime._advance_guarded(
        store, worker, task, runner=None, lane_runner=None
    )

    assert lines == ["advanced"]
    assert seen[0]["phase"] == "pr_gates_second"
    saved = store.row("task", tid)
    assert saved is not None
    # The retry keeps its phase, so the scheduler does not drop it as stale.
    assert saved["payload"]["coordinator"] == {
        "phase": "pr_gates_second",
        "retry": {**retry, "phase": "pr_gates_second"},
    }
    store.close()


def test_unknown_coordinator_phase_names_recorded_slots_only_for_old_shapes(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    write_coordinator_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)

    def runner(argv):  # type: ignore[no-untyped-def]
        pytest.fail(f"dispatch refusal must not run {argv}")

    def refusal(index: int, phase: str) -> list[str]:
        oid = f"aa000001-0000-4000-8000-00000000020{index}"
        task = seed_order_task(store, worker, oid, phase=phase)
        return coordinator_runtime._advance_guarded(
            store, worker, task, runner=runner, lane_runner=None
        )

    try:
        assert recorded_names() is None
        # Without recorded_slots, an old phase name is refused with the hint.
        assert refusal(1, "pr_gates_driver-a") == [
            f"blocked: unknown coordinator phase pr_gates_driver-a {HINT}"
        ]
        # A phase of no old shape cannot be fixed by recorded_slots: no hint.
        assert refusal(2, "pr_gate_first") == [
            "blocked: unknown coordinator phase pr_gate_first"
        ]
        # With recorded_slots, a driver it does not name stays unknown without
        # the hint.
        install_recorded_names(RECORDED)
        assert refusal(3, "pr_gates_driver-c") == [
            "blocked: unknown coordinator phase pr_gates_driver-c"
        ]
    finally:
        store.close()


def test_tick_keeps_old_checklist_keys_until_recorded_slots_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_coordinator_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    old = "aa000001-0000-4000-8000-000000000301"
    other = "aa000001-0000-4000-8000-000000000302"
    for tid, phase, created in (
        (old, "await_merge", "2026-01-01T00:00:00Z"),
        (other, "implement", "2026-01-02T00:00:00Z"),
    ):
        store.write(
            "task",
            "insert",
            tid,
            {
                "id": tid,
                "session_id": worker.session_id,
                "workflow": "implement",
                "state": "open",
                "models": {"first": "driver-a", "second": "driver-b"},
                "payload": {"coordinator": {"phase": phase}},
                "created_at": created,
            },
        )
    # The old task holds every key, its review keys under the names an earlier
    # version wrote, all of them closed.
    old_names = {
        "review_1_quality": "driver-a_pr_quality",
        "review_1_logic": "driver-a_pr_logic",
        "review_2_quality": "driver-b_pr_quality",
        "review_2_logic": "driver-b_pr_logic",
    }
    stored = [old_names.get(key, key) for key in CHECKLIST_KEYS["implement"]]
    for key in stored:
        cid = str(uuid.uuid4())
        store.write(
            "checklist_item",
            "insert",
            cid,
            {
                "id": cid,
                "task_id": old,
                "key": key,
                "status": "ja",
                "evidence": "reviewed",
                "source": "script",
                "deviation_declared": False,
                "deviation_granted": False,
                "granted_by": None,
                "updated_at": utcnow(),
            },
        )
    advanced: list[str] = []

    def advance(store, worker, task, **kwargs):  # type: ignore[no-untyped-def]
        advanced.append(str(task["id"]))
        return [f"advanced {task['id']}"]

    def runner(argv):  # type: ignore[no-untyped-def]
        pytest.fail(f"a refused tick must not run {argv}")

    def keys(tid: str) -> list[str]:
        return sorted(
            str(row["key"])
            for row in store.rows("checklist_item")
            if row.get("task_id") == tid
        )

    def review_statuses() -> dict[str, str]:
        task = store.row("task", old)
        assert task is not None
        rows = TaskFacts(store, task).checklist()
        return {key: str(rows[key]["status"]) for key in old_names if key in rows}

    monkeypatch.setattr(coordinator_runtime, "_advance_guarded", advance)
    try:
        assert recorded_names() is None
        lines = coordinator_runtime.advance_one(
            store, worker, runner=runner, lane_runner=None
        )

        # The old task is refused with the hint and gets no row; the other
        # task is reconciled and advances.
        assert lines == [
            f"blocked: task {old} holds unknown checklist key driver-a_pr_logic {HINT}",
            f"advanced {other}",
        ]
        assert advanced == [other]
        assert keys(old) == sorted(stored)
        assert keys(other) == sorted(CHECKLIST_KEYS["implement"])
        assert review_statuses() == {}

        # Configured later, the entry restores the recorded values, and the
        # tick advances the old task without adding a row.
        install_recorded_names(RECORDED)
        assert review_statuses() == dict.fromkeys(old_names, "ja")
        advanced.clear()
        assert coordinator_runtime.advance_one(
            store, worker, runner=runner, lane_runner=None
        ) == [f"advanced {old}", f"advanced {other}"]
        assert advanced == [old, other]
        assert keys(old) == sorted(stored)
        assert review_statuses() == dict.fromkeys(old_names, "ja")
    finally:
        store.close()


def test_tick_advances_a_younger_task_past_a_refused_runnable_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_coordinator_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    old = "aa000001-0000-4000-8000-000000000311"
    other = "aa000001-0000-4000-8000-000000000312"
    for tid, created in (
        (old, "2026-01-01T00:00:00Z"),
        (other, "2026-01-02T00:00:00Z"),
    ):
        store.write(
            "task",
            "insert",
            tid,
            {
                "id": tid,
                "session_id": worker.session_id,
                "workflow": "implement",
                "state": "open",
                "models": {"first": "driver-a", "second": "driver-b"},
                "payload": {"coordinator": {"phase": "implement"}},
                "created_at": created,
            },
        )
    old_names = {
        "review_1_quality": "driver-a_pr_quality",
        "review_1_logic": "driver-a_pr_logic",
        "review_2_quality": "driver-b_pr_quality",
        "review_2_logic": "driver-b_pr_logic",
    }
    stored = [old_names.get(key, key) for key in CHECKLIST_KEYS["implement"]]
    for key in stored:
        cid = str(uuid.uuid4())
        store.write(
            "checklist_item",
            "insert",
            cid,
            {
                "id": cid,
                "task_id": old,
                "key": key,
                "status": "ja",
                "evidence": "reviewed",
                "source": "script",
                "deviation_declared": False,
                "deviation_granted": False,
                "granted_by": None,
                "updated_at": utcnow(),
            },
        )
    advanced: list[str] = []

    def advance(store, worker, task, **kwargs):  # type: ignore[no-untyped-def]
        advanced.append(str(task["id"]))
        return [f"advanced {task['id']}"]

    def runner(argv):  # type: ignore[no-untyped-def]
        pytest.fail(f"a refused tick must not run {argv}")

    def keys(tid: str) -> list[str]:
        return sorted(
            str(row["key"])
            for row in store.rows("checklist_item")
            if row.get("task_id") == tid
        )

    monkeypatch.setattr(coordinator_runtime, "_advance_guarded", advance)
    try:
        assert recorded_names() is None
        # The older task is runnable but refused; the younger task must still
        # advance in the same tick.
        assert coordinator_runtime.advance_one(
            store, worker, runner=runner, lane_runner=None
        ) == [
            f"blocked: task {old} holds unknown checklist key driver-a_pr_logic {HINT}",
            f"advanced {other}",
        ]
        assert advanced == [other]
        assert keys(old) == sorted(stored)
        assert keys(other) == sorted(CHECKLIST_KEYS["implement"])
    finally:
        store.close()


@pytest.mark.no_pg
def test_read_only_views_show_neutral_phases_without_writing() -> None:
    install_recorded_names(RECORDED)
    task = {"id": "t", "payload": {"coordinator": {"phase": "pr_gates_driver-a"}}}

    assert current_phase(task) == "pr_gates_first"
    assert task["payload"]["coordinator"]["phase"] == "pr_gates_driver-a"
    assert event_view({"id": "e", "payload": {"phase": "pr_gates_driver-b"}})[
        "phase"
    ] == "pr_gates_second"
    assert event_view({"id": "e", "payload": {}})["phase"] is None


@pytest.mark.no_pg
def test_legacy_execution_binding_omits_models_only_for_recorded_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fields = {
        "models": {"first": "driver-a", "second": "driver-b"},
        "lane_execution": {"mode": "this-device-is-lane-host"},
        "workspace": "/work",
    }
    monkeypatch.setattr(
        coordinator_runtime,
        "_execution_binding_fields",
        lambda *args, **kwargs: copy.deepcopy(fields),
    )

    def digest(selected: dict) -> str:
        encoded = json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def legacy(models: ModelSlots, recorded: RecordedNames | None) -> str:
        ai = SimpleNamespace(models=models, recorded_slots=recorded)
        return coordinator_runtime._legacy_execution_binding(
            None, None, "example/project", ai  # type: ignore[arg-type]
        )

    with_models = {"models": fields["models"], "workspace": "/work"}
    assert legacy(ModelSlots("driver-a", "driver-b"), RECORDED) == digest(
        {"workspace": "/work"}
    )
    assert legacy(ModelSlots("driver-a", "driver-b"), None) == digest(with_models)
    assert legacy(ModelSlots("driver-c", "driver-b"), RECORDED) == digest(with_models)


def test_cli_accepts_recorded_names_as_deprecated_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    write_drivers(tmp_path)
    write_accounts(tmp_path)
    main(["init"])
    main(
        [
            "session", "register", "--id", "s", "--kind", "human",
            "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review",
        ]
    )
    main(["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    out = capsys.readouterr().out
    tid = [line for line in out.splitlines() if line.startswith("task ")][-1].split()[1]
    # A new process has installed nothing before the key is read.
    install_recorded_names(None)

    main(
        [
            "checklist", "set", "--task", tid, "--key", "driver-a_pr_quality",
            "--status", "pending", "--source", "script",
        ]
    )

    captured = capsys.readouterr()
    assert captured.err == (
        "deprecated stage/key name driver-a_pr_quality; use review_1_quality\n"
    )
    assert captured.out == "checklist review_1_quality=pending\n"


def test_cli_names_recorded_slots_for_old_names_without_the_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    document = driver_document()
    del document["recorded_slots"]
    write_drivers(tmp_path, document)
    write_accounts(tmp_path)
    main(["init"])
    main(
        [
            "session", "register", "--id", "s", "--kind", "human",
            "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review",
        ]
    )
    main(["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    out = capsys.readouterr().out
    tid = [line for line in out.splitlines() if line.startswith("task ")][-1].split()[1]
    gate = [
        "gate", "record", "--task", tid, "--dimension", "quality",
        "--vendor", "driver-a", "--verdict", "approved", "--head", "abc123",
        "--agent", str(uuid.uuid4()), "--stage",
    ]

    refusals = (
        (
            [
                "agent", "start", "--session", "s", "--task", tid,
                "--role", "pr-reviewer-quality", "--vendor", "driver-a",
                "--stage", "driver-a-pr",
            ],
            "stage must be review-1|review-2",
        ),
        ([*gate, "driver-b-pr"], "stage must be review-1|review-2"),
        (
            [
                "checklist", "set", "--task", tid, "--key", "driver-a_pr_quality",
                "--status", "pending", "--source", "script",
            ],
            f"checklist driver-a_pr_quality for task {tid} not found",
        ),
        (
            [
                "close-step", "--task", tid, "--key", "driver-b_pr_logic",
                "--source", "script", "--evidence", "reviewed",
            ],
            "unknown key driver-b_pr_logic",
        ),
    )
    for argv, refusal in refusals:
        with pytest.raises(SystemExit) as refused:
            main(argv)
        assert str(refused.value) == f"agent: {refusal} {HINT}", argv
    # Another unknown name is refused without the hint.
    with pytest.raises(SystemExit) as other:
        main([*gate, "review-3"])
    assert str(other.value) == "agent: stage must be review-1|review-2"
    assert capsys.readouterr().err == ""

    # A stored gate row with an old name reads as absent; task done names why.
    store = Store(tmp_path)
    try:
        gate_id = str(uuid.uuid4())
        store.write(
            "review_gate",
            "insert",
            gate_id,
            {
                "id": gate_id,
                "task_id": tid,
                "stage": "driver-a-pr",
                "dimension": "quality",
                "vendor": "driver-a",
                "verdict": "approved",
                "evidence": None,
                "head_sha": "abc123",
                "agent_id": str(uuid.uuid4()),
                "recorded_at": utcnow(),
            },
        )
    finally:
        store.close()
    with pytest.raises(SystemExit) as not_done:
        main(["task", "state", tid, "done"])
    assert str(not_done.value) == f"agent: task is not done: summaries missing {HINT}"
