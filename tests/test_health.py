"""Tests for health.py decision logic. Pure module: no Store, no runner, no filesystem."""

from __future__ import annotations

from agent_cli.finalize import _budget
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


def test_runner_config_problems_reports_a_malformed_per_skill_stall_override() -> None:
    # _budget reads a skill's own stall_minutes and substitutes the default
    # only when the value is absent, so a present-but-broken override is
    # what the supervisor gets. Health has to reject it here or the job is
    # skipped at run time with nothing having complained.
    for bad in (1.5, "soon", True, -1):
        runner_config = {
            "defaults": {"timeout_minutes": 60, "stall_minutes": 10},
            "clone_stall_minutes": 5,
            "skills": {"pr-review": {"stall_minutes": bad}},
        }
        problems = runner_config_problems(runner_config)
        assert len(problems) == 1, f"{bad!r} was accepted"
        assert "stall_minutes" in problems[0]
        assert "pr-review" in problems[0]


def test_runner_config_problems_accepts_a_healthy_per_skill_stall_override() -> None:
    runner_config = {
        "defaults": {"timeout_minutes": 60, "stall_minutes": 10},
        "clone_stall_minutes": 5,
        "skills": {"pr-review": {"stall_minutes": 3}},
    }
    assert runner_config_problems(runner_config) == []


def test_runner_config_problems_lets_a_skill_omit_its_stall_override() -> None:
    # Absent is not broken: the skill inherits the default, which the rule
    # above has already required to be usable.
    runner_config = {
        "defaults": {"timeout_minutes": 60, "stall_minutes": 10},
        "clone_stall_minutes": 5,
        "skills": {"pr-review": {"timeout_minutes": 30}},
    }
    assert runner_config_problems(runner_config) == []


def test_runner_config_problems_reports_a_float_default_stall_minutes() -> None:
    runner_config = {
        "defaults": {"timeout_minutes": 60, "stall_minutes": 1.5},
        "clone_stall_minutes": 5,
        "skills": {},
    }
    problems = runner_config_problems(runner_config)
    assert len(problems) == 1
    assert "defaults.stall_minutes" in problems[0]


def test_runner_config_problems_reports_a_float_clone_stall_minutes() -> None:
    runner_config = {
        "defaults": {"timeout_minutes": 60, "stall_minutes": 10},
        "clone_stall_minutes": 1.5,
        "skills": {},
    }
    problems = runner_config_problems(runner_config)
    assert len(problems) == 1
    assert "clone_stall_minutes" in problems[0]


def test_any_config_this_calls_healthy_yields_a_usable_budget() -> None:
    # The property the per-skill rules exist for, and the one that was
    # actually false before a skill's stall override was checked. The
    # candidates deliberately mix healthy and broken configs: the assertion
    # is the implication, so dropping a rule makes some broken config report
    # healthy and fails here. Checking only hand-picked healthy configs
    # would pass no matter which rule was removed.
    handwritten = [
        # Budgets inherited wholly from defaults.
        {"defaults": {"timeout_minutes": 60, "stall_minutes": 10},
         "clone_stall_minutes": 5, "skills": {"pr-review": {}}},
        # Both overridden on the skill.
        {"defaults": {"timeout_minutes": 60, "stall_minutes": 10},
         "clone_stall_minutes": 5,
         "skills": {"pr-review": {"timeout_minutes": 30, "stall_minutes": 3}}},
        # One of each, in both directions.
        {"defaults": {"timeout_minutes": 60, "stall_minutes": 10},
         "clone_stall_minutes": 5, "skills": {"pr-review": {"timeout_minutes": 30}}},
        {"defaults": {"timeout_minutes": 60, "stall_minutes": 10},
         "clone_stall_minutes": 5, "skills": {"pr-review": {"stall_minutes": 3}}},
    ]
    # Every way a single field can be unusable, placed on the skill and on
    # defaults in turn. `None` is the only value here that is not always
    # broken, and only in one of the two places: on the skill it means
    # "inherit", and the default it inherits was already checked. Sitting in
    # defaults it has nothing to inherit from, so it is rejected like the
    # rest. Two of the twenty-four rows are therefore healthy, not four.
    generated: list[tuple[str, dict]] = []
    for bad in (1.5, "soon", True, -1, 0, None):
        for field in ("timeout_minutes", "stall_minutes"):
            generated.append((
                f"skill:{field}={bad!r}",
                {"defaults": {"timeout_minutes": 60, "stall_minutes": 10},
                 "clone_stall_minutes": 5, "skills": {"pr-review": {field: bad}}},
            ))
            defaults = {"timeout_minutes": 60, "stall_minutes": 10}
            defaults[field] = bad
            generated.append((
                f"defaults:{field}={bad!r}",
                {"defaults": defaults, "clone_stall_minutes": 5,
                 "skills": {"pr-review": {}}},
            ))

    def healthy_labels(rows: list[tuple[str, dict]]) -> set[str]:
        """Labels of the rows reporting no problems, asserting the implication.

        The assertion runs for every row counted here; a failure raises out
        of this helper rather than being folded into the returned set.
        """
        healthy = set()
        for label, runner_config in rows:
            if runner_config_problems(runner_config) != []:
                continue
            healthy.add(label)
            for field in ("timeout_minutes", "stall_minutes"):
                assert _budget(runner_config, "pr-review", field) is not None, (
                    f"health passed but {field} does not resolve: {runner_config}"
                )
        return healthy

    handwritten_healthy = healthy_labels(
        [(str(i), cfg) for i, cfg in enumerate(handwritten)]
    )
    generated_healthy = healthy_labels(generated)
    # Guard the guard, per group. A single total was satisfied by the
    # handwritten rows alone, which are all healthy by construction, so the
    # generated rows could have stopped contributing unnoticed.
    #
    # For the generated group, naming the rows is strictly stronger than
    # counting them: 2 of its 24 are expected healthy, so identity also
    # rejects one row going healthy while another stops. That is an argument
    # from the shape of the assertion; no mutation here has had to show it.
    # For the handwritten group it is not stronger at all — every row is
    # expected healthy, so the expected set is the whole label space and
    # set-equality says exactly what `== 4` would. It is written as a set
    # only to match its neighbour.
    assert handwritten_healthy == {"0", "1", "2", "3"}
    assert generated_healthy == {
        "skill:timeout_minutes=None",
        "skill:stall_minutes=None",
    }


def test_runner_config_problems_accepts_a_config_with_no_skills_table_at_all() -> None:
    # The skills table holds overrides. A config taking every budget from
    # defaults omits it, and that config runs: _budget resolves both budgets
    # from defaults and unresolved_skills calls the same shape healthy. A
    # missing table must therefore read the same as an empty one, or this
    # check reports a problem in a configuration the supervisor is running.
    runner_config = {
        "defaults": {"timeout_minutes": 60, "stall_minutes": 10},
        "clone_stall_minutes": 5,
    }
    assert runner_config_problems(runner_config) == []
    # And it agrees with both consumers on that same config.
    assert _budget(runner_config, "pr-review", "timeout_minutes") == 60
    assert _budget(runner_config, "pr-review", "stall_minutes") == 10


def test_runner_config_problems_reads_an_absent_skills_table_like_an_empty_one() -> None:
    absent = {
        "defaults": {"timeout_minutes": 60, "stall_minutes": 10},
        "clone_stall_minutes": 5,
    }
    empty = dict(absent, skills={})
    assert runner_config_problems(absent) == runner_config_problems(empty) == []


def test_runner_config_problems_still_reports_a_skills_table_set_to_a_non_mapping() -> None:
    # Tolerating the absent case must not tolerate a present, wrong-typed
    # one: that is a real mistake rather than an omission.
    for wrong in (["pr-review"], "pr-review", 7):
        runner_config = {
            "defaults": {"timeout_minutes": 60, "stall_minutes": 10},
            "clone_stall_minutes": 5,
            "skills": wrong,
        }
        problems = runner_config_problems(runner_config)
        assert len(problems) == 1, f"{wrong!r} was accepted"
        assert "skills" in problems[0]
