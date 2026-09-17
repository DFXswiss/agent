"""Tests for health.py decision logic. Pure module: no Store, no runner, no filesystem."""

from __future__ import annotations

from agent_cli.health import (
    agent_config_problems,
    runner_config_problems,
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
    # Length is 3 so that, without the type guard, the set comparison would
    # be reached and raise on the unhashable element. The guard short-circuits
    # ahead of it and reports a problem instead, which is this module's
    # contract: unusable input is reported, never raised.
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


# ---------------------------------------------------------- runner_config_problems


def test_runner_config_problems_returns_empty_for_a_fully_configured_runner() -> None:
    runner_config = {
        "defaults": {"timeout_minutes": 60, "stall_minutes": 10},
        "clone_stall_minutes": 5,
        "skills": {"pr-review": {"timeout_minutes": 30}, "implement": {}},
    }
    assert runner_config_problems(runner_config) == []


def test_runner_config_problems_reports_a_missing_stall_minutes() -> None:
    # The per-skill check covers deny and timeout_minutes only, so without
    # this rule nothing catches a missing stall_minutes at all. A job that
    # is not overdue is then skipped every pass; an overdue one is still
    # killed, since the timeout watchdog no longer needs this budget.
    runner_config = {
        "defaults": {"timeout_minutes": 60},
        "clone_stall_minutes": 5,
        "skills": {"pr-review": {}},
    }
    problems = runner_config_problems(runner_config)
    assert len(problems) == 1
    assert "stall_minutes" in problems[0]


def test_runner_config_problems_reports_a_missing_clone_stall_minutes() -> None:
    runner_config = {
        "defaults": {"timeout_minutes": 60, "stall_minutes": 10},
        "skills": {},
    }
    problems = runner_config_problems(runner_config)
    assert len(problems) == 1
    assert "clone_stall_minutes" in problems[0]


def test_runner_config_problems_rejects_a_zero_budget() -> None:
    # Strictly positive here, where _budget accepts zero at run time. The
    # original is stricter in its check than in its runtime for these
    # fields and that asymmetry is deliberate.
    runner_config = {
        "defaults": {"timeout_minutes": 0, "stall_minutes": 10},
        "clone_stall_minutes": 5,
        "skills": {},
    }
    problems = runner_config_problems(runner_config)
    assert any("defaults.timeout_minutes" in p for p in problems)


def test_runner_config_problems_rejects_a_bool_budget() -> None:
    # bool is a subclass of int, so True must not read as a positive number.
    runner_config = {
        "defaults": {"timeout_minutes": True, "stall_minutes": 10},
        "clone_stall_minutes": 5,
        "skills": {},
    }
    assert any(
        "defaults.timeout_minutes" in p for p in runner_config_problems(runner_config)
    )


def test_runner_config_problems_reports_a_float_budget() -> None:
    # The original's jq check admits a float, but _budget returns a budget
    # only for an int, so a float would pass health and then skip every job
    # at run time. Health must not certify a config the supervisor cannot
    # run, so the laxer half of the original is not carried over.
    runner_config = {
        "defaults": {"timeout_minutes": 1.5, "stall_minutes": 10},
        "clone_stall_minutes": 5,
        "skills": {},
    }
    problems = runner_config_problems(runner_config)
    assert len(problems) == 1
    assert "defaults.timeout_minutes" in problems[0]


def test_runner_config_problems_reports_a_float_skill_timeout() -> None:
    # Same rule on the per-skill path, where the effective value is the
    # skill's own rather than the inherited default.
    runner_config = {
        "defaults": {"timeout_minutes": 60, "stall_minutes": 10},
        "clone_stall_minutes": 5,
        "skills": {"pr-review": {"timeout_minutes": 1.5}},
    }
    problems = runner_config_problems(runner_config)
    assert len(problems) == 1
    assert "pr-review" in problems[0]


def test_runner_config_problems_reports_a_skill_whose_effective_timeout_is_unusable() -> None:
    # The skill's own value shadows a healthy default, so the effective
    # timeout is the broken one.
    runner_config = {
        "defaults": {"timeout_minutes": 60, "stall_minutes": 10},
        "clone_stall_minutes": 5,
        "skills": {"pr-review": {"timeout_minutes": "soon"}},
    }
    problems = runner_config_problems(runner_config)
    assert len(problems) == 1
    assert "pr-review" in problems[0]


def test_runner_config_problems_lets_a_skill_inherit_a_healthy_default_timeout() -> None:
    runner_config = {
        "defaults": {"timeout_minutes": 60, "stall_minutes": 10},
        "clone_stall_minutes": 5,
        "skills": {"pr-review": {}},
    }
    assert runner_config_problems(runner_config) == []


def test_runner_config_problems_reports_a_skills_value_that_is_not_a_mapping() -> None:
    runner_config = {
        "defaults": {"timeout_minutes": 60, "stall_minutes": 10},
        "clone_stall_minutes": 5,
        "skills": ["pr-review"],
    }
    problems = runner_config_problems(runner_config)
    assert any("skills" in p for p in problems)


def test_runner_config_problems_reports_a_problem_when_the_config_is_not_a_dict() -> None:
    assert runner_config_problems("not-a-dict") == ["runner config is not a dict"]


def test_unresolved_skills_names_the_catalogue_when_it_cannot_be_read() -> None:
    # The sentinel is load-bearing: an empty list reads as healthy, so pin
    # the actual string rather than merely asserting the list is non-empty.
    assert unresolved_skills({"skills": "not-a-list"}, {}) == [
        "the catalogue cannot be read"
    ]


def test_unresolved_skills_reads_a_catalogue_of_only_unusable_entries_as_healthy() -> None:
    # Every entry is skipped, so the result is [] — the module's own
    # "healthy" answer. That is the behaviour, and the name says so. It is
    # pinned deliberately: unlike an unreadable catalogue, which returns a
    # sentinel, a readable catalogue of unusable entries is not reported.
    # If that should ever change, this test is where it is decided.
    catalog = {"skills": ["not-a-dict", {"no_id": 1}, {"id": ""}]}
    assert unresolved_skills(catalog, {"defaults": {"deny": [], "timeout_minutes": 5}}) == []


def test_unresolved_skills_accepts_deny_from_the_skill_and_timeout_from_defaults() -> None:
    # Mixed resolution: each setting is defended on its own path, so taking
    # one from each level must resolve cleanly.
    catalog = {"skills": [{"id": "pr-review"}]}
    runner_config = {
        "skills": {"pr-review": {"deny": ["Bash"]}},
        "defaults": {"timeout_minutes": 30},
    }
    assert unresolved_skills(catalog, runner_config) == []


def test_unresolved_skills_accepts_timeout_from_the_skill_and_deny_from_defaults() -> None:
    catalog = {"skills": [{"id": "pr-review"}]}
    runner_config = {
        "skills": {"pr-review": {"timeout_minutes": 30}},
        "defaults": {"deny": ["Bash"]},
    }
    assert unresolved_skills(catalog, runner_config) == []
