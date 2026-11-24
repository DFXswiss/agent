"""The error shape of 03-api.md sections 1 and 11: `{"error": code, "detail"?}`, never exception or database text."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import psycopg
import pytest
from fastapi import Request
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException
from starlette.responses import StreamingResponse

from agent_hub.config import HubConfig
from agent_hub.errors import ERROR_STATUSES, HubError, error_body, error_bytes
from agent_hub.http.context import Context
from agent_hub.http.handlers import pointers
from agent_hub.http.routing import HubRouter
from tests.platform.contracts.harness import CONTRACTS, load_json
from tests.platform.hub.conftest import FixedClock, HubHarness, start_hub

SECRET_TEXT = "secret-value-7f3a"
router = HubRouter()


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    count: int


@router.post("/v1/test/refuse", auth="anon", right="anon")
async def refuse() -> None:
    raise HubError("origin_seq_gap", detail={"expected": 17}, headers={"X-Test": "1"})


@router.post("/v1/test/crash", auth="anon", right="anon")
async def crash() -> None:
    raise RuntimeError(SECRET_TEXT)


@router.post("/v1/test/database", auth="anon", right="anon")
async def database(context: Context) -> None:
    with context.pool.connection() as connection:
        connection.execute(f"SELECT * FROM hub.no_such_table_{SECRET_TEXT.replace('-', '_')}")


@router.post("/v1/test/model", auth="anon", right="anon")
async def model(item: Item) -> dict[str, Any]:
    return item.model_dump()


@router.get("/v1/test/query", auth="anon", right="anon")
async def query(limit: int) -> dict[str, Any]:
    return {"limit": limit}


@router.post("/v1/test/http", auth="anon", right="anon")
async def http_exception(request: Request) -> None:
    raise HTTPException(int((await request.body()) or b"400"))


@router.get("/v1/test/late-crash", auth="anon", right="anon")
async def late_crash_read() -> Any:
    async def chunks() -> Any:
        yield b"partial"
        raise RuntimeError(SECRET_TEXT)

    return StreamingResponse(chunks())


@router.post("/v1/test/late-crash", auth="anon", right="anon")
async def late_crash() -> Any:
    async def chunks() -> Any:
        yield b"partial"
        raise RuntimeError(SECRET_TEXT)

    return StreamingResponse(chunks())


@pytest.fixture
def errors_hub(hub_config: HubConfig, hub_pool: ConnectionPool[Any], clock: FixedClock) -> Iterator[HubHarness]:
    yield from start_hub(hub_config, hub_pool, clock, routers=[("test", router)])


def test_error_codes_equal_the_contract() -> None:
    document = load_json(CONTRACTS / "enums" / "error-codes.json")
    expected = {row["code"]: tuple(row["statuses"]) for row in document["codes"] if row["origin"] == "hub"}
    assert dict(ERROR_STATUSES) == expected


def test_hub_error_rules() -> None:
    assert HubError("size_exceeded").status == 400
    assert HubError("size_exceeded", status=413).status == 413
    with pytest.raises(ValueError, match="closed list"):
        HubError("no_such_code")
    with pytest.raises(ValueError, match="status is not allowed"):
        HubError("forbidden", status=404)
    with pytest.raises(ValueError, match="detail"):
        HubError("forbidden", detail={"message": "x"})
    error = HubError("id_conflict", detail={"pointers": ["/a"]}, headers={"Retry-After": "1"})
    assert (error.code, error.status, error.detail, error.headers) == (
        "id_conflict",
        409,
        {"pointers": ["/a"]},
        {"Retry-After": "1"},
    )
    assert error_body("not_found") == {"error": "not_found"}
    assert json.loads(error_bytes("seq_conflict", {"expected_seq": 3})) == {
        "error": "seq_conflict",
        "detail": {"expected_seq": 3},
    }


def test_hub_error_from_a_route(errors_hub: HubHarness) -> None:
    response = errors_hub.anon.post("/v1/test/refuse")
    assert response.status_code == 409
    assert response.json() == {"error": "origin_seq_gap", "detail": {"expected": 17}}
    assert response.headers["x-test"] == "1"


def assert_no_secret(response: httpx.Response) -> None:
    assert SECRET_TEXT not in response.text
    assert SECRET_TEXT.replace("-", "_") not in response.text


def test_exception_text_never_leaves(errors_hub: HubHarness, caplog: pytest.LogCaptureFixture) -> None:
    response = errors_hub.anon.post("/v1/test/crash")
    assert response.status_code == 503
    assert response.json() == {"error": "unavailable"}
    assert_no_secret(response)
    assert "the route failed" in caplog.text


def test_database_text_never_leaves(errors_hub: HubHarness) -> None:
    response = errors_hub.anon.post("/v1/test/database")
    assert response.status_code == 503
    assert response.json() == {"error": "unavailable"}
    assert_no_secret(response)


def test_a_crash_of_a_signed_request_is_signed(errors_hub: HubHarness) -> None:
    response = errors_hub.root.post("/v1/test/crash")
    assert response.status_code == 503
    assert response.extensions["hub_signature"] is True


def test_a_crash_after_the_response_started(errors_hub: HubHarness, caplog: pytest.LogCaptureFixture) -> None:
    # Nothing can be answered any more: the response stays incomplete and the failure is logged.
    response = errors_hub.anon.get("/v1/test/late-crash")
    assert response.status_code == 200
    assert_no_secret(response)
    assert "the route failed" in caplog.text


def test_a_write_that_crashes_after_its_first_chunk_is_refused(
    errors_hub: HubHarness, caplog: pytest.LogCaptureFixture
) -> None:
    # A write is held until it is settled (ADR-063): its first chunk never leaves, the client gets 503.
    response = errors_hub.anon.post("/v1/test/late-crash")
    assert response.status_code == 503
    assert response.json() == {"error": "unavailable"}
    assert_no_secret(response)
    assert "the route failed" in caplog.text


def test_validation_errors_carry_pointers_never_values(errors_hub: HubHarness) -> None:
    response = errors_hub.anon.post("/v1/test/model", json={"name": SECRET_TEXT, "count": "3", "extra": 1})
    assert response.status_code == 400
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/count", ""]}}
    assert_no_secret(response)
    assert errors_hub.anon.post("/v1/test/model", json={"name": "a", "count": 3}).json() == {"name": "a", "count": 3}


def test_malformed_json(errors_hub: HubHarness) -> None:
    body = b"{" + SECRET_TEXT.encode()
    response = errors_hub.anon.post("/v1/test/model", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json() == {"error": "request_invalid"}
    # Without a JSON content type the body is not parsed at all: the whole body is the offending field.
    response = errors_hub.anon.post("/v1/test/model", content=body)
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": [""]}}


def test_query_validation(errors_hub: HubHarness) -> None:
    response = errors_hub.anon.get("/v1/test/query", params={"limit": "x"})
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/limit"]}}


def test_unknown_routes_and_methods(errors_hub: HubHarness) -> None:
    assert errors_hub.anon.post("/v1/unknown").json() == {"error": "not_found"}
    response = errors_hub.anon.request("PATCH", "/v1/test/refuse")
    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}


@pytest.mark.parametrize(("status", "code", "sent"), [(405, "not_found", 404), (418, "request_invalid", 400)])
def test_http_exceptions(errors_hub: HubHarness, status: int, code: str, sent: int) -> None:
    response = errors_hub.anon.post("/v1/test/http", content=str(status).encode())
    assert response.status_code == sent
    assert response.json() == {"error": code}


def test_http_server_error(errors_hub: HubHarness) -> None:
    response = errors_hub.anon.post("/v1/test/http", content=b"500")
    assert response.status_code == 503
    assert response.json() == {"error": "unavailable"}


def test_pointers() -> None:
    errors = [
        {"type": "missing", "loc": ("body", "params", "repo")},
        {"type": "missing", "loc": ("body", "a/b~c")},
        {"type": "missing", "loc": ("body", "params", "repo")},
        {"type": "json_invalid", "loc": ("body", 3)},
        {"type": "missing", "loc": ("body",)},
        {"type": "missing", "loc": ("body", "x" * 300)},
        {"type": "extra_forbidden", "loc": ("body", "params", "chosen-by-client")},
        "not a dict",
    ]
    assert pointers(errors) == ["/params/repo", "/a~1b~0c", "", "/params"]
    assert "chosen-by-client" not in str(pointers(errors))
    assert len(pointers([{"type": "missing", "loc": ("body", str(n))} for n in range(100)])) == 64


def test_writer_errors_are_not_psycopg_text() -> None:
    # The pipeline answers every exception class the same way; psycopg errors are exceptions like any other.
    assert issubclass(psycopg.Error, Exception)
