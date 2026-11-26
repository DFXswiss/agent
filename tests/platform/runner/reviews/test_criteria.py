"""U-RUN-reviews (KOR-51, ADR-094 sections 5 and 6): the formal merge criteria as a pure function - without criteria
nothing is required, verdicts count only on exactly the head, from sessions with the criteria's profile, never the
merging session's or, with separate duties, a writing session's, one per session; a rejection blocks; checks and
GitHub approvals."""

from __future__ import annotations

from datetime import UTC, datetime

from agent_runner.operations.settings import MergeCriteria
from agent_runner.reviews import criteria
from agent_runner.reviews.store import Verdict

HEAD = "a" * 40
AT = datetime(2026, 11, 25, tzinfo=UTC)


def verdict(session: str, outcome: str = "passed", head: str = HEAD, profile: str | None = "review") -> Verdict:
    return Verdict(f"v-{session}-{outcome}", session, "task", head, outcome, profile, AT)


def facts(*verdicts: Verdict, committers: frozenset[str] = frozenset(), **extra: object) -> criteria.Facts:
    return criteria.Facts(HEAD, "merger", tuple(verdicts), committers, **extra)  # type: ignore[arg-type]


def test_without_criteria_nothing_is_required() -> None:
    assert criteria.refusal(MergeCriteria(), False, facts()) is None
    assert criteria.refusal(MergeCriteria(), True, facts()) is None
    assert criteria.refusal(MergeCriteria(), True, facts(committers=frozenset({"merger"}))) == criteria.SEPARATE


def test_which_verdicts_count() -> None:
    wanted = MergeCriteria(passed_verdicts=2)
    found = facts(
        verdict("a"),
        verdict("a"),  # one per session
        verdict("b", head="b" * 40),  # another head
        verdict("c", profile="development"),
        verdict("d", profile=None),
        verdict("merger"),  # the merging session's own
        verdict("writer"),
        committers=frozenset({"writer"}),
    )
    passed, rejected = criteria.counted(wanted, True, found)
    assert ([v.session_id for v in passed], rejected) == (["a"], [])
    assert "2 passed verdict(s)" in (criteria.refusal(wanted, True, found) or "")
    passed, _ = criteria.counted(wanted, False, found)
    assert sorted(v.session_id for v in passed) == ["a", "writer"]
    assert criteria.refusal(wanted, False, found) is None
    assert "rejected this head" in (
        criteria.refusal(wanted, False, facts(verdict("a"), verdict("e", "rejected"))) or ""
    )


def test_checks_and_approvals() -> None:
    green = MergeCriteria(checks_green=True)
    assert criteria.refusal(green, False, facts(checks_state="success")) is None
    assert "checks are pending" in (criteria.refusal(green, False, facts(checks_state="pending")) or "")
    assert "none (the head has no check)" in (criteria.refusal(green, False, facts(checks_state="none")) or "")
    approved = MergeCriteria(github_approvals=2)
    assert "2 approving GitHub review(s)" in (
        criteria.refusal(approved, False, facts(approvals=frozenset({"x"}))) or ""
    )
    assert criteria.refusal(approved, False, facts(approvals=frozenset({"x", "y"}))) is None


def test_approvals_of_the_head() -> None:
    reviews = [
        ("x", "APPROVED", HEAD),
        ("y", "APPROVED", "b" * 40),
        ("z", "APPROVED", HEAD),
        ("z", "CHANGES_REQUESTED", HEAD),
        (None, "APPROVED", HEAD),
    ]
    assert criteria.approvals_of(reviews, HEAD) == frozenset({"x"})
