"""The query DTO of `dfx.debug` (ADR-102 section 4; `contracts/ops/objects.json#/$defs/params_dfx_debug`).

The DTO is the one of the dfx-debug CLI and of the endpoint (`DebugQueryDto` of the DFX API): `table`, `select`,
`where`, `groupBy`, `orderBy`, `limit`, `offset`. The rules here are the formal checks of the runner
(`operations.checks`, step 1): the schema of the contract plus what a schema cannot state and the endpoint refuses
anyway - the bounds of the WHERE tree (depth `MAX_DEPTH`, `MAX_NODES` nodes, `MAX_LEAVES` leaves, `MAX_CHILDREN`
children of `and`/`or`) and the value of a leaf by its operator (`IN`/`NOT IN` a list of at most `MAX_IN` values,
`IS NULL`/`IS NOT NULL` none, every other operator one value). The walk is iterative, so no tree can exhaust the stack.
Which tables and columns exist is the endpoint's allowlist; the runner does not copy it.

`filter_summary(where)` is the audit's summary of a filter: the tree with its columns and operators and without a
single value (`and(amlCheck =, not(mail =))`), at most `MAX_SUMMARY` characters.
"""

from __future__ import annotations

import re
from typing import Any

MAX_DEPTH = 5
MAX_NODES = 200
MAX_LEAVES = 50
MAX_CHILDREN = 5
MAX_IN = 100
MAX_TEXT = 1024
MAX_SELECT = 100
MAX_LIST = 20
MAX_SUMMARY = 256
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
TABLE_RE = re.compile(r"[a-z_][a-z0-9_]{0,63}")
PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}([.][A-Za-z_][A-Za-z0-9_]{0,63}){0,7}")
LIST_OPS = frozenset({"IN", "NOT IN"})
NULL_OPS = frozenset({"IS NULL", "IS NOT NULL"})
OPS = frozenset({"=", "!=", "<", "<=", ">", ">=", "LIKE", "ILIKE"}) | LIST_OPS | NULL_OPS
SELECT_KINDS = frozenset({"column", "jsonb", "aggregate"})
AGGREGATES = frozenset({"count", "sum", "min", "max", "avg"})


def identifier(value: object) -> bool:
    return isinstance(value, str) and IDENT_RE.fullmatch(value) is not None


def table(value: object) -> bool:
    return isinstance(value, str) and TABLE_RE.fullmatch(value) is not None


def _select_item(item: object) -> bool:
    if not isinstance(item, dict) or not {"kind", "column"} <= set(item) <= {
        "kind",
        "column",
        "as",
        "aggregate",
        "jsonbPath",
    }:
        return False
    path = item.get("jsonbPath", "a")
    return (
        item["kind"] in SELECT_KINDS
        and identifier(item["column"])
        and ("as" not in item or identifier(item["as"]))
        and ("aggregate" not in item or item["aggregate"] in AGGREGATES)
        and isinstance(path, str)
        and len(path) <= 256
        and PATH_RE.fullmatch(path) is not None
    )


def select(value: object) -> bool:
    return isinstance(value, list) and 1 <= len(value) <= MAX_SELECT and all(_select_item(item) for item in value)


def group_by(value: object) -> bool:
    return isinstance(value, list) and len(value) <= MAX_LIST and all(identifier(item) for item in value)


def order_by(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= MAX_LIST
        and all(
            isinstance(item, dict)
            and "column" in item
            and set(item) <= {"column", "direction"}
            and identifier(item["column"])
            and item.get("direction", "ASC") in ("ASC", "DESC")
            for item in value
        )
    )


def count(low: int, high: int) -> Any:
    return lambda value: type(value) is int and low <= value <= high


def _scalar(value: object) -> bool:
    if isinstance(value, str):
        return len(value) <= MAX_TEXT
    return isinstance(value, (bool, int, float))


def _leaf(node: dict[str, Any]) -> bool:
    if not {"kind", "column", "op"} <= set(node) <= {"kind", "column", "op", "value"}:
        return False
    op = node["op"]
    if not identifier(node["column"]) or op not in OPS:
        return False
    if op in NULL_OPS:
        return "value" not in node
    value = node.get("value")
    if op in LIST_OPS:
        return isinstance(value, list) and len(value) <= MAX_IN and all(_scalar(item) for item in value)
    return "value" in node and _scalar(value)


def _children(node: dict[str, Any]) -> list[Any] | None:
    """The children of a valid inner node, None for anything else."""
    kind = node["kind"]
    if kind in ("and", "or") and set(node) == {"kind", "children"}:
        children = node["children"]
        if isinstance(children, list) and 1 <= len(children) <= MAX_CHILDREN:
            return children
        return None
    if kind == "not" and set(node) == {"kind", "child"}:
        return [node["child"]]
    return None


def where(value: object) -> bool:
    """A WHERE tree within its bounds (module docstring), walked without recursion."""
    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = leaves = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES or not isinstance(node, dict) or "kind" not in node:
            return False
        if node["kind"] == "leaf":
            leaves += 1
            if leaves > MAX_LEAVES or not _leaf(node):
                return False
            continue
        children = _children(node)
        if children is None:
            return False
        stack.extend((child, depth + 1) for child in children)
    return True


def _summary(node: dict[str, Any]) -> str:
    if node["kind"] == "leaf":
        return f"{node['column']} {node['op']}"
    if node["kind"] == "not":
        return f"not({_summary(node['child'])})"
    return f"{node['kind']}({', '.join(_summary(child) for child in node['children'])})"


def filter_summary(value: object) -> str:
    """The audit summary of a checked WHERE tree (`where`), empty without one (module docstring)."""
    if not isinstance(value, dict):
        return ""
    text = _summary(value)
    return text if len(text) <= MAX_SUMMARY else text[: MAX_SUMMARY - 3] + "..."
