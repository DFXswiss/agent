"""U-HUB-profiles: the classes of the operation types against the contract, the built-in roles derived from them and
the rule "a child never has more than its parent" (ADR-090 sections 1 and 2)."""

from __future__ import annotations

import json

from agent_hub.operations.types import OPERATION_TYPES
from agent_hub.profiles import model
from agent_hub.profiles.model import BUILTIN, CLASSES, Profile, allows, from_json, invalid_operations, within
from agent_hub.profiles.store import intersect
from tests.platform.contracts.harness import CONTRACTS


def test_every_type_has_the_class_of_the_contract() -> None:
    data = json.loads((CONTRACTS / "ops" / "classes.json").read_text(encoding="utf-8"))
    assert data["types"] == dict(CLASSES)
    assert set(CLASSES) == OPERATION_TYPES
    assert set(CLASSES.values()) <= set(data["classes"]) == set(model.CLASS_NAMES)


def test_roles_are_derived_from_the_classes() -> None:
    assert set(BUILTIN) == {"read_only", "development", "security", "review", "merge"}
    read_only = BUILTIN["read_only"].operations
    assert "github.get_pull_request" in read_only
    assert "subagent.request" in read_only
    assert not {"github.commit", "github.push", "test.run", "task.set_title"} & read_only
    development = BUILTIN["development"].operations
    assert {"github.commit", "github.create_pull_request", "test.run"} <= development
    assert all(CLASSES[name] != model.MERGE for name in development)
    assert "test.run" in BUILTIN["security"].operations
    assert "github.commit" not in BUILTIN["security"].operations
    assert all(item.reads == model.REACH_ALL for item in BUILTIN.values())


def test_no_profile_allows_everything_and_a_profile_its_types() -> None:
    assert all(allows(None, name) for name in OPERATION_TYPES)
    assert allows(BUILTIN["read_only"], "github.list_issues")
    assert not allows(BUILTIN["read_only"], "github.comment")


def test_within() -> None:
    narrow = Profile("narrow", frozenset({"github.get_issue"}), model.REACH_TASK)
    assert within(narrow, None)
    assert within(narrow, BUILTIN["read_only"])
    assert not within(BUILTIN["development"], BUILTIN["read_only"])
    wide_reads = Profile("wide", frozenset({"github.get_issue"}), model.REACH_ALL)
    assert not within(wide_reads, narrow)
    assert within(narrow, narrow)


def test_snapshots_round_trip_and_intersect() -> None:
    profile = Profile("x", frozenset({"github.get_issue", "github.checkout"}), model.REACH_WORKGROUP)
    assert profile.as_json() == {
        "name": "x",
        "operations": ["github.checkout", "github.get_issue"],
        "reads": "workgroup",
    }
    assert from_json(profile.as_json()) == profile
    assert from_json(None) is None
    assert intersect([]) is None
    other = Profile("y", frozenset({"github.get_issue", "github.comment"}), model.REACH_TASK)
    assert intersect([profile, other]) == Profile("x", frozenset({"github.get_issue"}), model.REACH_TASK)


def test_invalid_operations() -> None:
    assert invalid_operations(["github.comment", "nope.type", "github.get_issue", "x.y"]) == [1, 3]
