"""U-RUN-reviews (KOR-51, ADR-094 section 5): the optional `merge_criteria` and `separate_duties` of a
`settings.workgroup` body; any other shape refuses the event."""

from __future__ import annotations

import pytest

from agent_runner.operations.settings import MergeCriteria, SettingsInvalid, parse

BASE = {"repositories": [], "protected_branches": [], "pr_draft_default": True}


def test_defaults_require_nothing() -> None:
    found = parse(BASE)
    assert (found.merge_criteria, found.separate_duties) == (MergeCriteria(), False)


def test_criteria_and_separation() -> None:
    found = parse(
        {
            **BASE,
            "merge_criteria": {
                "passed_verdicts": 2,
                "verdict_profile": "qa",
                "checks_green": True,
                "github_approvals": 1,
            },
            "separate_duties": True,
        }
    )
    assert found.merge_criteria == MergeCriteria(2, "qa", True, 1)
    assert found.separate_duties is True


@pytest.mark.parametrize(
    "extra",
    [
        {"merge_criteria": []},
        {"merge_criteria": {"other": 1}},
        {"merge_criteria": {"passed_verdicts": -1}},
        {"merge_criteria": {"passed_verdicts": True}},
        {"merge_criteria": {"verdict_profile": "Bad"}},
        {"merge_criteria": {"verdict_profile": 1}},
        {"merge_criteria": {"checks_green": "yes"}},
        {"separate_duties": 1},
        {"unknown": True},
    ],
)
def test_other_shapes_are_refused(extra: dict[str, object]) -> None:
    with pytest.raises(SettingsInvalid):
        parse({**BASE, **extra})
