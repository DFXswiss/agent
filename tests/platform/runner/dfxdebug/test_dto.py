"""U-RUN-dfxdebug-dto (ADR-102 section 4): the formal checks of the query DTO - the contract's form plus the bounds of
the WHERE tree and the value of a leaf by its operator - and the audit's filter summary, which names columns and
operators and never a value."""

from __future__ import annotations

from typing import Any

import pytest

from agent_runner.dfxdebug import dto
from agent_runner.operations.extensions import CheckFailed
from tests.platform.runner.operations.test_checks import VALID, refused

NONE = object()


def leaf(column: str = "id", op: str = "=", value: object = 1, **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"kind": "leaf", "column": column, "op": op, **extra}
    if value is not NONE:
        node["value"] = value
    return node


def nested(depth: int) -> dict[str, Any]:
    node: dict[str, Any] = leaf()
    for _ in range(depth - 1):
        node = {"kind": "not", "child": node}
    return node


@pytest.mark.parametrize(
    "where",
    [
        leaf(),
        leaf(op="IS NOT NULL", value=NONE),
        nested(dto.MAX_DEPTH),
        {"kind": "and", "children": [leaf()] * dto.MAX_CHILDREN},
        leaf(op="NOT IN", value=["x" * dto.MAX_TEXT] * dto.MAX_IN),
        leaf(value=False),
        leaf(value=-1.5),
        {"kind": "or", "children": [{"kind": "and", "children": [leaf()] * 5} for _ in range(5)]},
    ],
)
def test_valid_where_trees(where: dict[str, Any]) -> None:
    assert dto.where(where)


@pytest.mark.parametrize(
    "where",
    [
        nested(dto.MAX_DEPTH + 1),
        {"kind": "and", "children": [leaf()] * (dto.MAX_CHILDREN + 1)},
        {"kind": "and", "children": []},
        {"kind": "and", "children": "x"},
        {"kind": "and", "children": [leaf()], "child": leaf()},
        {"kind": "not", "child": leaf(), "children": []},
        {"kind": "xor", "children": [leaf()]},
        {"kind": "leaf", "column": "id"},
        leaf(extra=1),
        leaf(column="a b"),
        leaf(op="==", value=1),
        {"kind": "leaf", "column": "id", "op": "="},
        leaf(value=None),
        leaf(value=[1]),
        leaf(value={"a": 1}),
        leaf(value="x" * (dto.MAX_TEXT + 1)),
        leaf(op="IN", value=1),
        leaf(op="IN", value=[1] * (dto.MAX_IN + 1)),
        leaf(op="IN", value=[[1]]),
        leaf(op="IS NULL", value=1),
        "leaf",
        {"column": "id"},
        # 51 leaves within depth 4: more than MAX_LEAVES
        {
            "kind": "and",
            "children": [
                {"kind": "or", "children": [{"kind": "and", "children": [leaf()] * 5} for _ in range(5)]}
                for _ in range(2)
            ]
            + [{"kind": "and", "children": [leaf()]}],
        },
    ],
)
def test_invalid_where_trees(where: object) -> None:
    assert not dto.where(where)


def test_a_full_tree_of_the_largest_shape_is_refused() -> None:
    node: dict[str, Any] = leaf()
    for _ in range(dto.MAX_DEPTH - 1):
        node = {"kind": "and", "children": [node] * dto.MAX_CHILDREN}
    assert not dto.where(node)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("table", "Buy_Crypto"),
        ("table", "x" * 65),
        ("select", []),
        ("select", [{"kind": "column", "column": "id"}] * 101),
        ("select", [{"kind": "column"}]),
        ("select", [{"kind": "column", "column": "id", "extra": 1}]),
        ("select", [{"kind": "star", "column": "id"}]),
        ("select", [{"kind": "column", "column": "id", "as": "a-b"}]),
        ("select", [{"kind": "aggregate", "column": "id", "aggregate": "median"}]),
        ("select", [{"kind": "jsonb", "column": "message", "jsonbPath": "a..b"}]),
        ("select", [{"kind": "jsonb", "column": "message", "jsonbPath": 5}]),
        ("select", ["id"]),
        ("groupBy", ["a"] * 21),
        ("groupBy", ["a b"]),
        ("orderBy", [{"column": "id", "direction": "asc"}]),
        ("orderBy", [{"direction": "ASC"}]),
        ("orderBy", [{"column": "id", "nulls": "first"}]),
        ("orderBy", ["id"]),
        ("limit", 0),
        ("limit", 10001),
        ("limit", True),
        ("offset", -1),
        ("offset", 1000001),
        ("poll", {"blockchain": "Ethereum", "address": "x", "fromBlock": 1, "toBlock": 2}),
    ],
)
def test_the_runner_refuses_a_dto_outside_the_contract(name: str, value: object) -> None:
    failed = refused("dfx.debug", {**VALID["dfx.debug"], name: value})
    assert isinstance(failed, CheckFailed)
    assert (failed.code, failed.pointers) == ("params_invalid", [f"/params/{name}"])


def test_the_filter_summary_names_columns_and_operators_never_values() -> None:
    where = {
        "kind": "and",
        "children": [
            leaf("mail", "=", value="someone@example.com"),
            {"kind": "not", "child": leaf("amlCheck", "IN", value=["Pass", "Fail"])},
            {"kind": "or", "children": [leaf("created", ">=", value="2026-11-01"), leaf("status", "IS NULL", NONE)]},
        ],
    }
    assert dto.where(where)
    assert dto.filter_summary(where) == "and(mail =, not(amlCheck IN), or(created >=, status IS NULL))"
    assert dto.filter_summary(None) == ""
    long = {"kind": "or", "children": [leaf("c" * 60, "NOT IN", value=[1]) for _ in range(5)]}
    text = dto.filter_summary(long)
    assert len(text) == dto.MAX_SUMMARY
    assert text.endswith("...")
