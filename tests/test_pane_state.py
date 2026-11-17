from __future__ import annotations

from dataclasses import replace

from agent_cli.pane_state import pane_is_idle, pane_is_working, pane_permission_prompt
from driver_fixtures import PANE_IDLE, PANE_PERMISSION, PANE_WORKING, pane_markers

MARKERS = pane_markers("driver-a")


def test_working_marker_is_working() -> None:
    assert pane_is_working(PANE_WORKING, MARKERS) is True
    assert pane_is_idle(PANE_WORKING, MARKERS) is False


def test_every_working_pattern_counts() -> None:
    pane = "  tool call in flight                          4.1s [stop]\n"
    assert pane_is_working(pane, MARKERS) is True


def test_working_pattern_keeps_its_own_flags() -> None:
    # The first pattern is case-insensitive; the second matches exactly.
    assert pane_is_working("  WORKING... 2s\n", MARKERS) is True
    assert pane_is_working("  [STOP]\n", MARKERS) is False


def test_idle_prompt_is_not_working() -> None:
    assert pane_is_working(PANE_IDLE, MARKERS) is False
    assert pane_is_idle(PANE_IDLE, MARKERS) is True


def test_permission_prompt_is_not_working_and_not_idle() -> None:
    assert pane_is_working(PANE_PERMISSION, MARKERS) is False
    assert pane_permission_prompt(PANE_PERMISSION, MARKERS) is True
    assert pane_is_idle(PANE_PERMISSION + "  > \n", MARKERS) is False


def test_permission_marker_without_confirmation_is_not_a_prompt() -> None:
    pane = "  The guide says the modal asks: approve?\n  > \n"
    assert pane_permission_prompt(pane, MARKERS) is False
    assert pane_is_idle(pane, MARKERS) is True


def test_permission_confirmation_is_case_insensitive() -> None:
    assert pane_permission_prompt("  Approve? edit a.txt\n  TAB:NEXT\n", MARKERS) is True


def test_queued_group_needs_every_part() -> None:
    assert pane_is_working("  1 Queued, press Enter to SEND NOW\n", MARKERS) is True
    assert pane_is_working("  1 queued\n  > \n", MARKERS) is False


def test_markers_without_queued_groups_ignore_queue_text() -> None:
    markers = replace(MARKERS, queued=())
    assert pane_is_working("  1 queued, send now\n", markers) is False


def test_empty_pane_is_neither_working_nor_idle() -> None:
    assert pane_is_working("", MARKERS) is False
    assert pane_permission_prompt("", MARKERS) is False
    assert pane_is_idle("", MARKERS) is False


def test_ansi_sequences_are_stripped_before_matching() -> None:
    pane = "\x1b[32m  working...\x1b[0m  2.0s\n"
    assert pane_is_working(pane, MARKERS) is True


def test_history_text_without_a_marker_is_not_working() -> None:
    pane = (
        "  task started: sync the host app\n"
        "  task completed in 5m48s\n"
        "  Status bar: model-a\n"
    )
    assert pane_is_working(pane, MARKERS) is False
    assert pane_is_idle(pane, MARKERS) is False
