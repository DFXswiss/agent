"""Tests for watchdog.py decision logic. Pure module: no Store, no runner, no filesystem."""

from __future__ import annotations

import pytest

from agent_cli.watchdog import (
    cpu_seconds,
    is_overdue,
    iso_epoch,
    pane_pids_argv,
    parse_pane_pids,
    ps_snapshot_argv,
    stall_verdict,
    tree_cpu_seconds,
)


# ---------------------------------------------------------------- argv builders


def test_pane_pids_argv_returns_exact_8_element_list_with_equals_prefix() -> None:
    # The `-t` value carries the `=` exact-match prefix. Without it tmux matches
    # a prefix, so one session would be reported under another's name.
    assert pane_pids_argv("/tmp/sock", "job-1") == [
        "tmux",
        "-S",
        "/tmp/sock",
        "list-panes",
        "-t",
        "=job-1",
        "-F",
        "#{pane_pid}",
    ]


def test_ps_snapshot_argv_returns_exact_3_element_list() -> None:
    assert ps_snapshot_argv() == ["ps", "-axo", "pid=,ppid=,time="]


def test_parse_pane_pids_drops_blank_lines_strips_whitespace_preserves_order() -> None:
    text = "  12345  \n\n  67890\n\n"
    assert parse_pane_pids(text) == ["12345", "67890"]


# ---------------------------------------------------------------- cpu_seconds


def test_cpu_seconds_parses_plain_seconds() -> None:
    assert cpu_seconds("12") == 12


def test_cpu_seconds_parses_minutes_seconds() -> None:
    assert cpu_seconds("1:30") == 90


def test_cpu_seconds_parses_hours_minutes_seconds() -> None:
    assert cpu_seconds("2:03:04") == 7384


def test_cpu_seconds_parses_days_hours_minutes_seconds() -> None:
    assert cpu_seconds("1-02:03:04") == 93784


def test_cpu_seconds_drops_fractional_part() -> None:
    assert cpu_seconds("1:30.75") == 90


def test_cpu_seconds_returns_none_for_four_colon_fields() -> None:
    assert cpu_seconds("1:2:3:4") is None


def test_cpu_seconds_returns_none_for_non_numeric() -> None:
    assert cpu_seconds("abc") is None


def test_cpu_seconds_returns_none_for_empty_string() -> None:
    assert cpu_seconds("") is None


def test_cpu_seconds_treats_leading_zero_as_decimal_not_octal() -> None:
    assert cpu_seconds("08:00") == 480


# ---------------------------------------------------------------- tree_cpu_seconds


def test_tree_cpu_seconds_sums_pane_and_children() -> None:
    snapshot = "100 1 10\n101 100 20\n"
    assert tree_cpu_seconds(snapshot, ["100"]) == 30


def test_tree_cpu_seconds_reaches_grandchild_before_parent_in_snapshot() -> None:
    # Grandchild (103) appears before its parent (102) in the snapshot.
    snapshot = "103 102 5\n102 100 10\n100 1 1\n"
    assert tree_cpu_seconds(snapshot, ["100"]) == 16


def test_tree_cpu_seconds_skips_lines_that_do_not_split_into_three_fields() -> None:
    snapshot = "100 1 10\nbadline\n101 100 20\n"
    assert tree_cpu_seconds(snapshot, ["100"]) == 30


def test_tree_cpu_seconds_returns_none_when_no_pane_pid_appears() -> None:
    snapshot = "200 1 10\n"
    assert tree_cpu_seconds(snapshot, ["100"]) is None


def test_tree_cpu_seconds_returns_zero_when_processes_present_but_no_cpu_used() -> None:
    snapshot = "100 1 0\n101 100 0\n"
    assert tree_cpu_seconds(snapshot, ["100"]) == 0


def test_tree_cpu_seconds_returns_none_when_selected_time_is_unparseable() -> None:
    snapshot = "100 1 abc\n"
    assert tree_cpu_seconds(snapshot, ["100"]) is None


def test_tree_cpu_seconds_ignores_unrelated_processes() -> None:
    snapshot = "100 1 10\n200 1 999\n"
    assert tree_cpu_seconds(snapshot, ["100"]) == 10


# ---------------------------------------------------------------- iso_epoch


def test_iso_epoch_roundtrips_valid_stamp() -> None:
    assert iso_epoch("2026-01-01T00:00:00Z") == 1767225600


def test_iso_epoch_returns_none_for_malformed_stamp() -> None:
    assert iso_epoch("not-a-time") is None


def test_iso_epoch_returns_none_for_non_string() -> None:
    assert iso_epoch(123) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------- is_overdue


def test_is_overdue_returns_true_past_budget() -> None:
    assert is_overdue("2026-01-01T00:00:00Z", 1767229200, 30) is True


def test_is_overdue_returns_false_inside_budget() -> None:
    assert is_overdue("2026-01-01T00:00:00Z", 1767227400, 30) is False


def test_is_overdue_returns_false_exactly_at_budget() -> None:
    # Strictly greater than, so equality is not overdue.
    assert is_overdue("2026-01-01T00:00:00Z", 1767227400, 60) is False


def test_is_overdue_returns_none_for_unparseable_started() -> None:
    assert is_overdue("bad", 1767229200, 30) is None


def test_is_overdue_returns_none_for_negative_timeout_minutes() -> None:
    assert is_overdue("2026-01-01T00:00:00Z", 1767229200, -1) is None


def test_is_overdue_returns_none_for_bool_timeout_minutes() -> None:
    assert is_overdue("2026-01-01T00:00:00Z", 1767229200, True) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------- stall_verdict


def test_stall_verdict_returns_baseline_for_non_integer_last_check() -> None:
    assert (
        stall_verdict(
            now_epoch=100,
            transcript_size=10,
            cpu=5,
            last_check="x",
            last_size=0,
            last_cpu=0,
            stall_minutes=5,
        )
        == "baseline"
    )


def test_stall_verdict_returns_baseline_for_non_integer_last_size() -> None:
    assert (
        stall_verdict(
            now_epoch=100,
            transcript_size=10,
            cpu=5,
            last_check=90,
            last_size=None,
            last_cpu=0,
            stall_minutes=5,
        )
        == "baseline"
    )


def test_stall_verdict_returns_baseline_for_non_integer_last_cpu() -> None:
    assert (
        stall_verdict(
            now_epoch=100,
            transcript_size=10,
            cpu=5,
            last_check=90,
            last_size=0,
            last_cpu="x",
            stall_minutes=5,
        )
        == "baseline"
    )


def test_stall_verdict_returns_wait_inside_stall_window() -> None:
    assert (
        stall_verdict(
            now_epoch=100,
            transcript_size=10,
            cpu=5,
            last_check=99,
            last_size=0,
            last_cpu=0,
            stall_minutes=5,
        )
        == "wait"
    )


def test_stall_verdict_returns_stalled_when_neither_signal_moved() -> None:
    assert (
        stall_verdict(
            now_epoch=400,
            transcript_size=10,
            cpu=5,
            last_check=100,
            last_size=10,
            last_cpu=5,
            stall_minutes=1,
        )
        == "stalled"
    )


def test_stall_verdict_returns_progress_when_transcript_grew_but_cpu_did_not() -> None:
    assert (
        stall_verdict(
            now_epoch=400,
            transcript_size=20,
            cpu=5,
            last_check=100,
            last_size=10,
            last_cpu=5,
            stall_minutes=1,
        )
        == "progress"
    )


def test_stall_verdict_returns_progress_when_cpu_grew_but_transcript_did_not() -> None:
    assert (
        stall_verdict(
            now_epoch=400,
            transcript_size=10,
            cpu=15,
            last_check=100,
            last_size=10,
            last_cpu=5,
            stall_minutes=1,
        )
        == "progress"
    )
