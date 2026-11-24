"""The per-action detail schemas of `contracts/audit/` (02-data-model.md section 2.9), enforced on every audit row.

`detail_violation(action, detail)` returns the name of the first property that breaks the detail schema of the
action (a missing required property, a property the schema does not list, or a value none of its alternatives
accepts), None when the detail fits. The schemas come resolved from `agent_hub.audit.detail_schemas`; each property
has one or more leaf alternatives with the keywords `type` (`string`, `integer`, `boolean`, `null`), `enum`,
`pattern` (ECMA-262: a final `$` matches only at the very end), `minLength`, `maxLength`, `minimum`, `maximum`.
Integers are JSON integers only (a boolean is never an integer); `enum` compares by type and value.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import cache
from typing import Any

from agent_hub.audit.detail_schemas import DETAIL_SCHEMAS


@cache
def _pattern(pattern: str) -> re.Pattern[str]:
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.compile(pattern)


def _same(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def _string_fits(rule: Mapping[str, Any], value: str) -> bool:
    return (
        len(value) >= rule.get("minLength", 0)
        and ("maxLength" not in rule or len(value) <= rule["maxLength"])
        and ("pattern" not in rule or _pattern(rule["pattern"]).search(value) is not None)
    )


def _integer_fits(rule: Mapping[str, Any], value: int) -> bool:
    return ("minimum" not in rule or value >= rule["minimum"]) and ("maximum" not in rule or value <= rule["maximum"])


def _fits(rule: Mapping[str, Any], value: object) -> bool:
    kind = rule["type"]
    if "enum" in rule and not any(_same(value, option) for option in rule["enum"]):
        return False
    if kind == "string":
        return isinstance(value, str) and _string_fits(rule, value)
    if kind == "integer":
        return type(value) is int and _integer_fits(rule, value)
    if kind == "boolean":
        return isinstance(value, bool)
    return value is None


def detail_violation(action: str, detail: Mapping[str, object]) -> str | None:
    """The first property of `detail` that breaks the schema of `action` (module docstring), None when it fits."""
    schema = DETAIL_SCHEMAS[action]
    properties: Mapping[str, list[Mapping[str, Any]]] = schema["properties"]
    for name in schema["required"]:
        if name not in detail:
            return str(name)
    for name, value in detail.items():
        alternatives = properties.get(name)
        if alternatives is None or not any(_fits(rule, value) for rule in alternatives):
            return name
    return None
