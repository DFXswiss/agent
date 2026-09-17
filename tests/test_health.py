"""Tests for health.py decision logic. Pure module: no Store, no runner, no filesystem."""

from __future__ import annotations

from agent_cli.health import (
    agent_config_problems,
    unresolved_skills,
)


# ---------------------------------------------------------------- unresolved_skills


def test_unresolved_skills_returns_an_empty_list_when_a_skill_is_resolved_entirely_by_its_own_entry() -> None:
    catalog = {"skills": [{"id": "spine"}]}
    runner_config = {
        "skills": {"spine": {"deny": [], "timeout_minutes": 30}},
        "defaults": {"deny": None, "timeout_minutes": None},
    }
    assert unresolved_skills(catalog, runner_config) == []


def test_unresolved_skills_returns_an_empty_list_when_a_skill_is_resolved_only_by_defaults() -> None:
    # Load-bearing: a config carrying only defaults is the ordinary case, and
    # an earlier version flagged every skill in one. The runner config omits
    # the skills key entirely, which is the shape that exposed that bug — an
    # empty skills entry would leave the sub-structure present and hide it.
    catalog = {"skills": [{"id": "spine"}]}
    runner_config = {"defaults": {"deny": [], "timeout_minutes": 30}}
    assert unresolved_skills(catalog, runner_config) == []


def test_unresolved_skills_returns_an_empty_list_when_a_skill_is_resolved_only_by_its_own_entry_and_defaults_are_absent() -> None:
    # Mirror of the defaults-only case: each resolution path must be defended on its own.
    catalog = {"skills": [{"id": "spine"}]}
    runner_config = {
        "skills": {"spine": {"deny": [], "timeout_minutes": 30}},
    }
    assert unresolved_skills(catalog, runner_config) == []


def test_unresolved_skills_reports_both_missing_settings_when_neither_source_resolves_a_skill() -> None:
    catalog = {"skills": [{"id": "spine"}]}
    runner_config = {"skills": {}, "defaults": {}}
    result = unresolved_skills(catalog, runner_config)
    assert len(result) == 2
    assert any("deny" in problem for problem in result)
    assert any("timeout_minutes" in problem for problem in result)
    assert all("spine" in problem for problem in result)


def test_unresolved_skills_treats_a_setting_present_as_none_on_the_skill_entry_as_missing() -> None:
    catalog = {"skills": [{"id": "spine"}]}
    runner_config = {
        "skills": {"spine": {"deny": None, "timeout_minutes": None}},
        "defaults": {},
    }
    result = unresolved_skills(catalog, runner_config)
    assert len(result) == 2
    assert any("deny" in problem for problem in result)
    assert any("timeout_minutes" in problem for problem in result)
    assert all("spine" in problem for problem in result)


def test_unresolved_skills_treats_a_setting_present_as_none_in_defaults_as_missing() -> None:
    catalog = {"skills": [{"id": "spine"}]}
    runner_config = {
        "skills": {},
        "defaults": {"deny": None, "timeout_minutes": None},
    }
    result = unresolved_skills(catalog, runner_config)
    assert len(result) == 2
    assert any("deny" in problem for problem in result)
    assert any("timeout_minutes" in problem for problem in result)
    assert all("spine" in problem for problem in result)


def test_unresolved_skills_reports_only_the_missing_setting() -> None:
    catalog = {"skills": [{"id": "spine"}]}
    runner_config = {
        "skills": {"spine": {"deny": []}},
        "defaults": {},
    }
    result = unresolved_skills(catalog, runner_config)
    assert len(result) == 1
    assert "timeout_minutes" in result[0]
    assert "spine" in result[0]


def test_unresolved_skills_returns_a_non_empty_list_when_the_catalogue_is_not_a_dict() -> None:
    # Empty would read as healthy, which is the opposite of the truth.
    result = unresolved_skills("not-a-dict", {})
    assert result != []


def test_unresolved_skills_returns_a_non_empty_list_when_catalogue_skills_is_not_a_list() -> None:
    result = unresolved_skills({"skills": "not-a-list"}, {})
    assert result != []


def test_unresolved_skills_skips_catalogue_entries_that_are_not_usable_skill_ids() -> None:
    catalog = {
        "skills": [
            "not-a-dict",
            {"name": "no-id"},
            {"id": ""},
            {"id": 7},
            {"id": "spine"},
        ]
    }
    runner_config = {"skills": {}, "defaults": {}}
    result = unresolved_skills(catalog, runner_config)
    assert len(result) == 2
    assert all("spine" in problem for problem in result)


def test_unresolved_skills_reports_problems_in_catalogue_order() -> None:
    catalog = {
        "skills": [
            {"id": "b"},
            {"id": "a"},
            {"id": "c"},
        ]
    }
    runner_config = {"skills": {}, "defaults": {}}
    result = unresolved_skills(catalog, runner_config)
    assert len(result) == 6
    assert "skill b" in result[0]
    assert "skill b" in result[1]
    assert "skill a" in result[2]
    assert "skill a" in result[3]
    assert "skill c" in result[4]
    assert "skill c" in result[5]


# ---------------------------------------------------------------- agent_config_problems


def test_agent_config_problems_returns_an_empty_list_for_a_correct_config() -> None:
    agent_config = {
        "cli": "agent",
        "session_kind": "runner",
        "skills": ["spine", "review-loop", "pr-review"],
    }
    assert agent_config_problems(agent_config) == []


def test_agent_config_problems_returns_an_empty_list_when_the_three_skills_are_in_a_different_order() -> None:
    # The check pins the set, not the sequence.
    agent_config = {
        "cli": "agent",
        "session_kind": "runner",
        "skills": ["pr-review", "spine", "review-loop"],
    }
    assert agent_config_problems(agent_config) == []


def test_agent_config_problems_reports_one_problem_when_a_skill_is_duplicated() -> None:
    agent_config = {
        "cli": "agent",
        "session_kind": "runner",
        "skills": ["spine", "spine", "pr-review"],
    }
    result = agent_config_problems(agent_config)
    assert len(result) == 1


def test_agent_config_problems_reports_one_problem_when_a_skill_is_missing() -> None:
    agent_config = {
        "cli": "agent",
        "session_kind": "runner",
        "skills": ["spine", "review-loop"],
    }
    result = agent_config_problems(agent_config)
    assert len(result) == 1


def test_agent_config_problems_reports_one_problem_when_an_extra_skill_is_present() -> None:
    agent_config = {
        "cli": "agent",
        "session_kind": "runner",
        "skills": ["spine", "review-loop", "pr-review", "something-else"],
    }
    result = agent_config_problems(agent_config)
    assert len(result) == 1


def test_agent_config_problems_reports_one_problem_naming_cli_when_cli_is_wrong() -> None:
    agent_config = {
        "cli": "codex",
        "session_kind": "runner",
        "skills": ["spine", "review-loop", "pr-review"],
    }
    result = agent_config_problems(agent_config)
    assert len(result) == 1
    assert "cli" in result[0]


def test_agent_config_problems_reports_one_problem_naming_session_kind_when_session_kind_is_wrong() -> None:
    agent_config = {
        "cli": "agent",
        "session_kind": "worker",
        "skills": ["spine", "review-loop", "pr-review"],
    }
    result = agent_config_problems(agent_config)
    assert len(result) == 1
    assert "session_kind" in result[0]


def test_agent_config_problems_returns_exactly_one_problem_when_agent_config_is_not_a_dict() -> None:
    result = agent_config_problems("not-a-dict")
    assert len(result) == 1


def test_agent_config_problems_reports_a_problem_instead_of_raising_on_an_unhashable_skill() -> None:
    # The list is the right length, so the set comparison is reached. An
    # unhashable element there would make set() raise, and this module's
    # contract is that unusable input is reported, never raised.
    agent_config = {
        "cli": "agent",
        "session_kind": "runner",
        "skills": ["spine", "review-loop", {"id": "pr-review"}],
    }
    result = agent_config_problems(agent_config)
    assert len(result) == 1
    assert "skills" in result[0]


def test_agent_config_problems_reports_a_problem_for_a_non_string_but_hashable_skill() -> None:
    # A hashable non-string would not raise, so this pins the same rule on
    # the path where only the type check can catch it.
    agent_config = {
        "cli": "agent",
        "session_kind": "runner",
        "skills": ["spine", "review-loop", 7],
    }
    result = agent_config_problems(agent_config)
    assert len(result) == 1
    assert "skills" in result[0]
