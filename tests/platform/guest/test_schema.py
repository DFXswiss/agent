"""U-GST-schema: the stdlib JSON Schema validator against every contract fixture and its own edge cases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_guest.schema import (
    MAX_DEPTH,
    SchemaError,
    SchemaSet,
    ValidationFailed,
    Violation,
    ecma_pattern,
    escape,
    json_equal,
    json_type,
    keyword_violations,
    pointers,
    unescape,
)

DRAFT = "https://json-schema.org/draft/2020-12/schema"
SKIPPED = frozenset({"node_modules", "fixtures", "__pycache__", ".pytest_cache"})


def _schema_files(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.json")):
        rel = path.relative_to(root)
        if SKIPPED.intersection(rel.parts[:-1]):
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(document, dict) and "$schema" in document:
            found.append(rel.as_posix())
    return found


def _fixtures(root: Path) -> list[tuple[str, str, str]]:
    cases = []
    for rel in _schema_files(root):
        base = root / "fixtures" / rel.removesuffix(".json")
        for kind in ("valid", "invalid"):
            for path in sorted((base / kind).glob("*.json")):
                cases.append((rel, kind, path.relative_to(root).as_posix()))
    return cases


CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
SCHEMA_FILES = _schema_files(CONTRACTS)
FIXTURES = _fixtures(CONTRACTS)


def test_contracts_are_present() -> None:
    assert len(SCHEMA_FILES) > 100
    assert len(FIXTURES) > 500


@pytest.mark.parametrize("rel", SCHEMA_FILES)
def test_every_contract_schema_loads(schemas: SchemaSet, rel: str) -> None:
    assert schemas.load(rel)["$schema"] == DRAFT


@pytest.mark.parametrize(("rel", "kind", "fixture"), FIXTURES, ids=[case[2] for case in FIXTURES])
def test_contract_fixture(schemas: SchemaSet, contracts_dir: Path, rel: str, kind: str, fixture: str) -> None:
    instance = json.loads((contracts_dir / fixture).read_text(encoding="utf-8"))
    violations = schemas.errors(rel, instance)
    if kind == "valid":
        assert violations == []
    else:
        assert violations, f"{fixture} must be refused"


# ---------------------------------------------------------------- own edge cases


def write(root: Path, rel: str, schema: dict[str, Any]) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"$schema": DRAFT, **schema}), encoding="utf-8")


@pytest.fixture
def lab(tmp_path: Path) -> tuple[Path, SchemaSet]:
    return tmp_path, SchemaSet(tmp_path)


def check(lab: tuple[Path, SchemaSet], schema: dict[str, Any], instance: object) -> list[Violation]:
    root, _ = lab
    write(root, "t.json", schema)
    return SchemaSet(root).errors("t.json", instance)


@pytest.mark.parametrize(
    ("schema", "instance", "keywords"),
    [
        ({"type": "integer"}, 3, []),
        ({"type": "integer"}, True, ["type"]),
        ({"type": "integer"}, 4.0, ["type"]),
        ({"type": "number"}, 4, []),
        ({"type": "number"}, 4.5, []),
        ({"type": "number"}, False, ["type"]),
        ({"type": "number"}, float("inf"), ["type"]),
        ({"type": "boolean"}, 0, ["type"]),
        ({"type": "null"}, None, []),
        ({"type": "string"}, b"x", ["type"]),
        ({"type": "array"}, (1,), ["type"]),
        ({"type": "object"}, [], ["type"]),
        ({"const": 1}, True, ["const"]),
        ({"const": 1}, 1.0, []),
        ({"const": True}, 1, ["const"]),
        ({"const": [1, {"a": None}]}, [1, {"a": None}], []),
        ({"const": [1, 2]}, [1], ["const"]),
        ({"const": {"a": 1}}, {"b": 1}, ["const"]),
        ({"const": {"a": 1}}, {"a": 2}, ["const"]),
        ({"const": "x"}, "x", []),
        ({"enum": ["a", 2]}, 2, []),
        ({"enum": ["a", 2]}, "b", ["enum"]),
        ({"pattern": "^[a-z]+$"}, "abc", []),
        ({"pattern": "^[a-z]+$"}, "abc\n", ["pattern"]),
        ({"pattern": "^a\\$"}, "a$", []),
        ({"pattern": "b"}, "abc", []),
        ({"minLength": 2, "maxLength": 3}, "é", ["minLength"]),
        ({"minLength": 2, "maxLength": 3}, "éééé", ["maxLength"]),
        ({"minLength": 2}, 5, []),
        ({"minimum": 1, "maximum": 2}, 0, ["minimum"]),
        ({"minimum": 1, "maximum": 2}, 2.5, ["maximum"]),
        ({"minimum": 1}, "0", []),
        ({"minItems": 1, "maxItems": 2}, [], ["minItems"]),
        ({"minItems": 1, "maxItems": 2}, [1, 2, 3], ["maxItems"]),
        ({"minProperties": 1, "maxProperties": 2}, {}, ["minProperties"]),
        ({"minProperties": 1, "maxProperties": 2}, {"a": 1, "b": 2, "c": 3}, ["maxProperties"]),
        ({"minProperties": 1, "maxProperties": 2}, {"a": 1}, []),
        ({"propertyNames": {"pattern": "^[a-z]+$"}}, {"ab": 1, "A": 2}, ["pattern"]),
        ({"propertyNames": True}, {"A": 1}, []),
        ({"items": False}, [1], ["items"]),
        ({"items": False}, [], []),
        ({"items": True}, [1], []),
        ({"items": {"type": "string"}}, ["a", 1], ["type"]),
        ({"additionalProperties": {"type": "integer"}}, {"a": 1, "b": "x"}, ["type"]),
        ({"additionalProperties": True}, {"a": 1}, []),
        (
            {"properties": {"a": {"type": "string"}}, "additionalProperties": False},
            {"a": "x", "b": 1},
            ["additionalProperties"],
        ),
        ({"anyOf": [{"type": "string"}, {"type": "null"}]}, None, []),
        ({"anyOf": [{"type": "string"}, {"type": "null"}]}, 1, ["type", "anyOf"]),
        ({"oneOf": [{"type": "integer"}, {"minimum": 0}]}, 1, ["oneOf"]),
        ({"oneOf": [{"type": "integer"}, {"type": "string"}]}, "a", []),
    ],
)
def test_keywords(lab: tuple[Path, SchemaSet], schema: dict[str, Any], instance: object, keywords: list[str]) -> None:
    assert [violation.keyword for violation in check(lab, schema, instance)] == keywords


def test_pointers_of_nested_violations(lab: tuple[Path, SchemaSet]) -> None:
    schema = {
        "type": "object",
        "properties": {
            "params": {
                "type": "object",
                "properties": {"repo": {"type": "string"}, "a/b~c": {"type": "integer"}},
                "required": ["repo", "x/y"],
                "additionalProperties": False,
            },
            "list": {"type": "array", "items": {"type": "integer"}},
        },
        "additionalProperties": False,
    }
    found = check(lab, schema, {"params": {"repo": 1, "a/b~c": "s"}, "list": [1, "x"], "extra": 1})
    assert pointers(found) == ["/params/x~1y", "/params/repo", "/params/a~1b~0c", "/list/1", "/extra"]
    assert pointers(found, limit=2) == ["/params/x~1y", "/params/repo"]


def test_closest_branch_pointers(lab: tuple[Path, SchemaSet]) -> None:
    schema = {
        "oneOf": [
            {"type": "object", "properties": {"k": {"const": "a"}, "n": {"type": "integer"}}, "required": ["k", "n"]},
            {"type": "object", "properties": {"k": {"const": "b"}}, "required": ["k"], "additionalProperties": False},
        ]
    }
    found = check(lab, schema, {"k": "a", "n": "x"})
    assert pointers(found) == ["/n", ""]


def test_refs_local_relative_and_siblings(lab: tuple[Path, SchemaSet]) -> None:
    root, _ = lab
    write(root, "common.json", {"$defs": {"id": {"type": "string", "pattern": "^[a-z]+$"}, "a~b/c": {"type": "null"}}})
    write(
        root,
        "sub/msg.json",
        {
            "$ref": "#/$defs/body",
            "$defs": {
                "body": {
                    "type": "object",
                    "properties": {
                        "id": {"$ref": "../common.json#/$defs/id", "maxLength": 3},
                        "n": {"$ref": "../common.json#/$defs/a~0b~1c"},
                        "self": {"$ref": "other.json"},
                    },
                    "additionalProperties": False,
                }
            },
        },
    )
    write(root, "sub/other.json", {"type": "boolean"})
    schemas = SchemaSet(root)
    assert schemas.errors("sub/msg.json", {"id": "abc", "n": None, "self": True}) == []
    assert [v.keyword for v in schemas.errors("sub/msg.json", {"id": "abcd"})] == ["maxLength"]
    assert [v.keyword for v in schemas.errors("sub/msg.json", {"id": "A"})] == ["pattern"]
    assert schemas.errors("common.json#/$defs/id", "abc") == []
    assert schemas.is_valid("sub/msg.json", {"n": None})
    assert not schemas.is_valid("sub/msg.json", {"n": 1})
    schemas.check("sub/other.json", False)
    with pytest.raises(ValidationFailed) as failed:
        schemas.check("sub/other.json", 1)
    assert failed.value.violations == [Violation("", "type")]
    assert schemas.load("common.json") is schemas.load("common.json")


@pytest.mark.parametrize(
    ("reference", "code"),
    [
        ("#/$defs/missing", "schema_ref_invalid"),
        ("#$defs", "schema_ref_invalid"),
        ("#/$defs/x/type", "schema_ref_invalid"),
        ("#/$defs/x/required", "schema_ref_invalid"),
        ("../../outside.json", "schema_path_invalid"),
        ("nothere.json", "schema_unreadable"),
        ("data.json", "schema_invalid"),
        ("broken.json", "schema_unreadable"),
    ],
)
def test_bad_references(lab: tuple[Path, SchemaSet], reference: str, code: str) -> None:
    root, schemas = lab
    write(root, "a/t.json", {"$ref": reference, "$defs": {"x": {"type": "string", "required": ["a"]}}})
    (root / "a" / "data.json").write_text("[1]", encoding="utf-8")
    (root / "a" / "broken.json").write_text("{", encoding="utf-8")
    with pytest.raises(SchemaError) as error:
        schemas.errors("a/t.json", "x")
    assert error.value.code == code


@pytest.mark.parametrize("rel", ["/abs.json", "../x.json", "..", "a/../b.json", "./a.json"])
def test_load_refuses_paths_outside_or_not_normal(lab: tuple[Path, SchemaSet], rel: str) -> None:
    _, schemas = lab
    with pytest.raises(SchemaError) as error:
        schemas.load(rel)
    assert error.value.code == "schema_path_invalid"


@pytest.mark.parametrize(
    ("schema", "where"),
    [
        ({"format": "uuid"}, "/format"),
        ({"type": "int"}, "/type"),
        ({"properties": []}, "/properties"),
        ({"properties": {"a": {"if": {}}}}, "/properties/a/if"),
        ({"$defs": {"x/y": {"not": {}}}}, "/$defs/x~1y/not"),
        ({"items": {"contains": {}}}, "/items/contains"),
        ({"additionalProperties": 3}, "/additionalProperties"),
        ({"oneOf": []}, "/oneOf"),
        ({"anyOf": {"a": 1}}, "/anyOf"),
        ({"anyOf": [{"allOf": []}]}, "/anyOf/0/allOf"),
    ],
)
def test_keyword_whitelist(lab: tuple[Path, SchemaSet], schema: dict[str, Any], where: str) -> None:
    root, schemas = lab
    assert next(iter(keyword_violations(schema))) == where
    write(root, "t.json", schema)
    with pytest.raises(SchemaError) as error:
        schemas.load("t.json")
    assert error.value.code == "schema_keyword"
    assert error.value.where == f"t.json#{where}"


def test_keyword_violations_of_a_non_object() -> None:
    assert list(keyword_violations(3)) == ["/"]
    assert list(keyword_violations(3, "/items")) == ["/items"]


def test_depth_limit(lab: tuple[Path, SchemaSet]) -> None:
    value: Any = 1
    for _ in range(MAX_DEPTH + 2):
        value = [value]
    found = check(lab, {"$ref": "#/$defs/node", "$defs": {"node": {"items": {"$ref": "#/$defs/node"}}}}, value)
    assert [violation.keyword for violation in found] == ["depth"]


def test_helpers() -> None:
    assert escape("a/b~c") == "a~1b~0c"
    assert unescape("a~1b~0c") == "a/b~c"
    assert json_type((1,)) == "unknown"
    assert json_type(None) == "null"
    assert json_equal(1, 1.0)
    assert not json_equal(True, 1)
    assert not json_equal("1", 1)
    assert ecma_pattern("^a$").pattern == "^a\\Z"
    assert pointers([Violation("/a", "type"), Violation("/a", "enum"), Violation("/b", "type")]) == ["/a", "/b"]
