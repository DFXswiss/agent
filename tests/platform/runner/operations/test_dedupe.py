"""U-RUN-operations: dedupe tags of the pipeline equal the vectors of `contracts/vectors/dedupe.json` (04-crypto.md
section 4.10, 03-api.md section 9)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_runner.crypto.dedupe import dedupe_tag
from agent_runner.crypto.errors import CryptoError
from agent_runner.encoding import b64u_decode
from agent_runner.operations.dedupe import dedupe_slot, fields_for
from agent_runner.operations.extensions import NEEDS_PRECHECK

CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"
VECTORS: dict[str, Any] = json.loads((CONTRACTS / "vectors" / "dedupe.json").read_text(encoding="utf-8"))
DK = b64u_decode(VECTORS["dedupe_key"])
WK = b64u_decode(VECTORS["workgroup_key"])
TASK = "01930004-0000-7000-8000-000000000001"


def split(vector: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Vector params -> (effective params, requesting session, pre-check dedupe fields)."""
    params = dict(vector["params"])
    session = params.pop("session", "01930005-0000-7000-8000-000000000001")
    extra = {name: params.pop(name) for name in NEEDS_PRECHECK.get(vector["type"], ())}
    return params, session, extra


TYPED = [vector for vector in VECTORS["tags"] if "params" in vector]
KEYED = [vector for vector in VECTORS["tags"] if "idempotency_key" in vector]


@pytest.mark.parametrize("vector", TYPED, ids=[v["type"] for v in TYPED])
def test_field_sets_and_tags_match_the_vectors(vector: dict[str, Any]) -> None:
    params, session, extra = split(vector)
    fields = fields_for(vector["type"], params, session_id=session, task_id=TASK, idempotency_key=None, extra=extra)
    assert fields == vector["fields"]
    assert fields is not None
    assert dedupe_tag(DK, vector["type"], fields) == vector["tag"]
    slot = dedupe_slot(
        vector["type"],
        params,
        session_id=session,
        task_id=TASK,
        idempotency_key=None,
        extra=extra,
        workgroup_key=WK,
        epoch=3,
    )
    assert slot == {"epoch": 3, "tag": vector["tag"]}


def test_every_type_with_dedupe_fields_has_a_vector() -> None:
    dedupe = json.loads((CONTRACTS / "ops" / "dedupe-fields.json").read_text(encoding="utf-8"))
    assert {vector["type"] for vector in TYPED} == set(dedupe["types"])


@pytest.mark.parametrize("vector", KEYED)
def test_an_idempotency_key_replaces_the_fields(vector: dict[str, Any]) -> None:
    fields = fields_for(
        vector["type"],
        {"anything": "ignored"},
        session_id="01930005-0000-7000-8000-000000000001",
        task_id=vector["task"],
        idempotency_key=vector["idempotency_key"],
        extra={},
    )
    assert fields == vector["fields"]
    slot = dedupe_slot(
        vector["type"],
        {},
        session_id="x",
        task_id=vector["task"],
        idempotency_key=vector["idempotency_key"],
        extra={},
        workgroup_key=WK,
        epoch=1,
    )
    assert slot == {"epoch": 1, "tag": vector["tag"]}


@pytest.mark.parametrize("op_type", ["github.list_repositories", "github.get_pull_request", "task.set_title"])
def test_types_without_dedupe_fields_have_no_slot(op_type: str) -> None:
    common: dict[str, Any] = {"session_id": "s", "task_id": TASK, "idempotency_key": None, "extra": {}}
    assert fields_for(op_type, {"title": "x"}, **common) is None
    assert dedupe_slot(op_type, {}, workgroup_key=WK, epoch=1, **common) is None
    # with a key every type has a slot
    keyed = {**common, "idempotency_key": "k"}
    assert fields_for(op_type, {}, **keyed) == {"idempotency_key": "k", "task": TASK}


def test_the_tag_depends_on_the_epoch_key() -> None:
    params = {"repo": "a/b", "number": 1, "body": "x"}
    common: dict[str, Any] = {"session_id": "s", "task_id": TASK, "idempotency_key": None, "extra": {}}
    one = dedupe_slot("github.comment", params, workgroup_key=WK, epoch=1, **common)
    two = dedupe_slot("github.comment", params, workgroup_key=bytes(32), epoch=2, **common)
    assert one is not None
    assert two is not None
    assert one["tag"] != two["tag"]


INVALID = [v for v in VECTORS["invalid"] if v["args"]["type"] != "task.set_title"]


@pytest.mark.parametrize("vector", INVALID, ids=[v["name"] for v in INVALID])
def test_invalid_vectors_are_refused(vector: dict[str, Any]) -> None:
    args = vector["args"]
    with pytest.raises(CryptoError):
        fields_for(args["type"], args["params"], session_id="s", task_id=TASK, idempotency_key=None, extra={})


def test_the_type_without_dedupe_vector_has_no_field_set_here() -> None:
    # crypto.dedupe refuses it (`header_invalid`); the pipeline never asks: such a type has no slot
    (vector,) = [v for v in VECTORS["invalid"] if v["args"]["type"] == "task.set_title"]
    args = vector["args"]
    assert (
        fields_for(args["type"], args["params"], session_id="s", task_id=TASK, idempotency_key=None, extra={}) is None
    )
