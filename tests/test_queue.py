"""Tests for queue.py decision logic. Pure module: no Store, no runner, no filesystem."""

from __future__ import annotations

from agent_cli.queue import (
    prunable,
    retention_days,
    retry_payload,
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


def test_state_counts_returns_an_empty_mapping_when_rows_is_not_a_list() -> None:
    # Without the guard this iterates the value instead of rejecting it.
    assert state_counts(None) == {}  # type: ignore[arg-type]


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


def test_retention_days_returns_none_when_the_runner_config_is_not_a_dict() -> None:
    # Without the guard this raises instead of answering "do not prune".
    assert retention_days(None, "done") is None  # type: ignore[arg-type]


def test_retention_days_returns_none_when_the_state_is_not_a_string() -> None:
    # A non-string key would look up successfully, so this fixture puts a
    # matching int key in the table: only the type guard rejects it.
    assert retention_days({"retention_days": {7: 7}}, 7) is None  # type: ignore[arg-type]


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


def test_prunable_returns_an_empty_list_when_rows_is_not_a_list() -> None:
    # Without the guard this raises on iteration rather than pruning nothing.
    assert prunable(None, state="done", now_epoch=1768435200, days=7) == []  # type: ignore[arg-type]


def test_prunable_skips_an_entry_of_rows_that_is_not_a_dict() -> None:
    # A malformed entry must not abort the pass or be read as a row.
    rows = [
        {"id": "a", "state": "done", "finished": "2026-01-01T00:00:00Z"},
        "not-a-dict",
        {"id": "b", "state": "done", "finished": "2026-01-01T00:00:00Z"},
    ]
    assert prunable(rows, state="done", now_epoch=1768435200, days=7) == ["a", "b"]  # type: ignore[list-item]


def test_prunable_returns_an_empty_list_when_now_epoch_is_not_an_int() -> None:
    rows = [{"id": "a", "state": "done", "finished": "2026-01-01T00:00:00Z"}]
    assert prunable(rows, state="done", now_epoch="1768435200", days=7) == []  # type: ignore[arg-type]


def test_prunable_returns_an_empty_list_for_a_negative_days_value() -> None:
    # A negative retention would push the cutoff into the future and delete a
    # row that just finished, so the guard here protects live data.
    rows = [{"id": "a", "state": "done", "finished": "2026-01-14T23:00:00Z"}]
    assert prunable(rows, state="done", now_epoch=1768435200, days=-1) == []


def test_prunable_returns_an_empty_list_for_a_bool_days_value() -> None:
    # bool is a subclass of int: True would silently mean a one-day retention
    # and prune a two-day-old row.
    rows = [{"id": "a", "state": "done", "finished": "2026-01-13T00:00:00Z"}]
    assert prunable(rows, state="done", now_epoch=1768435200, days=True) == []


# ---------------------------------------------------------------- retry_payload


def test_retry_payload_increments_attempts_by_one_on_a_normal_integer_value() -> None:
    # A normal integer tally must go up by one.
    row = {"id": "job-1", "state": "failed", "attempts": 2}
    result = retry_payload(row)
    assert result is not None
    assert result["attempts"] == 3


def test_retry_payload_returns_attempts_equal_to_one_when_the_row_has_no_attempts_key() -> None:
    # A missing attempts key is treated as zero, so the retry is attempt 1.
    row = {"id": "job-1", "state": "failed"}
    result = retry_payload(row)
    assert result is not None
    assert result["attempts"] == 1


def test_retry_payload_returns_attempts_equal_to_one_when_attempts_is_the_bool_true() -> None:
    # bool is a subclass of int, so True must not count as a tally of 1.
    row = {"id": "job-1", "state": "failed", "attempts": True}
    result = retry_payload(row)
    assert result is not None
    assert result["attempts"] == 1


def test_retry_payload_sets_state_to_queued() -> None:
    # A retried job must go back to queued so the runner can pick it up.
    row = {"id": "job-1", "state": "failed"}
    result = retry_payload(row)
    assert result is not None
    assert result["state"] == "queued"


def test_retry_payload_resets_the_previous_attempt_fields_to_none() -> None:
    # The previous attempt's runtime fields must not leak into the retry.
    row = {
        "id": "job-1",
        "state": "failed",
        "started": "2026-01-15T10:00:00Z",
        "session": "sess-1",
        "worktree": "/tmp/worktree-job-1",
        "finished": "2026-01-15T10:05:00Z",
        "exit_code": 1,
        "outcome": "failed",
        "done_kind": "error",
        "work_performed": True,
        "contract_followed": False,
    }
    result = retry_payload(row)
    assert result is not None
    assert result["started"] is None
    assert result["session"] is None
    assert result["worktree"] is None
    assert result["finished"] is None
    assert result["exit_code"] is None
    assert result["outcome"] is None
    assert result["done_kind"] is None
    assert result["work_performed"] is None
    assert result["contract_followed"] is None


# Without this reset a retried job never announces itself again and its
# old outcome counts as already reported, so the retry becomes invisible
def test_retry_payload_resets_reported_and_announced_to_false() -> None:
    # to the supervisor and to any UI that surfaces job status.
    row = {
        "id": "job-1",
        "state": "failed",
        "reported": True,
        "announced": True,
    }
    result = retry_payload(row)
    assert result is not None
    assert result["reported"] is False
    assert result["announced"] is False


# The stall check reads a missing progress field as "take a first
# baseline"; a None value would be a value the stall check would have
def test_retry_payload_omits_the_progress_keys_from_the_result_rather_than_setting_them_to_none() -> None:
    # to special-case instead of simply being absent.
    row = {
        "id": "job-1",
        "state": "failed",
        "progress_check_epoch": 1768435200,
        "progress_transcript_size": 4096,
        "progress_cpu_seconds": 12.5,
    }
    result = retry_payload(row)
    assert result is not None
    assert "progress_check_epoch" not in result
    assert "progress_transcript_size" not in result
    assert "progress_cpu_seconds" not in result


def test_retry_payload_leaves_unrelated_fields_untouched() -> None:
    # Fields that retry_payload does not own must survive the call unchanged.
    row = {
        "id": "job-1",
        "state": "failed",
        "repo": "DFXswiss/agent",
        "ref": "main",
        "job_type": "implement",
        "actor": "alice",
        "created_at": "2026-01-15T09:00:00Z",
    }
    result = retry_payload(row)
    assert result is not None
    assert result["repo"] == "DFXswiss/agent"
    assert result["ref"] == "main"
    assert result["job_type"] == "implement"
    assert result["actor"] == "alice"
    assert result["created_at"] == "2026-01-15T09:00:00Z"


def test_retry_payload_does_not_mutate_the_input_row() -> None:
    # The original row dict must keep its pre-call state and attempts.
    row = {"id": "job-1", "state": "failed", "attempts": 2}
    original_state = row["state"]
    original_attempts = row["attempts"]
    retry_payload(row)
    assert row["state"] == original_state
    assert row["attempts"] == original_attempts


def test_retry_payload_returns_none_for_a_non_dict_input() -> None:
    # Wrong-shaped input yields None, never a half-built retry row.
    assert retry_payload("not-a-dict") is None  # type: ignore[arg-type]
    assert retry_payload(None) is None  # type: ignore[arg-type]


def test_retry_payload_returns_none_for_a_dict_row_that_has_no_id_key() -> None:
    # A row without an id cannot be retried.
    assert retry_payload({"state": "failed"}) is None


def test_retry_payload_returns_none_for_a_dict_row_whose_id_is_an_empty_string() -> None:
    # An empty id is not a job id, so there is nothing to retry.
    assert retry_payload({"id": "", "state": "failed"}) is None
