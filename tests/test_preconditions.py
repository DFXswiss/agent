from __future__ import annotations

import pytest

from agent_cli import allow, main, model_slots, preconditions
from agent_cli.preconditions import (
    ACTIONS,
    CHECKS,
    CHECK_STATUSES,
    ISSUE,
    ORDER,
    OVERRIDABLE,
    PHASE_ACTION,
    PHASES,
    REGISTRY_KEYS,
    REMEDY_ACTIONS,
    ROUTE_ALIASES,
    SKIPPABLE,
    WAITING_PHASES,
    WHEN_NAMES,
    action_requiring,
)

pytestmark = pytest.mark.no_pg


def test_registry_keys_do_not_collide_with_workflow_keys() -> None:
    workflow_keys = {
        key for keys in allow.CHECKLIST_KEYS.values() for key in keys
    }
    legacy_keys = set(model_slots.LEGACY_KEY_ALIASES)
    legacy_keys.update(model_slots.LEGACY_KEY_ALIASES.values())

    assert REGISTRY_KEYS.isdisjoint(workflow_keys)
    assert REGISTRY_KEYS.isdisjoint(legacy_keys)
    assert REGISTRY_KEYS == {
        "order_authorized",
        "spec_available",
        "checkout_owned",
        "duplicates_checked",
        "no_live_lane",
        "round_budget",
        "head_signed_clean",
        "pr_mergeable",
        "conflict_mandate",
        "review_2",
        "one_review_permitted",
    }


def test_every_requirement_names_a_declared_check() -> None:
    for action in ACTIONS.values():
        for requirement in action.requires:
            assert requirement.check in CHECKS
            assert set(requirement.kinds) <= {ORDER, ISSUE}


def test_every_route_target_is_a_phase_or_alias() -> None:
    targets = set(PHASES) | set(ROUTE_ALIASES)
    for check in CHECKS.values():
        for outcome in (check.on_pending, check.on_no):
            if outcome.startswith("route:"):
                assert outcome.removeprefix("route:") in targets


def test_every_when_is_declared() -> None:
    for action in ACTIONS.values():
        for requirement in action.requires:
            if requirement.when in WHEN_NAMES:
                continue
            assert requirement.when.startswith("phase:")
            assert requirement.when.removeprefix("phase:") in PHASES


def test_every_phase_has_an_action_or_is_waiting() -> None:
    action_phases = set(PHASE_ACTION)

    assert action_phases | set(WAITING_PHASES) == set(PHASES)
    assert action_phases.isdisjoint(WAITING_PHASES)
    assert len(PHASES) == len(set(PHASES))
    assert len(PHASE_ACTION) == sum(len(action.phases) for action in ACTIONS.values())


def test_overridable_checks() -> None:
    assert OVERRIDABLE == {"duplicates_checked", "review_2"}


def test_only_duplicates_checked_is_skippable() -> None:
    assert SKIPPABLE == {"duplicates_checked"}


def test_action_requiring() -> None:
    assert action_requiring("duplicates_checked", ORDER) == "open_draft"
    assert action_requiring("round_budget", ISSUE) == "implement"
    assert action_requiring("round_budget", ORDER) == "implement"
    assert action_requiring("duplicates_checked", ISSUE) is None
    assert action_requiring("no_such_check", ORDER) is None
    assert "action_requiring" in preconditions.__all__


def test_every_skippable_check_has_an_order_action() -> None:
    for key in SKIPPABLE:
        assert action_requiring(key, ORDER) in ACTIONS


def test_issue_tasks_share_exactly_three_checks() -> None:
    shared = {
        requirement.check
        for action in ACTIONS.values()
        if action.name != "close_draft"
        for requirement in action.requires
        if ISSUE in requirement.kinds
    }

    assert shared == {"no_live_lane", "round_budget", "pr_mergeable"}


def test_resolver_is_named_exactly_for_resolve_checks() -> None:
    for check in CHECKS.values():
        assert (check.resolver != "") == (check.on_pending == "resolve")
    assert all(CHECKS[key].on_pending == "resolve" for key in REGISTRY_KEYS)


def test_every_action_for_orders_starts_with_order_active() -> None:
    for action in ACTIONS.values():
        if action.name != "close_draft":
            assert action.requires[0].check == "order_active"


def test_check_statuses_are_the_checklist_vocabulary() -> None:
    assert set(CHECK_STATUSES) == set(main.CHECKLIST_STATUSES)
    assert len(CHECK_STATUSES) == 5


def test_waived_actions_name_an_overridable_registry_check() -> None:
    waived = set()
    for action in ACTIONS.values():
        assert bool(action.waived_by) == bool(action.waived_route)
        if not action.waived_by:
            continue
        waived.add(action.name)
        assert action.waived_by in REGISTRY_KEYS
        assert action.waived_by in OVERRIDABLE
        assert action.waived_route in set(PHASES) | set(ROUTE_ALIASES)
    assert waived == {"second_review", "second_review_comment"}


def test_pending_route_is_declared_only_on_resolve_checks() -> None:
    special = set()
    for check in CHECKS.values():
        if check.pending_route:
            assert check.on_pending == "resolve"
            assert check.pending_route in set(PHASES) | set(ROUTE_ALIASES)
        if check.override_pending:
            assert check.overridable
        if check.pending_route or check.override_pending:
            special.add(check.key)
    assert special == {"review_2"}


def test_review_2_replaces_the_dimension_requirements() -> None:
    removed = {"review_2_quality", "review_2_logic", "second_comment_posted"}
    assert all(
        requirement.check not in removed
        for action in ACTIONS.values()
        for requirement in action.requires
    )
    assert [item.check for item in ACTIONS["ci"].requires] == [
        "order_active",
        "draft_open",
        "review_2",
    ]
    assert "review_2" in {
        item.check for item in ACTIONS["readiness"].requires
    }
    waiver = ACTIONS["waive_second_review"]
    assert waiver.phases == ()
    assert [item.check for item in waiver.requires] == [
        "order_active",
        "draft_open",
        "one_review_permitted",
    ]
    for key in ("review_2", "one_review_permitted"):
        requirements = [
            item
            for action in ACTIONS.values()
            for item in action.requires
            if item.check == key
        ]
        assert requirements
        assert all(item.kinds == (ORDER,) for item in requirements)
    assert action_requiring("review_2", ORDER) == "ci"
    assert action_requiring("one_review_permitted", ORDER) == "waive_second_review"


def test_pr_mergeable_declares_the_conflict_remedy() -> None:
    check = CHECKS["pr_mergeable"]
    assert check.remedy == "conflict_resolution"
    assert check.remedy_kinds == (ORDER,)
    assert check.on_no == "block"
    assert check.block_kind == "conflict"
    assert check.overridable is False
    assert [item.key for item in CHECKS.values() if item.remedy] == [
        "pr_mergeable"
    ]


def test_every_remedy_names_an_action_without_a_phase() -> None:
    for check in CHECKS.values():
        if not check.remedy:
            assert check.remedy_kinds == ()
            continue
        assert check.remedy_kinds
        assert set(check.remedy_kinds) <= {ORDER, ISSUE}
        action = ACTIONS[REMEDY_ACTIONS[check.remedy]]
        assert action.phases == ()


def test_resolve_conflicts_check_list() -> None:
    action = ACTIONS["resolve_conflicts"]
    assert [item.check for item in action.requires] == [
        "order_active",
        "order_authorized",
        "draft_open",
        "head_published",
        "conflict_mandate",
    ]
    assert all(item.kinds == (ORDER,) for item in action.requires)
    check = CHECKS["conflict_mandate"]
    assert check.fact == "registry"
    assert check.head_bound is True
    assert check.recheck is True
    assert check.on_pending == "resolve"
    assert check.overridable is False
    assert check.skippable is False
