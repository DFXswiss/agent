"""The formal merge criteria of a workgroup and the separation of duties (ADR-093 sections 5 and 6; §10 point 7:
deterministic and formal, never on content).

`refusal(...)` is a pure function of what the runner read: the workgroup's `MergeCriteria` and `separate_duties`, the
pull request's head, the verdicts the device knows, the sessions that made or pushed a commit of the pull request, the
state of the head's checks and GitHub's approvals. It answers the sentence of the first criterion not met (E-05: what
is required, what is there, what to do), None when the merge may go on. Without criteria (the default) nothing is
required: GitHub's own rules decide at the merge call (E-11).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from agent_runner.operations.settings import MergeCriteria
from agent_runner.reviews.store import Verdict

SEPARATE = (
    "This session made or pushed a commit of this pull request; with separate duties another session reviews and merges"
    " it."
)


@dataclass(frozen=True)
class Facts:
    head_sha: str
    session_id: str
    verdicts: tuple[Verdict, ...]
    committers: frozenset[str]
    """Sessions that made or pushed a commit of the pull request."""
    checks_state: str | None = None
    """`success`, `failure`, `pending` or `none`; None when not read (no criterion needs it)."""
    approvals: frozenset[str] = frozenset()
    """GitHub accounts that approved exactly the head."""


def active(criteria: MergeCriteria, separate: bool) -> bool:
    """True when the workgroup set any merge rule (then the default branch changes only through `github.merge`)."""
    return separate or criteria.passed_verdicts > 0 or criteria.checks_green or criteria.github_approvals > 0


def counted(criteria: MergeCriteria, separate: bool, facts: Facts) -> tuple[list[Verdict], list[Verdict]]:
    """The passed and rejected verdicts that count: on exactly the head, from sessions with the criteria's profile,
    never the merging session's, and with separate duties never one of a session that wrote the pull request."""
    fitting = [
        verdict
        for verdict in facts.verdicts
        if verdict.head_sha == facts.head_sha
        and verdict.profile == criteria.verdict_profile
        and verdict.session_id != facts.session_id
        and not (separate and verdict.session_id in facts.committers)
    ]
    passed = list({v.session_id: v for v in fitting if v.outcome == "passed"}.values())
    rejected = [v for v in fitting if v.outcome == "rejected"]
    return passed, rejected


def refusal(criteria: MergeCriteria, separate: bool, facts: Facts) -> str | None:
    """The sentence of the first criterion not met (module docstring), None when every one is met."""
    if separate and facts.session_id in facts.committers:
        return SEPARATE
    if criteria.passed_verdicts > 0:
        passed, rejected = counted(criteria, separate, facts)
        if rejected:
            return (
                f"A session with profile {criteria.verdict_profile} rejected this head; fix it and get a new verdict on"
                " the new head."
            )
        if len(passed) < criteria.passed_verdicts:
            return (
                f"{criteria.passed_verdicts} passed verdict(s) of sessions with profile {criteria.verdict_profile} on"
                f" this head are required, {len(passed)} found on this device. Request a review (subagent.request with"
                f" profile {criteria.verdict_profile}, then review.verdict)."
            )
    if criteria.checks_green and facts.checks_state != "success":
        state = "none (the head has no check)" if facts.checks_state == "none" else facts.checks_state
        return f"Every check of this head must have succeeded; the checks are {state}. Wait for them or fix them."
    if criteria.github_approvals > len(facts.approvals):
        return (
            f"{criteria.github_approvals} approving GitHub review(s) of this head are required, {len(facts.approvals)}"
            " found. Ask a person to approve the pull request at GitHub."
        )
    return None


def approvals_of(reviews: Iterable[tuple[str | None, str, str | None]], head_sha: str) -> frozenset[str]:
    """The accounts whose newest review of `head_sha` approved it (`(author, state, commit_id)` oldest first)."""
    newest: dict[str, str] = {}
    for author, state, commit in reviews:
        if author is not None and commit == head_sha:
            newest[author] = state
    return frozenset(author for author, state in newest.items() if state == "APPROVED")
