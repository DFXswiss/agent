"""Tests for queue.py decision logic. Pure module: no Store, no runner, no filesystem."""

from __future__ import annotations

from agent_cli.queue import (
    prunable,
    retention_days,
    state_counts,
)


# ---------------------------------------------------------------- state_counts


def test_state_counts_counts_rows_per_state_across_multiple_state_values() -> None:
    rows = [
        {"state": "queued"},
        {"state": "running"},
        {"state": "queued"},
        {"state": "done"},
        {"state": "running"},
        {"state": "queued"},
    ]
    assert state_counts(rows) == {"queued": 3, "running": 2, "done": 1}


def test_state_counts_ignores_a_row_that_is_not_a_dict() -> None:
    rows = [
        {"state": "queued"},
        "not-a-dict",
        None,
        {"state": "queued"},
    ]
    assert state_counts(rows) == {"queued": 2}  # type: ignore[arg-type]


def test_state_counts_ignores_a_dict_row_whose_state_key_is_missing() -> None:
    rows = [
        {"state": "queued"},
        {"id": "job-1"},
        {"state": "queued"},
    ]
    assert state_counts(rows) == {"queued": 2}


def test_state_counts_ignores_a_dict_row_whose_state_is_not_a_non_empty_string() -> None:
    rows = [
        {"state": "queued"},
        {"state": ""},
        {"state": 7},
        {"state": "queued"},
    ]
    assert state_counts(rows) == {"queued": 2}


def test_state_counts_omits_states_that_do_not_appear_in_any_row() -> None:
    assert state_counts([{"state": "queued"}]) == {"queued": 1}


# ---------------------------------------------------------------- retention_days


def test_retention_days_returns_a_valid_non_negative_int_unchanged() -> None:
    assert retention_days({"retention_days": {"done": 7}}, "done") == 7


def test_retention_days_returns_none_when_the_retention_days_table_is_missing() -> None:
    assert retention_days({"other": 1}, "done") is None


def test_retention_days_returns_none_when_the_requested_state_key_is_missing() -> None:
    assert retention_days({"retention_days": {"done": 7}}, "failed") is None


def test_retention_days_returns_none_for_a_non_int_value() -> None:
    assert retention_days({"retention_days": {"done": "7"}}, "done") is None


def test_retention_days_returns_none_for_a_negative_int() -> None:
    assert retention_days({"retention_days": {"done": -1}}, "done") is None


def test_retention_days_returns_none_for_a_bool_value() -> None:
    # bool is a subclass of int, so True must be rejected explicitly.
    assert retention_days({"retention_days": {"done": True}}, "done") is None


# ---------------------------------------------------------------- prunable


def test_prunable_returns_a_row_whose_finished_timestamp_is_strictly_before_the_cutoff() -> None:
    # 1768435200 is 2026-01-15T00:00:00Z; with days=7 the cutoff is 2026-01-08T00:00:00Z.
    assert (
        prunable(
            [{"id": "job-1", "state": "done", "finished": "2026-01-01T00:00:00Z"}],
            state="done",
            now_epoch=1768435200,
            days=7,
        )
        == ["job-1"]
    )


def test_prunable_does_not_return_a_row_whose_finished_timestamp_is_after_the_cutoff() -> None:
    assert (
        prunable(
            [{"id": "job-1", "state": "done", "finished": "2026-01-10T00:00:00Z"}],
            state="done",
            now_epoch=1768435200,
            days=7,
        )
        == []
    )


def test_prunable_does_not_return_a_row_whose_finished_timestamp_is_exactly_at_the_cutoff() -> None:
    # Comparison is strictly older, so equality with the cutoff is not prunable.
    assert (
        prunable(
            [{"id": "job-1", "state": "done", "finished": "2026-01-08T00:00:00Z"}],
            state="done",
            now_epoch=1768435200,
            days=7,
        )
        == []
    )


def test_prunable_does_not_return_a_row_whose_finished_field_is_missing() -> None:
    assert (
        prunable(
            [{"id": "job-1", "state": "done"}],
            state="done",
            now_epoch=1768435200,
            days=7,
        )
        == []
    )


def test_prunable_does_not_return_a_row_whose_finished_field_is_not_a_valid_timestamp() -> None:
    assert (
        prunable(
            [{"id": "job-1", "state": "done", "finished": "not-a-time"}],
            state="done",
            now_epoch=1768435200,
            days=7,
        )
        == []
    )


def test_prunable_ignores_a_row_in_a_different_state_even_when_its_finished_time_qualifies() -> None:
    assert (
        prunable(
            [{"id": "job-1", "state": "running", "finished": "2026-01-01T00:00:00Z"}],
            state="done",
            now_epoch=1768435200,
            days=7,
        )
        == []
    )


def test_prunable_ignores_a_row_whose_id_is_missing_or_not_a_non_empty_string() -> None:
    rows = [
        {"state": "done", "finished": "2026-01-01T00:00:00Z"},
        {"id": "", "state": "done", "finished": "2026-01-01T00:00:00Z"},
        {"id": 123, "state": "done", "finished": "2026-01-01T00:00:00Z"},
        {"id": "keep", "state": "done", "finished": "2026-01-01T00:00:00Z"},
    ]
    assert prunable(rows, state="done", now_epoch=1768435200, days=7) == ["keep"]


def test_prunable_preserves_input_row_order_when_several_rows_qualify() -> None:
    rows = [
        {"id": "c", "state": "done", "finished": "2026-01-01T00:00:00Z"},
        {"id": "a", "state": "done", "finished": "2026-01-01T00:00:00Z"},
        {"id": "b", "state": "done", "finished": "2026-01-01T00:00:00Z"},
    ]
    assert prunable(rows, state="done", now_epoch=1768435200, days=7) == ["c", "a", "b"]
