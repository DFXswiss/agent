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


def test_only_duplicates_checked_is_overridable() -> None:
    assert OVERRIDABLE == {"duplicates_checked"}


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
