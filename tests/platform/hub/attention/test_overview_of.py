"""U-HUB-attention (the overview of the open tasks, ADR-103): every attention record falls into exactly one of the three
groups of the start page and the task dashboard, except a closed task."""

from __future__ import annotations

import pytest

from agent_hub.attention.derive import CATEGORIES, OVERVIEW, overview_of


@pytest.mark.parametrize(
    ("category", "reason", "expected"),
    [
        ("working", "working", "working"),
        ("working", "quota_wait", "working"),
        ("working", "queued", "working"),
        ("needs_decision", "question", "waiting"),
        ("needs_decision", "device_missing", "waiting"),
        ("blocked", "session_failed", "no_agent"),
        # a captured task that was never started, and a task that lies idle
        ("done", "idle", "no_agent"),
        ("done", "closed", None),
        ("unknown", "x", None),
    ],
)
def test_one_overview_per_record(category: str, reason: str, expected: str | None) -> None:
    assert overview_of(category, reason) == expected


def test_every_category_is_in_one_overview() -> None:
    named = [category for categories in OVERVIEW.values() for category in categories]
    assert sorted(named) == sorted(CATEGORIES)
