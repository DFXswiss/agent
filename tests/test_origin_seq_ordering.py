"""Direct proof that gate/check "latest" selection follows payload origin_seq."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.chain import _latest_agent
from agent_cli.main import (
    _chain_snapshot,
    _latest_checks,
    _latest_gates,
    _origin_seq_sort_key,
    load_task_dict,
)
from agent_cli.store import Store, dumps
from test_cli import _last_agent_id, _last_task_id, run


def _insert_legacy_row(store: Store, table: str, row_id: str, payload: dict) -> None:
    """Write directly into row_data, bypassing _write_in_txn's origin_seq auto-stamp —
    simulates a genuinely pre-origin_seq legacy row (no origin_seq key at all)."""
    origin = store.device_id()
    with store._lock, store.conn.transaction():
        store._upsert_row(
            table, row_id, origin, dumps(payload), str(payload.get("recorded_at") or "")
        )


def test_latest_gates_prefers_higher_origin_seq_at_same_timestamp(tmp_path: Path) -> None:
    """Same-second recorded_at must not hide a later write: higher origin_seq wins."""
    store = Store(tmp_path)
    try:
        same_ts = "2026-04-01T12:00:00Z"
        tid = "task-seq-order"
        store.write(
            "review_gate",
            "insert",
            "g-old",
            {
                "id": "g-old",
                "task_id": tid,
                "stage": "grok-pr",
                "dimension": "quality",
                "vendor": "grok",
                "verdict": "rejected",
                "evidence": "stale",
                "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "agent_id": "a1",
                "recorded_at": same_ts,
                "origin_seq": 10,
            },
        )
        store.write(
            "review_gate",
            "insert",
            "g-new",
            {
                "id": "g-new",
                "task_id": tid,
                "stage": "grok-pr",
                "dimension": "quality",
                "vendor": "grok",
                "verdict": "approved",
                "evidence": None,
                "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "agent_id": "a2",
                "recorded_at": same_ts,
                "origin_seq": 11,
            },
        )
        latest = _latest_gates(store, tid)
        got = latest[("grok-pr", "quality")]
        old = store.row("review_gate", "g-old")
        new = store.row("review_gate", "g-new")
        assert old is not None and new is not None
        assert "origin_seq" in old and "origin_seq" in new
        assert new["origin_seq"] > old["origin_seq"]
        assert got["id"] == "g-new"
        assert got["verdict"] == "approved"
        assert got["origin_seq"] == new["origin_seq"]
    finally:
        store.close()


def test_latest_checks_prefers_higher_origin_seq_at_same_timestamp(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        same_ts = "2026-04-01T12:00:00Z"
        tid = "task-check-seq"
        store.write(
            "local_check",
            "insert",
            "c-old",
            {
                "id": "c-old",
                "task_id": tid,
                "name": "pytest",
                "command": "pytest",
                "result": "fail",
                "output": "stale",
                "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "ran_at": same_ts,
                "origin_seq": 3,
            },
        )
        store.write(
            "local_check",
            "insert",
            "c-new",
            {
                "id": "c-new",
                "task_id": tid,
                "name": "pytest",
                "command": "pytest",
                "result": "pass",
                "output": None,
                "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "ran_at": same_ts,
                "origin_seq": 9,
            },
        )
        latest = _latest_checks(store, tid)
        assert latest["pytest"]["id"] == "c-new"
        assert latest["pytest"]["result"] == "pass"
    finally:
        store.close()


def test_load_task_dict_gate_order_follows_origin_seq(tmp_path: Path) -> None:
    """load_task_dict lists gates oldest→newest by origin_seq so last-wins is correct."""
    store = Store(tmp_path)
    try:
        tid = "task-load-order"
        same_ts = "2026-04-01T12:00:00Z"
        store.write(
            "task",
            "insert",
            tid,
            {
                "id": tid,
                "session_id": "s",
                "workflow": "implement",
                "state": "implementing",
                "title": "t",
                "change_summary_en": "",
                "change_summary_de": "",
                "payload": {},
            },
        )
        store.write(
            "review_gate",
            "insert",
            "g1",
            {
                "id": "g1",
                "task_id": tid,
                "stage": "grok-pr",
                "dimension": "logic",
                "vendor": "grok",
                "verdict": "rejected",
                "head_sha": "aa",
                "recorded_at": same_ts,
                "origin_seq": 2,
            },
        )
        store.write(
            "review_gate",
            "insert",
            "g2",
            {
                "id": "g2",
                "task_id": tid,
                "stage": "grok-pr",
                "dimension": "logic",
                "vendor": "grok",
                "verdict": "approved",
                "head_sha": "bb",
                "recorded_at": same_ts,
                "origin_seq": 8,
            },
        )
        snap = load_task_dict(store, tid)
        logic = [g for g in snap["gates"] if g.get("dimension") == "logic"]
        assert [g["verdict"] for g in logic] == ["rejected", "approved"]
    finally:
        store.close()


def test_missing_origin_seq_sorts_before_stamped_rows(tmp_path: Path) -> None:
    """Pre-change rows without origin_seq are older than any stamped row."""
    store = Store(tmp_path)
    try:
        tid = "task-legacy"
        _insert_legacy_row(
            store,
            "review_gate",
            "g-legacy",
            {
                "id": "g-legacy",
                "task_id": tid,
                "stage": "grok-pr",
                "dimension": "quality",
                "vendor": "grok",
                "verdict": "approved",
                "head_sha": "old",
                "recorded_at": "2026-12-31T23:59:59Z",
            },
        )
        store.write(
            "review_gate",
            "insert",
            "g-stamped",
            {
                "id": "g-stamped",
                "task_id": tid,
                "stage": "grok-pr",
                "dimension": "quality",
                "vendor": "grok",
                "verdict": "rejected",
                "head_sha": "new",
                "recorded_at": "2026-01-01T00:00:00Z",
            },
        )
        legacy = next(
            r for r in store.rows("review_gate") if r.get("id") == "g-legacy"
        )
        assert "origin_seq" not in legacy
        latest = _latest_gates(store, tid)
        assert latest[("grok-pr", "quality")]["id"] == "g-stamped"
    finally:
        store.close()


def test_origin_seq_sort_key_missing_sorts_before_stamped() -> None:
    """Hand-built dicts without origin_seq sort before any stamped row, even with a newer timestamp."""
    missing = _origin_seq_sort_key({"recorded_at": "2099-01-01"}, "recorded_at")
    stamped = _origin_seq_sort_key(
        {"recorded_at": "2000-01-01", "origin_seq": 1}, "recorded_at"
    )
    assert missing < stamped


def test_check_record_stamps_origin_seq_via_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Real check record stamps origin_seq inside write(); later call wins latest."""
    run(tmp_path, ["init"])
    run(
        tmp_path,
        [
            "session",
            "register",
            "--id",
            "s",
            "--kind",
            "human",
            "--skill",
            "spine",
            "--skill",
            "review-loop",
            "--skill",
            "pr-review",
        ],
    )
    run(
        tmp_path,
        ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"],
    )
    tid = _last_task_id(capsys.readouterr().out)

    run(
        tmp_path,
        [
            "check",
            "record",
            "--task",
            tid,
            "--name",
            "pytest",
            "--command",
            "pytest -q",
            "--result",
            "fail",
            "--output",
            "stale fail",
        ],
    )
    run(
        tmp_path,
        [
            "check",
            "record",
            "--task",
            tid,
            "--name",
            "pytest",
            "--command",
            "pytest -q",
            "--result",
            "pass",
        ],
    )

    store = Store(tmp_path)
    try:
        checks = [
            c
            for c in store.rows("local_check")
            if c.get("task_id") == tid and c.get("name") == "pytest"
        ]
        assert len(checks) == 2
        by_result = {c["result"]: c for c in checks}
        assert "origin_seq" in by_result["fail"]
        assert "origin_seq" in by_result["pass"]
        assert by_result["pass"]["origin_seq"] > by_result["fail"]["origin_seq"]
        latest = _latest_checks(store, tid)
        assert latest["pytest"]["id"] == by_result["pass"]["id"]
        assert latest["pytest"]["result"] == "pass"
    finally:
        store.close()


def test_agent_finish_does_not_bump_origin_seq_so_latest_reviewer_is_retry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """agent finish must keep insert-time origin_seq; unavailable retry stays latest.

    Without insert-only stamping, finish rewrites origin_seq to a later ledger
    seq and can reorder the released reviewer past a later retry in
    agents_ordered / _latest_agent.
    """
    run(tmp_path, ["init"])
    run(
        tmp_path,
        [
            "session",
            "register",
            "--id",
            "s",
            "--kind",
            "human",
            "--skill",
            "spine",
            "--skill",
            "review-loop",
            "--skill",
            "pr-review",
        ],
    )
    run(
        tmp_path,
        ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"],
    )
    tid = _last_task_id(capsys.readouterr().out)

    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "reviewer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    released_id = _last_agent_id(capsys.readouterr().out)

    store = Store(tmp_path)
    try:
        released_at_start = store.row("agent", released_id)
        assert released_at_start is not None
        assert "origin_seq" in released_at_start
        released_seq_at_insert = int(released_at_start["origin_seq"])
    finally:
        store.close()

    run(
        tmp_path,
        [
            "agent",
            "finish",
            "--id",
            released_id,
            "--verdict",
            "unavailable",
            "--note",
            "released-unavailable",
        ],
    )
    capsys.readouterr()

    store = Store(tmp_path)
    try:
        released_after_finish = store.row("agent", released_id)
        assert released_after_finish is not None
        assert released_after_finish["status"] == "done"
        assert int(released_after_finish["origin_seq"]) == released_seq_at_insert
    finally:
        store.close()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "reviewer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    real_id = _last_agent_id(capsys.readouterr().out)

    store = Store(tmp_path)
    try:
        real_at_start = store.row("agent", real_id)
        assert real_at_start is not None
        assert "origin_seq" in real_at_start
        real_seq_at_insert = int(real_at_start["origin_seq"])
        assert real_seq_at_insert > released_seq_at_insert
    finally:
        store.close()

    run(
        tmp_path,
        [
            "agent",
            "finish",
            "--id",
            real_id,
            "--verdict",
            "approved",
            "--note",
            "real-approved",
        ],
    )
    capsys.readouterr()

    store = Store(tmp_path)
    try:
        released = store.row("agent", released_id)
        real = store.row("agent", real_id)
        assert released is not None and real is not None
        assert int(released["origin_seq"]) == released_seq_at_insert
        assert int(real["origin_seq"]) == real_seq_at_insert
        assert int(real["origin_seq"]) > int(released["origin_seq"])

        reviewers = [
            a
            for a in store.rows("agent")
            if a.get("task_id") == tid and a.get("role") == "reviewer"
        ]
        reviewers.sort(key=lambda a: int(a["origin_seq"]))
        assert [a["id"] for a in reviewers] == [released_id, real_id]

        snap = _chain_snapshot(store, tid)
        latest = _latest_agent(snap, "reviewer", "grok")
        assert latest is not None
        assert latest["note"] == "real-approved"
        assert latest["status"] == "done"
    finally:
        store.close()
