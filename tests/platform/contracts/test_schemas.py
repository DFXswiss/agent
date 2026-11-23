"""CON-01: every schema is valid Draft 2020-12, uses only whitelisted keywords, is closed, resolves all `$ref`s and
classifies its fixtures correctly (docs/platform/spec/07-testing.md section 4, CTR-01)."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tests.platform.contracts.harness import (
    ALLOWED_KEYWORDS,
    CONTRACTS,
    DRAFT,
    ID_BASE,
    Contracts,
    fixture_dir,
    is_library,
    keyword_violations,
    load_json,
    object_violations,
    open_objects,
    refs,
    schema_files,
    subschemas,
)

SCHEMAS = schema_files(CONTRACTS)
REF_RE = re.compile(r"(\.\./)*([a-z0-9-]+/)*[a-z0-9.-]+\.json#/\$defs/[a-z0-9_]+|#/\$defs/[a-z0-9_]+")
LIBRARIES = sorted(rel for rel, schema in SCHEMAS.items() if is_library(schema))
MESSAGES = sorted(rel for rel, schema in SCHEMAS.items() if not is_library(schema))


def test_schemas_are_found() -> None:
    assert "common.json" in SCHEMAS
    assert {"api/objects.json", "common.json"} <= set(LIBRARIES)
    assert len(MESSAGES) >= 2 * 62 + 1


@pytest.mark.parametrize("rel", sorted(SCHEMAS))
def test_schema_header(rel: str) -> None:
    schema = SCHEMAS[rel]
    assert schema["$schema"] == DRAFT
    assert schema["$id"] == ID_BASE + rel
    assert schema.get("title"), "every schema has a title"
    assert schema.get("description"), "every schema has a description"
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("rel", sorted(SCHEMAS))
def test_only_whitelisted_keywords(rel: str) -> None:
    assert keyword_violations(SCHEMAS[rel]) == []


@pytest.mark.parametrize("rel", sorted(SCHEMAS))
def test_types_are_single_names(rel: str) -> None:
    """Unions are written with `anyOf` (contracts/README.md), which the stdlib validator and Ajv strict mode share."""
    for pointer, sub in subschemas(SCHEMAS[rel]):
        if "type" in sub:
            assert sub["type"] in {"object", "array", "string", "integer", "number", "boolean", "null"}, pointer


# Constructs whose meaning differs between Python `re` and ECMA-262 (or that only one of them supports).
NON_PORTABLE = re.compile(r"\\[dDwWsSbBAZz]|\(\?[<P=!]|\\[pPuxk]")


@pytest.mark.parametrize("rel", sorted(SCHEMAS))
def test_patterns_are_anchored_and_portable(rel: str) -> None:
    """contracts/README.md: patterns are anchored and use the common subset of Python `re` and ECMA-262."""
    for pointer, sub in subschemas(SCHEMAS[rel]):
        if "pattern" in sub:
            pattern = sub["pattern"]
            assert pattern.startswith("^"), pointer
            assert pattern.endswith("$"), pointer
            assert not NON_PORTABLE.search(pattern), (pointer, pattern)
            re.compile(pattern)


@pytest.mark.parametrize(
    ("pattern", "portable"),
    [
        ("^[0-9]+$", True),
        ("^\\d+$", False),
        ("^a\\Z", False),
        ("^(?<n>a)$", False),
        ("^(?P<n>a)$", False),
        ("^\\.$", True),
    ],
)
def test_non_portable_constructs_are_found(pattern: str, portable: bool) -> None:
    assert (NON_PORTABLE.search(pattern) is None) is portable


@pytest.mark.parametrize("rel", sorted(SCHEMAS))
def test_objects_are_closed(rel: str) -> None:
    assert object_violations(SCHEMAS[rel]) == []


@pytest.mark.parametrize("rel", sorted(SCHEMAS))
def test_refs_resolve(contracts: Contracts, rel: str) -> None:
    for pointer, reference in refs(SCHEMAS[rel]):
        assert REF_RE.fullmatch(reference), (pointer, reference)
        target = contracts.resolve(rel, reference)
        assert isinstance(target, dict), (pointer, reference)


@pytest.mark.parametrize("rel", MESSAGES)
def test_fixtures_exist(contracts: Contracts, rel: str) -> None:
    found = contracts.fixtures(rel)
    assert found["valid"], f"no valid fixture in {fixture_dir(CONTRACTS, rel)}"
    assert found["invalid"], f"no invalid fixture in {fixture_dir(CONTRACTS, rel)}"


def test_every_fixture_directory_belongs_to_a_schema() -> None:
    root = CONTRACTS / "fixtures"
    expected = {fixture_dir(CONTRACTS, rel) for rel in MESSAGES}
    found = {path.parent.parent for path in root.rglob("*.json")}
    assert found <= expected, sorted(str(p.relative_to(root)) for p in found - expected)
    kinds = {path.parent.name for path in root.rglob("*.json")}
    assert kinds == {"valid", "invalid"}


def _fixtures(kind: str) -> list[tuple[str, Path]]:
    return [(rel, path) for rel in MESSAGES for path in sorted((fixture_dir(CONTRACTS, rel) / kind).glob("*.json"))]


@pytest.mark.parametrize(("rel", "path"), _fixtures("valid"), ids=lambda v: v if isinstance(v, str) else v.stem)
def test_valid_fixture(contracts: Contracts, rel: str, path: Path) -> None:
    contracts.validate(rel, load_json(path))


@pytest.mark.parametrize(("rel", "path"), _fixtures("invalid"), ids=lambda v: v if isinstance(v, str) else v.stem)
def test_invalid_fixture(contracts: Contracts, rel: str, path: Path) -> None:
    assert contracts.errors(rel, load_json(path)), f"{path} must be invalid"


@pytest.mark.parametrize(("rel", "path"), _fixtures("valid"), ids=lambda v: v if isinstance(v, str) else v.stem)
def test_mutations_of_valid_fixtures(contracts: Contracts, rel: str, path: Path) -> None:
    """Beyond the fixture files: every required top-level field is required and no top-level extra field passes."""
    instance = load_json(path)
    if not isinstance(instance, dict):
        return
    for key in instance:
        variant = copy.deepcopy(instance)
        del variant[key]
        if key in _required(contracts, rel, instance):
            assert contracts.errors(rel, variant), f"{key} is required"
    variant = copy.deepcopy(instance)
    variant["zz_unexpected"] = None
    if "/" in open_objects(SCHEMAS[rel]):
        contracts.validate(rel, variant)  # declared third-party input: unknown fields are ignored
    else:
        assert contracts.errors(rel, variant)


def _required(contracts: Contracts, rel: str, instance: dict[str, object]) -> set[str]:
    schema = SCHEMAS[rel]
    while "$ref" in schema:
        target = contracts.resolve(rel, schema["$ref"])
        assert isinstance(target, dict)
        schema = target
    alternatives = schema.get("oneOf") or [schema]
    required: set[str] = set()
    for alternative in alternatives:
        if set(alternative.get("required", [])) <= set(instance):
            required |= set(alternative.get("required", []))
    return required


@pytest.mark.parametrize(
    ("schema", "violations"),
    [
        ({"type": "string", "format": "uuid"}, ["/format"]),
        ({"type": "object", "patternProperties": {}, "additionalProperties": False}, ["/patternProperties"]),
        ({"allOf": [{"type": "string"}]}, ["/allOf"]),
        ({"not": {"type": "null"}}, ["/not"]),
        ({"properties": {"a": {"if": {}}}, "additionalProperties": False}, ["/properties/a/if"]),
        ({"$defs": {"x": {"items": {"prefixItems": []}}}}, ["/$defs/x/items/prefixItems"]),
        ({"anyOf": [{"type": "integer", "exclusiveMinimum": 0}]}, ["/anyOf/0/exclusiveMinimum"]),
        ({"type": "object", "additionalProperties": {"uniqueItems": True}}, ["/additionalProperties/uniqueItems"]),
        ({"type": "object", "properties": {"format": {"type": "string"}}, "additionalProperties": False}, []),
        ({"enum": [{"format": 1}], "const": {"not": 1}}, []),
    ],
)
def test_keyword_whitelist_rejects_other_keywords(schema: dict[str, object], violations: list[str]) -> None:
    assert keyword_violations(schema) == violations


def test_whitelist_matches_readme() -> None:
    readme = (CONTRACTS / "README.md").read_text()
    section = readme.split("## Allowed schema keywords", 1)[1].split("\n## ", 1)[0]
    listed = set(re.findall(r"^- `([^`]+)`", section, re.MULTILINE))
    assert listed == set(ALLOWED_KEYWORDS)


@pytest.mark.parametrize(
    ("schema", "violations"),
    [
        ({"type": "object"}, ["/"]),
        ({"type": "object", "properties": {}}, ["/"]),
        ({"type": "object", "properties": {}, "additionalProperties": True}, ["/"]),
        ({"type": "object", "properties": {}, "additionalProperties": {"type": "string"}}, ["/"]),
        ({"properties": {"a": {"type": "object", "additionalProperties": False}}, "additionalProperties": False}, []),
        ({"type": "object", "additionalProperties": {"type": "string"}}, []),
        ({"anyOf": [{"type": "object", "properties": {}}]}, ["/anyOf/0"]),
    ],
)
def test_open_objects_are_found(schema: dict[str, object], violations: list[str]) -> None:
    assert object_violations(schema) == violations


INT_SCHEMA = "api/wraps-post.response.json"


@pytest.mark.parametrize("value", [True, False, 1.0, 4.5, "1", None])
def test_integers_reject_booleans_and_floats(contracts: Contracts, value: object) -> None:
    assert contracts.errors(INT_SCHEMA, {"stored": value, "ignored": 0})


@pytest.mark.parametrize("value", [0, 1, 2**53 - 1])
def test_integers_accept_integers(contracts: Contracts, value: int) -> None:
    contracts.validate(INT_SCHEMA, {"stored": value, "ignored": 0})


def test_numbers_reject_booleans(contracts: Contracts) -> None:
    sample = load_json(CONTRACTS / "fixtures/api/me-quotas.response/valid/basic.json")
    sample["quotas"][0]["used_percent"] = True
    assert contracts.errors("api/me-quotas.response.json", sample)
    sample["quotas"][0]["used_percent"] = 7
    contracts.validate("api/me-quotas.response.json", sample)


def test_pattern_end_anchor_is_ecma(contracts: Contracts) -> None:
    """`$` must not match before a trailing line feed (Python `re` would)."""
    assert contracts.errors("api/health.response.json", {"ok": True, "version": "0.1.0\n"})
    contracts.validate("api/health.response.json", {"ok": True, "version": "0.1.0"})


def test_load_from_another_root(tmp_path: Path) -> None:
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "x.json").write_text('{"$schema": "ignored fixture"}')
    (tmp_path / "data.json").write_text('{"codes": []}')
    (tmp_path / "list.json").write_text("[]")
    (tmp_path / "a.json").write_text(json.dumps({"$schema": DRAFT, "$id": f"{ID_BASE}a.json", "type": "integer"}))
    loaded = Contracts.load(tmp_path)
    assert list(loaded.schemas) == ["a.json"]
    assert loaded.errors("a.json", "x")
