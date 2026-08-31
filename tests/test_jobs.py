from __future__ import annotations

import pytest

from agent_cli.jobs import STATES, Verdict, admits, job_id, transition_allowed
from agent_cli.store import OWNED_TABLES

pytestmark = pytest.mark.no_pg


def _policy(**over: object) -> dict:
    base: dict = {
        "actors_allow": ["davidleomay"],
        "repos_allow": ["owner/name"],
        "job_types_allow": ["pr-review"],
    }
    base.update(over)
    return base


def test_job_is_an_owned_table() -> None:
    # Without this the store refuses every write and the runner has nowhere to
    # keep a job — silently, because the refusal happens at the first insert.
    assert "job" in OWNED_TABLES


def test_the_same_request_keeps_the_same_identity() -> None:
    # A retried mention must land on the job it retries, not beside it.
    first = job_id("DFXswiss/backend", "5178", "pr-review")
    again = job_id("dfxswiss/BACKEND", "5178", "pr-review")
    assert first == again
    assert first.startswith("dfxswiss_backend__5178__pr_review__")


@pytest.mark.parametrize(
    "left,right",
    [
        # The readable slug alone is not injective: runs of punctuation collapse
        # to a single _ and the edges are trimmed. Without the digest these pairs
        # would meet, and the queue would lose one of the two jobs.
        (("a/b", "1", "x"), ("a/b", "_1", "x")),
        (("a/b", "1_2", "x"), ("a/b", "1__2", "x")),
        (("a/b", "_1", "x"), ("a_b", "1", "x")),
        (("a/b_c", "1", "x"), ("a/b__c", "1", "x")),
    ],
)
def test_two_requests_that_slug_alike_still_differ(
    left: tuple[str, str, str], right: tuple[str, str, str]
) -> None:
    assert job_id(*left) != job_id(*right)


def test_different_requests_keep_different_identities() -> None:
    base = job_id("owner/name", "1", "pr-review")
    assert base != job_id("owner/name", "2", "pr-review")
    assert base != job_id("owner/other", "1", "pr-review")
    assert base != job_id("owner/name", "1", "pr-ready")


@pytest.mark.parametrize("bad", [("", "1", "t"), ("r", "", "t"), ("r", "1", ""), ("  ", "1", "t")])
def test_an_incomplete_request_has_no_identity(bad: tuple[str, str, str]) -> None:
    with pytest.raises(ValueError):
        job_id(*bad)


def test_the_lifecycle_moves_only_forward() -> None:
    assert transition_allowed("queued", "running")
    assert transition_allowed("running", "done")
    assert transition_allowed("running", "failed")
    # A retry re-queues; that is the one way back.
    assert transition_allowed("failed", "queued")
    assert transition_allowed("done", "queued")
    # And these would let a job skip the work itself.
    assert not transition_allowed("queued", "done")
    assert not transition_allowed("queued", "failed")
    assert not transition_allowed("running", "queued")
    for state in STATES:
        assert not transition_allowed(state, "nonsense")


def test_a_named_actor_repo_and_job_type_are_admitted() -> None:
    v = admits(_policy(), actor="davidleomay", repo="owner/name", job_type="pr-review", private=False)
    assert v == Verdict(True, "admitted")


def test_admission_ignores_case() -> None:
    v = admits(_policy(), actor="DavidLeoMay", repo="Owner/Name", job_type="PR-Review", private=False)
    assert v.admitted, v.reason


@pytest.mark.parametrize(
    "field",
    ["actors_allow", "repos_allow", "job_types_allow"],
)
def test_an_empty_allow_list_admits_nothing(field: str) -> None:
    # Fail-closed. A policy that loses a list must not start answering to
    # everyone; that is the failure worth being loud about.
    v = admits(_policy(**{field: []}), actor="davidleomay", repo="owner/name", job_type="pr-review", private=False)
    assert not v.admitted
    assert "not allowed" in v.reason


@pytest.mark.parametrize(
    "field",
    ["actors_allow", "repos_allow", "job_types_allow"],
)
def test_a_missing_allow_list_admits_nothing(field: str) -> None:
    policy = _policy()
    del policy[field]
    v = admits(policy, actor="davidleomay", repo="owner/name", job_type="pr-review", private=False)
    assert not v.admitted


@pytest.mark.parametrize("junk", [None, [], "everyone", {"actors_allow": "davidleomay"}])
def test_a_malformed_policy_admits_nothing(junk: object) -> None:
    v = admits(junk, actor="davidleomay", repo="owner/name", job_type="pr-review", private=False)
    assert not v.admitted


def test_a_deny_beats_an_allow() -> None:
    for field, value in (("actors_deny", "davidleomay"), ("repos_deny", "owner/name")):
        v = admits(
            _policy(**{field: [value.upper()]}),
            actor="davidleomay",
            repo="owner/name",
            job_type="pr-review",
            private=False,
        )
        assert not v.admitted
        assert "denied" in v.reason


def test_a_disabled_instance_admits_nothing() -> None:
    v = admits(
        _policy(enabled=False), actor="davidleomay", repo="owner/name", job_type="pr-review"
    , private=False)
    assert not v.admitted
    assert "disabled" in v.reason


def test_a_private_repo_needs_naming_twice() -> None:
    # repos_allow alone is not enough: reaching into private code is stated once
    # more, so a broad allow-list cannot quietly include it.
    policy = _policy()
    v = admits(policy, actor="davidleomay", repo="owner/name", job_type="pr-review", private=True)
    assert not v.admitted
    assert "private_repos_allow" in v.reason

    policy["agent_identity"] = {"private_repos_allow": ["Owner/Name"]}
    v2 = admits(policy, actor="davidleomay", repo="owner/name", job_type="pr-review", private=True)
    assert v2.admitted
    assert "private_repos_allow" in v2.reason


def test_a_private_exception_does_not_bypass_the_other_lists() -> None:
    policy = _policy(repos_allow=[], agent_identity={"private_repos_allow": ["owner/name"]})
    v = admits(policy, actor="davidleomay", repo="owner/name", job_type="pr-review", private=True)
    assert not v.admitted


def test_private_has_no_default() -> None:
    # A caller who forgets `private` would otherwise get the public path for a
    # private repository — the one mistake this gate exists to make impossible.
    import inspect

    sig = inspect.signature(admits)
    assert sig.parameters["private"].default is inspect.Parameter.empty
