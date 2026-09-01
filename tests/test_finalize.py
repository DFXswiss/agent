from __future__ import annotations

import pytest

from agent_cli.finalize import done_kind, judge, transcript_has_marker


# ---------------------------------------------------------------- done_kind


def test_done_kind_returns_configured_value_as_is() -> None:
    # A configured value in DONE_KINDS is returned as-is.
    policy = {"skills": {"pr-review": {"done_kind": "pr-reviewed"}}}
    assert done_kind(policy, "pr-review") == "pr-reviewed"


def test_done_kind_degrades_unrecognised_setting_to_marker() -> None:
    # A configured non-empty string that is NOT a known kind returns "marker".
    policy = {"skills": {"pr-review": {"done_kind": "weird"}}}
    assert done_kind(policy, "pr-review") == "marker"


def test_done_kind_missing_or_empty_falls_through_to_derivation() -> None:
    # Missing / None / empty configuration falls through to derivation.
    assert done_kind(None, "pr-review") == "pr-reviewed"
    assert done_kind({}, "pr-review") == "pr-reviewed"
    assert done_kind({"skills": {}}, "pr-review") == "pr-reviewed"
    assert done_kind({"skills": {"pr-review": {}}}, "pr-review") == "pr-reviewed"
    assert done_kind({"skills": {"pr-review": {"done_kind": ""}}}, "pr-review") == "pr-reviewed"


def test_done_kind_derives_pr_reviewed_for_pr_review_and_pr_ready() -> None:
    # Derivation: pr-review -> "pr-reviewed", pr-ready -> "pr-reviewed".
    assert done_kind(None, "pr-review") == "pr-reviewed"
    assert done_kind(None, "pr-ready") == "pr-reviewed"


def test_done_kind_derives_pr_mergeable_for_merge_conflict() -> None:
    # Derivation: merge-conflict -> "pr-mergeable".
    assert done_kind(None, "merge-conflict") == "pr-mergeable"


def test_done_kind_derives_marker_for_other_job_types() -> None:
    # Anything else -> "marker".
    assert done_kind(None, "something-else") == "marker"


def test_done_kind_non_dict_policy_falls_through() -> None:
    # A policy that is not a dict falls through to derivation without raising.
    assert done_kind("not-a-dict", "pr-review") == "pr-reviewed"
    assert done_kind(["list"], "pr-review") == "pr-reviewed"


def test_done_kind_non_dict_skills_falls_through() -> None:
    # A skills that is not a dict falls through to derivation without raising.
    assert done_kind({"skills": "not-a-dict"}, "pr-review") == "pr-reviewed"
    assert done_kind({"skills": ["list"]}, "pr-review") == "pr-reviewed"


def test_done_kind_non_dict_per_skill_entry_falls_through() -> None:
    # A per-skill entry that is not a dict falls through to derivation without raising.
    assert done_kind({"skills": {"pr-review": "not-a-dict"}}, "pr-review") == "pr-reviewed"
    assert done_kind({"skills": {"pr-review": ["list"]}}, "pr-review") == "pr-reviewed"


# ---------------------------------------------------------------- transcript_has_marker


def test_transcript_has_marker_matches_line_start_done_with_ref() -> None:
    # A line starting DONE review PR 206 matches ref 206.
    transcript = "DONE review PR 206\nother line"
    assert transcript_has_marker(transcript, "206") is True
    assert transcript_has_marker(transcript, "#206") is True


def test_transcript_has_marker_ignores_done_not_at_line_start() -> None:
    # A DONE that is not at line start does NOT match.
    transcript = "- None of the lines `DONE review PR 206 …` — gate A did not pass"
    assert transcript_has_marker(transcript, "206") is False


def test_transcript_has_marker_requires_ref_from_field_three() -> None:
    # DONE 206 alone does not match ref 206: field 2 is the identifier.
    assert transcript_has_marker("DONE 206", "206") is False
    assert transcript_has_marker("DONE foo 206", "206") is True


def test_transcript_has_marker_is_textual_not_numeric() -> None:
    # Ref 1 is not matched by a field of 01 — the comparison is textual.
    assert transcript_has_marker("DONE x 01", "1") is False
    assert transcript_has_marker("DONE x 1", "1") is True


def test_transcript_has_marker_accepts_tab_after_done() -> None:
    # A tab after DONE works as well as a space.
    transcript = "DONE\treview PR 206"
    assert transcript_has_marker(transcript, "206") is True


def test_transcript_has_marker_strips_hash_from_ref() -> None:
    # A ref passed as #206 matches the same line as 206.
    transcript = "DONE review PR 206"
    assert transcript_has_marker(transcript, "#206") is True
    assert transcript_has_marker(transcript, "206") is True


@pytest.mark.parametrize("bad", [None, 123, [], {}])
def test_transcript_has_marker_non_string_transcript_returns_false(bad: object) -> None:
    # Non-string transcript returns False.
    assert transcript_has_marker(bad, "206") is False


@pytest.mark.parametrize("bad", [None, 123, [], {}])
def test_transcript_has_marker_non_string_ref_returns_false(bad: object) -> None:
    # Non-string ref returns False.
    assert transcript_has_marker("DONE x 206", bad) is False


def test_transcript_has_marker_empty_ref_returns_false() -> None:
    # An empty ref returns False.
    assert transcript_has_marker("DONE x 206", "") is False
    assert transcript_has_marker("DONE x 206", "#") is False


# ---------------------------------------------------------------- judge


def test_judge_non_success_passes_through_unchanged() -> None:
    # A non-success outcome passes through unchanged as (outcome, "n_a").
    assert judge(outcome="agent_failed", kind="marker", kinds=set(), contract_followed=True) == ("agent_failed", "n_a")
    assert judge(outcome="timeout", kind="pr-reviewed", kinds={"review"}, contract_followed=True) == ("timeout", "n_a")


def test_judge_kinds_none_returns_none() -> None:
    # kinds is None returns None — the check could not be answered.
    assert judge(outcome="success", kind="marker", kinds=None, contract_followed=True) is None


def test_judge_kinds_empty_downgrades_to_agent_failed() -> None:
    # kinds empty downgrades the outcome to "agent_failed".
    assert judge(outcome="success", kind="marker", kinds=set(), contract_followed=True) == ("agent_failed", "yes")


def test_judge_agent_failed_yes_is_reachable_for_pr_mergeable() -> None:
    # ("agent_failed", "yes") is reachable with kinds empty and kind="pr-mergeable".
    result = judge(outcome="success", kind="pr-mergeable", kinds=set(), mergeable=True, contract_followed=False)
    assert result == ("agent_failed", "yes")


def test_judge_agent_failed_yes_is_reachable_for_marker() -> None:
    # ("agent_failed", "yes") is reachable with kinds empty and kind="marker".
    result = judge(outcome="success", kind="marker", kinds=set(), contract_followed=True)
    assert result == ("agent_failed", "yes")


def test_judge_pr_reviewed_yes_when_review_in_kinds() -> None:
    # kind="pr-reviewed": "review" in kinds -> "yes".
    assert judge(outcome="success", kind="pr-reviewed", kinds={"review"}, contract_followed=False) == ("success", "yes")


def test_judge_pr_reviewed_no_when_review_not_in_kinds() -> None:
    # kinds present but without a review -> "no".
    assert judge(outcome="success", kind="pr-reviewed", kinds={"comment"}, contract_followed=False) == ("success", "no")


def test_judge_pr_mergeable_yes_true() -> None:
    # kind="pr-mergeable": mergeable True -> "yes".
    assert judge(outcome="success", kind="pr-mergeable", kinds={"comment"}, mergeable=True, contract_followed=False) == ("success", "yes")


def test_judge_pr_mergeable_no_false() -> None:
    # kind="pr-mergeable": mergeable False -> "no".
    assert judge(outcome="success", kind="pr-mergeable", kinds={"comment"}, mergeable=False, contract_followed=False) == ("success", "no")


def test_judge_pr_mergeable_unknown_none() -> None:
    # kind="pr-mergeable": mergeable None -> "unknown".
    assert judge(outcome="success", kind="pr-mergeable", kinds={"comment"}, mergeable=None, contract_followed=False) == ("success", "unknown")


def test_judge_marker_follows_contract_followed() -> None:
    # kind="marker": follows contract_followed.
    assert judge(outcome="success", kind="marker", kinds={"comment"}, contract_followed=True) == ("success", "yes")
    assert judge(outcome="success", kind="marker", kinds={"comment"}, contract_followed=False) == ("success", "no")


def test_judge_unrecognised_kind_yields_unknown() -> None:
    # An unrecognised kind yields "unknown".
    assert judge(outcome="success", kind="weird", kinds={"comment"}, contract_followed=True) == ("success", "unknown")
