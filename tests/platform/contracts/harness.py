"""Validation harness of `contracts/` (docs/platform/spec/07-testing.md section 4, CON-01).

Loads every JSON Schema of the contracts directory into one registry and validates instances with the rules the
platform requires on top of Draft 2020-12:

- only the keywords of `ALLOWED_KEYWORDS` (the subset the stdlib validator of the guest implements),
- integers are JSON integers only: booleans and whole floats such as `4.0` are refused,
- `pattern` anchors behave as in ECMA-262: `$` matches only at the very end (Python's `$` also matches before a
  trailing line feed).

Other platform suites (hub, web tooling) use `Contracts.load()` and `Contracts.validate(schema_path, instance)` to check
the messages they send and receive.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError, validators
from jsonschema.protocols import Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = REPO_ROOT / "contracts"
DRAFT = "https://json-schema.org/draft/2020-12/schema"
ID_BASE = "https://agent.dfx.swiss/contracts/"
SKIPPED_DIRS = frozenset({"node_modules", "fixtures", "__pycache__", ".pytest_cache"})

# The keyword whitelist of contracts/README.md (the subset implemented by the guest's stdlib validator).
ASSERTION_KEYWORDS = frozenset(
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
        "$ref",
        "$defs",
        "oneOf",
        "anyOf",
    }
)
ANNOTATION_KEYWORDS = frozenset({"$schema", "$id", "title", "description", "$comment"})
ALLOWED_KEYWORDS = ASSERTION_KEYWORDS | ANNOTATION_KEYWORDS

Schema = dict[str, Any]


def _is_integer(_checker: object, instance: object) -> bool:
    return type(instance) is int


def _is_number(_checker: object, instance: object) -> bool:
    return type(instance) in (int, float)


@cache
def ecma_pattern(pattern: str) -> re.Pattern[str]:
    """`pattern` compiled so that a final `$` matches only at the end of the string, as in ECMA-262."""
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.compile(pattern)


def _pattern(validator: Validator, pattern: str, instance: object, schema: Schema) -> Iterator[ValidationError]:
    if isinstance(instance, str) and not ecma_pattern(pattern).search(instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


# `validators.extend` carries no annotations in jsonschema; the result implements the `Validator` protocol.
StrictValidator: type[Validator] = validators.extend(  # type: ignore[no-untyped-call]
    Draft202012Validator,
    validators={"pattern": _pattern},
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine_many({"integer": _is_integer, "number": _is_number}),
)


def subschemas(schema: object, pointer: str = "") -> Iterator[tuple[str, Schema]]:
    """Every schema object inside `schema` (the root included), with its JSON pointer.

    Walks only positions that hold schemas under the whitelist; `enum` and `const` values are data. Unknown keywords
    are reported by `keyword_violations`, not walked."""
    if not isinstance(schema, dict):
        return
    yield pointer, schema
    for key in ("properties", "$defs"):
        children = schema.get(key)
        if isinstance(children, dict):
            for name, child in children.items():
                yield from subschemas(child, f"{pointer}/{key}/{name}")
    for key in ("items", "additionalProperties"):
        yield from subschemas(schema.get(key), f"{pointer}/{key}")
    for key in ("oneOf", "anyOf"):
        for index, child in enumerate(schema.get(key) or []):
            yield from subschemas(child, f"{pointer}/{key}/{index}")


def keyword_violations(schema: Schema) -> list[str]:
    """Pointers of keywords outside the whitelist (`<pointer>/<keyword>`)."""
    return [
        f"{pointer}/{keyword}"
        for pointer, sub in subschemas(schema)
        for keyword in sub
        if keyword not in ALLOWED_KEYWORDS
    ]


OPEN_MARKER = "open: "


def is_open_input(schema: Schema) -> bool:
    """A declared third-party input object (contracts/README.md): `additionalProperties: {}` and a `$comment` starting
    with `open: ` that names the producer. Only hook and MCP client inputs of Claude Code may use it (CTR-02)."""
    return schema.get("additionalProperties") == {} and str(schema.get("$comment", "")).startswith(OPEN_MARKER)


def object_violations(schema: Schema) -> list[str]:
    """Pointers of object schemas that are open: every `type: object` declares `additionalProperties`, and one with
    `properties` sets it to `false` (maps keyed by IDs carry a value schema instead) unless it is a declared
    third-party input (`is_open_input`)."""
    found = []
    for pointer, sub in subschemas(schema):
        is_object = sub.get("type") == "object" or "properties" in sub
        if not is_object:
            continue
        extra = sub.get("additionalProperties")
        if extra is None or ("properties" in sub and extra is not False and not is_open_input(sub)):
            found.append(pointer or "/")
    return found


def open_objects(schema: Schema) -> list[str]:
    """Pointers of the declared third-party input objects of `schema`."""
    return [pointer or "/" for pointer, sub in subschemas(schema) if "properties" in sub and is_open_input(sub)]


def refs(schema: Schema) -> Iterator[tuple[str, str]]:
    """Every `$ref` value with its pointer."""
    for pointer, sub in subschemas(schema):
        if "$ref" in sub:
            yield pointer, sub["$ref"]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def schema_files(root: Path) -> dict[str, Schema]:
    """Contract-relative POSIX path → schema, for every JSON file whose top level declares `$schema`."""
    found: dict[str, Schema] = {}
    for path in sorted(root.rglob("*.json")):
        rel = path.relative_to(root)
        if SKIPPED_DIRS.intersection(rel.parts[:-1]):
            continue
        document = load_json(path)
        if isinstance(document, dict) and "$schema" in document:
            found[rel.as_posix()] = document
    return found


def is_library(schema: Schema) -> bool:
    """A definitions-only schema (`common.json`, `api/objects.json`): no assertion at the top level."""
    return not (set(schema) & ASSERTION_KEYWORDS - {"$defs"})


def fixture_dir(root: Path, rel: str) -> Path:
    """`fixtures/<schema path without .json>/` (07-testing.md section 4, CTR-01)."""
    return root / "fixtures" / rel.removesuffix(".json")


@dataclass
class Contracts:
    """All schemas of one contracts directory, resolvable by path or `$id`."""

    root: Path
    schemas: dict[str, Schema]
    registry: Registry[Any] = field(repr=False)

    @classmethod
    def load(cls, root: Path = CONTRACTS) -> Contracts:
        schemas = schema_files(root)
        resources = [(ID_BASE + rel, DRAFT202012.create_resource(schema)) for rel, schema in schemas.items()]
        return cls(root=root, schemas=schemas, registry=Registry().with_resources(resources))

    def validator(self, rel: str) -> Validator:
        return StrictValidator(self.schemas[rel], registry=self.registry)

    def errors(self, rel: str, instance: Any) -> list[ValidationError]:
        return list(self.validator(rel).iter_errors(instance))

    def validate(self, rel: str, instance: Any) -> None:
        """Raise `jsonschema.ValidationError` unless `instance` matches the schema at `rel` (`api/me.response.json`)."""
        self.validator(rel).validate(instance)

    def resolve(self, rel: str, reference: str) -> object:
        """Resolve `reference` as written in the schema at `rel`."""
        resolver = self.registry.resolver(base_uri=ID_BASE + rel)
        return resolver.lookup(reference).contents

    def fixtures(self, rel: str) -> Mapping[str, list[Path]]:
        base = fixture_dir(self.root, rel)
        return {kind: sorted((base / kind).glob("*.json")) for kind in ("valid", "invalid")}
