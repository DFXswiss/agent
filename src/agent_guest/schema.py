"""Standard-library JSON Schema validator for the subset used in ``contracts/`` (03-api.md, 05-runner.md §4.6 no. 3).

The keyword whitelist is the one of ``contracts/README.md``: ``type properties required additionalProperties enum
const pattern minLength maxLength minimum maximum items minItems maxItems minProperties maxProperties propertyNames
$ref $defs oneOf anyOf`` plus the
annotations ``$schema $id title description $comment``. A schema file with any other keyword is refused when it is
loaded (``SchemaError``), so the guest never silently ignores a rule.

Platform rules on top of Draft 2020-12:

* ``integer`` is a JSON integer only: booleans and floats (also ``4.0``) are refused; ``number`` is ``int`` or
  ``float`` but never ``bool``. ``enum``/``const`` compare as JSON values (``true`` never equals ``1``).
* ``pattern`` behaves as in ECMA-262: a final ``$`` matches only at the very end of the string.
* ``$ref`` is local (``#/$defs/x``) or relative to the referring file (``../common.json#/$defs/uuid``) and never
  leaves the contracts root; sibling keywords of ``$ref`` apply as well.

Interface:

* ``SchemaSet(root)`` - the schemas below one contracts directory, loaded lazily and cached per file
* ``SchemaSet.errors(rel, instance) -> list[Violation]`` - ``rel`` is a file or ``file#/$defs/name``; every
  violation with the JSON pointer of the offending instance location (a missing property points at the property)
* ``SchemaSet.check(rel, instance)`` - raises ``ValidationFailed(violations)`` unless valid
* ``pointers(violations, limit=32) -> list[str]`` - distinct pointers for an error ``detail``
"""

from __future__ import annotations

import json
import math
import posixpath
import re
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

ASSERTIONS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "enum",
        "const",
        "pattern",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "items",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "propertyNames",
        "$ref",
        "$defs",
        "oneOf",
        "anyOf",
    }
)
ANNOTATIONS = frozenset({"$schema", "$id", "title", "description", "$comment"})
KEYWORDS = ASSERTIONS | ANNOTATIONS
TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
MAX_DEPTH = 64

Schema = Mapping[str, Any]


class SchemaError(Exception):
    """A schema that the validator cannot use: unknown keyword, bad reference, unreadable file."""

    def __init__(self, code: str, where: str) -> None:
        super().__init__(f"{code} {where}")
        self.code = code
        self.where = where


@dataclass(frozen=True)
class Violation:
    """One failed assertion: instance pointer and the keyword that failed."""

    pointer: str
    keyword: str


class ValidationFailed(Exception):
    def __init__(self, violations: list[Violation]) -> None:
        super().__init__(f"{len(violations)} violation(s)")
        self.violations = violations


def escape(token: str) -> str:
    """One JSON-pointer reference token (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


def unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def pointers(violations: list[Violation], limit: int = 32) -> list[str]:
    """Distinct instance pointers in order of appearance, at most ``limit``."""
    found: list[str] = []
    for violation in violations:
        if violation.pointer not in found:
            found.append(violation.pointer)
    return found[:limit]


@cache
def ecma_pattern(pattern: str) -> re.Pattern[str]:
    """``pattern`` compiled so that a final ``$`` matches only at the end of the string (ECMA-262)."""
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.compile(pattern)


_TYPE_NAMES = {
    type(None): "null",
    bool: "boolean",
    int: "integer",
    float: "number",
    str: "string",
    list: "array",
    dict: "object",
}


def json_type(value: object) -> str:
    """JSON type name of a decoded value; ``integer`` for ints, ``number`` for floats."""
    return _TYPE_NAMES.get(type(value), "unknown")


def has_type(value: object, name: str) -> bool:
    actual = json_type(value)
    if name == "number":
        return actual == "integer" or (isinstance(value, float) and math.isfinite(value))
    return actual == name


def json_equal(left: object, right: object) -> bool:
    """Equality of JSON values: booleans never equal numbers, ``1`` equals ``1.0``."""
    left_type, right_type = json_type(left), json_type(right)
    numeric = ("integer", "number")
    if left_type in numeric and right_type in numeric:
        return left == right
    if left_type != right_type:
        return False
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(json_equal(left[key], right[key]) for key in left)
    return left == right


def keyword_violations(schema: object, where: str = "") -> Iterator[str]:
    """Pointers ``<schema pointer>/<keyword>`` of keywords outside the whitelist and malformed keyword values."""
    if not isinstance(schema, dict):
        yield where or "/"
        return
    for key, value in schema.items():
        if key not in KEYWORDS:
            yield f"{where}/{escape(key)}"
        elif key in ("properties", "$defs"):
            if not isinstance(value, dict):
                yield f"{where}/{key}"
                continue
            for name, child in value.items():
                yield from keyword_violations(child, f"{where}/{key}/{escape(name)}")
        elif key in ("items", "additionalProperties", "propertyNames"):
            if not isinstance(value, bool):
                yield from keyword_violations(value, f"{where}/{key}")
        elif key in ("oneOf", "anyOf"):
            if not isinstance(value, list) or not value:
                yield f"{where}/{key}"
                continue
            for index, child in enumerate(value):
                yield from keyword_violations(child, f"{where}/{key}/{index}")
        elif key == "type" and value not in TYPES:
            yield f"{where}/type"


class SchemaSet:
    """Schemas below ``root``, addressed by POSIX path relative to it (``guest/assign.request.json``)."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._files: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def load(self, rel: str) -> dict[str, Any]:
        """The schema document at ``rel``; refused if it is not a schema or uses keywords outside the subset."""
        with self._lock:
            found = self._files.get(rel)
            if found is not None:
                return found
            normal = posixpath.normpath(rel)
            if normal != rel or rel.startswith(("/", "../")) or rel == "..":
                raise SchemaError("schema_path_invalid", rel)
            try:
                document = json.loads((self.root / rel).read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise SchemaError("schema_unreadable", rel) from error
            if not isinstance(document, dict) or "$schema" not in document:
                raise SchemaError("schema_invalid", rel)
            bad = list(keyword_violations(document))
            if bad:
                raise SchemaError("schema_keyword", f"{rel}#{bad[0]}")
            self._files[rel] = document
            return document

    def resolve(self, base: str, reference: str) -> tuple[str, Schema]:
        """Target file and schema of ``reference`` written in the file ``base``."""
        path, _, fragment = reference.partition("#")
        target = posixpath.normpath(posixpath.join(posixpath.dirname(base), path)) if path else base
        node: Any = self.load(target)
        if fragment:
            if not fragment.startswith("/"):
                raise SchemaError("schema_ref_invalid", f"{base}: {reference}")
            for token in fragment[1:].split("/"):
                key = unescape(token)
                if not isinstance(node, dict) or key not in node:
                    raise SchemaError("schema_ref_invalid", f"{base}: {reference}")
                node = node[key]
        if not isinstance(node, dict):
            raise SchemaError("schema_ref_invalid", f"{base}: {reference}")
        return target, node

    def errors(self, rel: str, instance: object) -> list[Violation]:
        """Violations of ``instance`` against the schema ``rel`` (a file, optionally with ``#/$defs/...``)."""
        path, _, fragment = rel.partition("#")
        base, schema = self.resolve(path, "#" + fragment) if fragment else (path, self.load(path))
        walk = _Walk(self)
        walk.validate(base, schema, instance, "", 0)
        return walk.out

    def check(self, rel: str, instance: object) -> None:
        found = self.errors(rel, instance)
        if found:
            raise ValidationFailed(found)

    def is_valid(self, rel: str, instance: object) -> bool:
        return not self.errors(rel, instance)


class _Walk:
    """One validation run; collects the violations in ``out``."""

    def __init__(self, schemas: SchemaSet) -> None:
        self.schemas = schemas
        self.out: list[Violation] = []

    def validate(self, base: str, schema: Schema, value: object, where: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            self.out.append(Violation(where, "depth"))
            return
        if "$ref" in schema:
            target, referenced = self.schemas.resolve(base, schema["$ref"])
            self.validate(target, referenced, value, where, depth + 1)
        expected = schema.get("type")
        if expected is not None and not has_type(value, expected):
            self.out.append(Violation(where, "type"))
            return
        if "const" in schema and not json_equal(value, schema["const"]):
            self.out.append(Violation(where, "const"))
        if "enum" in schema and not any(json_equal(value, option) for option in schema["enum"]):
            self.out.append(Violation(where, "enum"))
        if isinstance(value, str):
            self.string(schema, value, where)
        elif isinstance(value, int | float) and not isinstance(value, bool):
            self.number(schema, value, where)
        elif isinstance(value, list):
            self.array(base, schema, value, where, depth)
        elif isinstance(value, dict):
            self.mapping(base, schema, value, where, depth)
        self.choice(base, schema, value, where, depth)

    def string(self, schema: Schema, value: str, where: str) -> None:
        if "minLength" in schema and len(value) < schema["minLength"]:
            self.out.append(Violation(where, "minLength"))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            self.out.append(Violation(where, "maxLength"))
        if "pattern" in schema and not ecma_pattern(schema["pattern"]).search(value):
            self.out.append(Violation(where, "pattern"))

    def number(self, schema: Schema, value: float, where: str) -> None:
        if "minimum" in schema and value < schema["minimum"]:
            self.out.append(Violation(where, "minimum"))
        if "maximum" in schema and value > schema["maximum"]:
            self.out.append(Violation(where, "maximum"))

    def array(self, base: str, schema: Schema, value: list[Any], where: str, depth: int) -> None:
        if "minItems" in schema and len(value) < schema["minItems"]:
            self.out.append(Violation(where, "minItems"))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            self.out.append(Violation(where, "maxItems"))
        items = schema.get("items")
        if items is False and value:
            self.out.append(Violation(where, "items"))
        elif isinstance(items, dict):
            for index, item in enumerate(value):
                self.validate(base, items, item, f"{where}/{index}", depth + 1)

    def mapping(self, base: str, schema: Schema, value: dict[str, Any], where: str, depth: int) -> None:
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            self.out.append(Violation(where, "minProperties"))
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            self.out.append(Violation(where, "maxProperties"))
        names = schema.get("propertyNames")
        if isinstance(names, dict):
            for name in value:
                self.validate(base, names, name, f"{where}/{escape(name)}", depth + 1)
        properties: Mapping[str, Any] = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                self.out.append(Violation(f"{where}/{escape(name)}", "required"))
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            here = f"{where}/{escape(name)}"
            if name in properties:
                self.validate(base, properties[name], item, here, depth + 1)
            elif extra is False:
                self.out.append(Violation(here, "additionalProperties"))
            elif isinstance(extra, dict):
                self.validate(base, extra, item, here, depth + 1)

    def choice(self, base: str, schema: Schema, value: object, where: str, depth: int) -> None:
        for keyword in ("oneOf", "anyOf"):
            if keyword not in schema:
                continue
            results: list[list[Violation]] = []
            for option in schema[keyword]:
                branch = _Walk(self.schemas)
                branch.validate(base, option, value, where, depth + 1)
                results.append(branch.out)
            passed = sum(1 for found in results if not found)
            if passed == 0:
                # Report the closest option (fewest violations, first on ties) so the pointers stay useful.
                closest: list[Violation] = min(results, key=len)
                self.out.extend(closest)
                self.out.append(Violation(where, keyword))
            elif keyword == "oneOf" and passed > 1:
                self.out.append(Violation(where, keyword))
