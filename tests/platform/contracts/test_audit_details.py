"""The hub enforces exactly the audit detail schemas of `contracts/audit/` (02-data-model.md section 2.9; KOR-07).

`agent_hub.audit.detail_schemas` must equal the resolved contracts, and `agent_hub.audit.details.detail_violation`
must accept exactly the details the JSON Schema validator of the contract harness accepts: the valid and invalid
fixtures of every action and, per property, boundary values of each of its alternatives.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agent_hub.audit import details
from agent_hub.audit.actions import AUDIT_ACTIONS
from agent_hub.audit.detail_schemas import DETAIL_SCHEMAS
from agent_hub.audit.details import detail_violation
from agent_hub.outbox.bodies import AUDIT_DETAILS
from tests.platform.contracts.audit_details import render, resolved, valid_detail
from tests.platform.contracts.harness import CONTRACTS, Contracts, load_json


def test_the_hub_module_equals_the_contracts() -> None:
    assert resolved() == DETAIL_SCHEMAS
    assert set(DETAIL_SCHEMAS) == AUDIT_ACTIONS
    assert render().startswith('"""The detail schemas of `contracts/audit/<action>.json`')


def _samples(rule: dict[str, Any], fitting: object) -> Iterator[object]:
    """Values at and beyond the bounds of one leaf alternative, and values of the other types."""
    yield from rule.get("enum", ())
    if rule["type"] == "string":
        low, high = rule.get("minLength", 0), rule.get("maxLength", 300)
        yield from ("a" * low, "a" * max(low - 1, 0), "a" * high, "a" * (high + 1), "A b", "a\n", "a:b", "x")
        if isinstance(fitting, str):
            yield from (fitting, fitting + "\n", fitting[:-1], fitting.upper())
    if rule["type"] == "integer":
        low, high = rule.get("minimum", -5), rule.get("maximum", 2**53)
        yield from (low, low - 1, high, high + 1, 0)
    yield from (None, True, False, 1.0, [], {}, "", 7)


def _candidates(action: str) -> Iterator[dict[str, Any]]:
    folder = CONTRACTS / "fixtures" / "audit" / action
    for path in sorted(folder.rglob("*.json")):
        detail = load_json(path)
        if isinstance(detail, dict):  # a non-object detail is refused by the generic form (`validate_row`)
            yield detail
    base = valid_detail(action)
    for name, alternatives in DETAIL_SCHEMAS[action]["properties"].items():
        for rule in alternatives:
            for value in _samples(rule, base.get(name)):
                yield {**base, name: value}
        yield {key: value for key, value in base.items() if key != name}
    yield {**base, "unlisted": 1}


@pytest.mark.parametrize("action", sorted(AUDIT_ACTIONS))
def test_the_checker_agrees_with_the_contract_schema(contracts: Contracts, action: str) -> None:
    for detail in _candidates(action):
        expected = contracts.errors(f"audit/{action}.json", detail) == []
        assert (detail_violation(action, detail) is None) is expected, (action, detail)


@pytest.mark.parametrize("action", sorted(AUDIT_DETAILS))
def test_the_outbox_models_agree_with_the_checker(action: str) -> None:
    """A runner's audit item is checked by the outbox models first; they must accept what `append_audit` accepts."""
    for detail in _candidates(action):
        try:
            AUDIT_DETAILS[action].model_validate(detail)
        except ValidationError:
            assert detail_violation(action, detail) is not None, (action, detail)
        else:
            assert detail_violation(action, detail) is None, (action, detail)


def test_the_violation_names_the_first_offending_property() -> None:
    assert detail_violation("invite.use", {}) == "invite_id"
    assert detail_violation("invite.use", {"invite_id": "A" * 21 + "B", "kind": "user"}) == "invite_id"
    assert detail_violation("invite.use", {"kind": "robot", "invite_id": "A" * 21 + "A"}) == "kind"
    assert detail_violation("invite.use", {"invite_id": "A" * 21 + "A", "kind": "user", "x": 1}) == "x"
    assert detail_violation("invite.use", {"invite_id": "A" * 21 + "A", "kind": "device"}) is None


def test_every_contract_fixture_folder_has_a_valid_basic_detail() -> None:
    for action in AUDIT_ACTIONS:
        assert Path(CONTRACTS / "fixtures" / "audit" / action / "valid" / "basic.json").is_file(), action


def test_patterns_anchor_like_ecma_262() -> None:
    """A final `$` matches only at the very end; a pattern without one is a plain search."""
    assert details._pattern("^a$").search("a\n") is None
    assert details._pattern("^a$").search("a") is not None
    assert details._pattern("^a").search("ab") is not None
    assert details._pattern("^a\\$").search("a$") is not None
