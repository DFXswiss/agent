"""CON-01: `contracts/api/INDEX.json` lists every HTTP endpoint of 03-api.md (sections 4, 5, 6.1, 6.2) and every
listed endpoint has its schemas (pending only for the owners named in `pending_owners`).

The specification tables are data in `spec/endpoints-<package>.json` (CTR-01 quotes 03-api.md; later contract
packages add their own file instead of editing this one, ADR-027 §4). INDEX.json must equal their union.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from tests.platform.contracts.harness import CONTRACTS, Contracts, load_json

SPEC_DIR = Path(__file__).resolve().parent / "spec"
SPEC_FILES = sorted(SPEC_DIR.glob("endpoints-*.json"))
SPECS: list[dict[str, Any]] = [load_json(path) for path in SPEC_FILES]
SPEC_ROWS = [row for spec in SPECS for row in spec["endpoints"]]
RIGHTS = {right for spec in SPECS for right in spec["rights"]}
AUTH = {auth for spec in SPECS for auth in spec["auth"]}
SECTIONS = {row["section"] for row in SPEC_ROWS}
ENTRY_KEYS = [
    "method",
    "path",
    "name",
    "section",
    "auth",
    "rights",
    "path_params",
    "request_kind",
    "request",
    "response",
    "read_resource",
    "streaming",
    "owner",
]
# 03-api.md section 3 (rights vocabulary): the CTR-01 table must contain at least these.
SPEC_RIGHTS = (
    "anon user org:member org:admin wg:read wg:write task:read task:write platform:inviter device:any device device:wg"
    " device:session"
)


def endpoints(index: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = index["endpoints"]
    return rows


def test_spec_tables() -> None:
    assert SPEC_DIR / "endpoints-ctr01.json" in SPEC_FILES
    for spec in SPECS:
        assert set(spec) == {"description", "rights", "auth", "endpoints"}
        for row in spec["endpoints"]:
            assert set(row) == {"method", "path", "rights", "section"}
            assert set(row["rights"]) <= set(spec["rights"]), row
    assert set(SPEC_RIGHTS.split()) <= RIGHTS
    ctr01 = load_json(SPEC_DIR / "endpoints-ctr01.json")
    sections = {row["section"] for row in ctr01["endpoints"]}
    assert sections == {f"4.{n}" for n in range(1, 12)} | {"5", "6.1", "6.2"}
    assert sum(1 for row in ctr01["endpoints"] if row["section"].startswith("4.")) == 62


def test_index_matches_the_specification(index: dict[str, Any]) -> None:
    listed = [(e["method"], e["path"], tuple(e["rights"]), e["section"]) for e in endpoints(index)]
    spec = [(row["method"], row["path"], tuple(row["rights"]), row["section"]) for row in SPEC_ROWS]
    assert sorted(listed) == sorted(spec)
    assert len({(m, p) for m, p, _, _ in listed}) == len(listed)


def test_entries_are_well_formed(index: dict[str, Any]) -> None:
    assert set(index) == {"description", "error_response", "pending_owners", "endpoints"}
    assert index["error_response"] == "api/error.response.json"
    names = [e["name"] for e in endpoints(index)]
    assert len(set(names)) == len(names)
    for e in endpoints(index):
        assert list(e) == ENTRY_KEYS, e["path"]
        assert re.fullmatch(r"[a-z]+(-[a-z]+)*", e["name"]), e["name"]
        assert e["auth"] in AUTH, e
        assert set(e["rights"]) <= RIGHTS, e
        assert e["section"] in SECTIONS, e
        assert isinstance(e["streaming"], bool), e
        if e["streaming"]:
            assert e["method"] == "GET", e
            assert e["request_kind"] == "stream", e
        else:
            assert e["request_kind"] == ("body" if e["method"] in ("POST", "PUT") else "query"), e
        assert e["name"].startswith("device-") == e["path"].startswith("/v1/device/"), e
        if e["auth"] == "anon":
            assert e["rights"] == ["anon"], e
    # ADR-101: the two ends of a VNC tunnel are WebSockets as well
    tunnels = {"/v1/vnc/{request}", "/v1/device/vnc/{request}"}
    assert {e["path"] for e in endpoints(index) if e["streaming"]} == {"/v1/stream", "/v1/device/ws"} | tunnels


def test_every_entry_has_schemas(index: dict[str, Any], contracts: Contracts) -> None:
    pending = set(index["pending_owners"])
    for e in endpoints(index):
        if e["request"] is None or e["response"] is None:
            assert e["owner"] in pending, e
            continue
        if not e["streaming"]:
            assert e["request"] == f"api/{e['name']}.request.json", e
            assert e["response"] == f"api/{e['name']}.response.json", e
        assert e["request"] in contracts.schemas, e
        assert e["response"] in contracts.schemas, e


def test_pending_owners_still_owe_something(index: dict[str, Any]) -> None:
    """An owner leaves `pending_owners` (in INDEX.json, which it edits anyway) once it has delivered every schema."""
    for owner in index["pending_owners"]:
        assert re.fullmatch(r"[A-Z]{3}-[0-9]{2}", owner)
        assert any(e["owner"] == owner and e["request"] is None for e in endpoints(index)), owner
    assert "CTR-01" not in index["pending_owners"]


def test_every_api_schema_is_listed(index: dict[str, Any]) -> None:
    referenced = {e[part] for e in endpoints(index) for part in ("request", "response") if e[part]}
    referenced |= {index["error_response"]}
    on_disk = {p.relative_to(CONTRACTS).as_posix() for p in (CONTRACTS / "api").glob("*.json")}
    libraries = {rel for rel in on_disk if rel.endswith("objects.json")}
    assert on_disk - libraries - {"api/INDEX.json"} <= referenced
    assert {rel for rel in referenced if rel.startswith("api/")} <= on_disk


def test_path_parameters(index: dict[str, Any], contracts: Contracts) -> None:
    defs = contracts.schemas["common.json"]["$defs"]
    for e in endpoints(index):
        assert re.findall(r"\{([a-z_]+)\}", e["path"]) == list(e["path_params"]), e
        for name in e["path_params"].values():
            assert name in defs, e


def test_read_resources(index: dict[str, Any]) -> None:
    """Every authenticated, non-streaming GET appends `read.<resource>` (03-api.md section 1); a streaming GET may
    (the browser live channel, ADR-063 section 3)."""
    resources = [e["read_resource"] for e in endpoints(index) if e["read_resource"]]
    assert len(set(resources)) == len(resources)
    for e in endpoints(index):
        authenticated_get = e["method"] == "GET" and e["auth"] != "anon"
        if e["read_resource"] is None:
            assert not authenticated_get or e["streaming"], e
        else:
            assert authenticated_get, e
        if e["read_resource"]:
            assert re.fullmatch(r"[a-z_]+(\.[a-z_]+)*", e["read_resource"]), e
            assert e["read_resource"].startswith("device.") == e["path"].startswith("/v1/device/"), e


def text_schemas(contracts: Contracts, rel: str, schema: dict[str, Any]) -> list[dict[str, Any]]:
    """The text schemas a query parameter allows (through `$ref`, `anyOf` and, for repeatable ones, `items`)."""
    if "$ref" in schema and "type" not in schema:
        target = contracts.resolve(rel, schema["$ref"])
        assert isinstance(target, dict)
        return text_schemas(contracts, rel, target)
    if "anyOf" in schema:
        return [found for alt in schema["anyOf"] for found in text_schemas(contracts, rel, alt)]
    if schema.get("type") == "array":
        return text_schemas(contracts, rel, schema["items"])
    return [schema]


@pytest.mark.parametrize(
    ("schema", "types"),
    [
        ({"type": "string"}, ["string"]),
        ({"type": "array", "items": {"type": "string"}}, ["string"]),
        ({"anyOf": [{"$ref": "../common.json#/$defs/uuid"}, {"type": "integer"}]}, ["string", "integer"]),
    ],
)
def test_text_schemas(contracts: Contracts, schema: dict[str, Any], types: list[str]) -> None:
    assert [found["type"] for found in text_schemas(contracts, "api/me.request.json", schema)] == types


def test_query_requests_are_text(index: dict[str, Any], contracts: Contracts) -> None:
    """Query parameters arrive as text: every parameter is a string (or, if repeatable, an array of strings)."""
    for e in endpoints(index):
        if e["request_kind"] != "query" or e["request"] is None:
            continue
        schema = contracts.schemas[e["request"]]
        assert schema["type"] == "object", e
        for name, prop in schema["properties"].items():
            for text in text_schemas(contracts, e["request"], prop):
                assert text["type"] == "string", (e["path"], name)
