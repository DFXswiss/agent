"""U-HUB-head (HOT-19, RFC 9110 section 9.3.2): every route that answers `GET` answers `HEAD` - as the app discovers
them, so every later package's route is checked by this test - with the status and headers of its `GET`
(`Content-Length`, `Cache-Control`, the security headers) and no body; rights, rate limit and read log as for the
`GET`. The router registers `HEAD` with every `GET`; the pipeline ends the route after its response start."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute

from agent_hub.agents.scope import agent_allowed
from agent_hub.http.pipeline import HeadAnswered, contains
from agent_hub.http.routing import route_spec
from tests.platform.contracts.harness import CONTRACTS
from tests.platform.hub.conftest import HubHarness, SignedClient
from tests.platform.hub.test_read_log_complete import client_for

# Response signatures cover the body, nonce and time of each answer; they differ between any two answers.
PER_ANSWER = ("agent-",)


def get_routes(hub: HubHarness) -> list[tuple[str, Any]]:
    """`(path, spec)` of every declared `GET` route without path parameters."""
    found = []
    for route in hub.app.router.routes:
        spec = route_spec(route)
        if not isinstance(route, APIRoute) or spec is None or "GET" not in (route.methods or set()):
            continue
        if "{" not in route.path and not required_queries("GET", route.path):
            found.append((route.path, spec))
    return sorted(found, key=lambda pair: pair[0])


def required_queries(method: str, path: str) -> list[str]:
    """The query parameters the contract of `method path` requires (a call without them breaks the contract)."""
    rows = json.loads((CONTRACTS / "api" / "INDEX.json").read_text(encoding="utf-8"))["endpoints"]
    for row in rows:
        if (row["method"], row["path"]) == (method, path) and row.get("request_kind") == "query":
            schema = json.loads((CONTRACTS / row["request"]).read_text(encoding="utf-8"))
            return list(schema.get("required", []))
    return []


def comparable(response: httpx.Response) -> tuple[int, dict[str, str]]:
    headers = {name: value for name, value in response.headers.items() if not name.startswith(PER_ANSWER)}
    return response.status_code, headers


def client(hub: HubHarness, auth: str) -> SignedClient:
    return hub.anon if auth in ("anon", "anon_or_browser", "device_enroll") else client_for(hub, auth)


def test_every_get_route_takes_head(hub: HubHarness) -> None:
    routes = [route for route in hub.app.router.routes if isinstance(route, APIRoute) and route_spec(route)]
    gets = [route for route in routes if "GET" in (route.methods or set())]
    assert len(gets) > 30
    assert all("HEAD" in (route.methods or set()) for route in gets)
    assert all("HEAD" not in (route.methods or set()) for route in routes if route not in gets)


def test_head_answers_like_get_on_every_route_without_parameters(hub: HubHarness) -> None:
    routes = get_routes(hub)
    assert len(routes) > 20
    changing: list[str] = []
    for path, spec in routes:
        if spec.streaming:
            continue
        hub.clock.advance(61)  # a fresh minute of the anonymous limit for every pair
        caller = client(hub, spec.auth)
        got = caller.get(path)
        head = caller.request("HEAD", path)
        again = caller.get(path)
        assert head.content == b"", path
        if comparable(got) == comparable(again):
            assert comparable(head) == comparable(got), path
        else:
            # the answer changes with every call (a read that lists the audit it just wrote): same status and headers
            # except the length
            unsized = [{k: v for k, v in comparable(r)[1].items() if k != "content-length"} for r in (head, got)]
            assert (head.status_code, unsized[0]) == (got.status_code, unsized[1]), path
            changing.append(path)
    assert changing == ["/v1/me/audit"]
    assert hub.anon.request("HEAD", "/").headers["content-length"] == hub.anon.get("/").headers["content-length"]


def test_head_without_a_signature_is_refused_like_get(hub: HubHarness) -> None:
    got, head = hub.anon.get("/v1/me"), hub.anon.request("HEAD", "/v1/me")
    assert comparable(head) == comparable(got)
    assert got.status_code == 401
    assert head.content == b""


def test_head_takes_the_anonymous_limit_like_get(hub: HubHarness) -> None:
    hub.clock.advance(61)
    answers = [hub.anon.request("HEAD", "/v1/health").status_code for _ in range(31)]
    assert answers[:30] == [200] * 30
    assert answers[30] == 429
    assert hub.anon.get("/v1/health").status_code == 429


def test_an_unknown_path_answers_head_like_get(hub: HubHarness) -> None:
    got, head = hub.anon.get("/no/such/page"), hub.anon.request("HEAD", "/no/such/page")
    assert comparable(head) == comparable(got)
    assert head.content == b""


@pytest.mark.parametrize(
    "error",
    [
        HeadAnswered(),
        RuntimeError("wrapped"),
        BaseExceptionGroup("group", [ValueError("other"), HeadAnswered()]),
    ],
)
def test_contains_finds_the_head_answer_however_it_was_wrapped(error: BaseException) -> None:
    if isinstance(error, RuntimeError):
        error.__cause__ = HeadAnswered()
    assert contains(error, HeadAnswered)


def test_contains_ends_on_a_cycle_without_the_kind() -> None:
    first, second = ValueError("first"), KeyError("second")
    first.__context__, second.__context__ = second, first
    assert not contains(first, HeadAnswered)


@pytest.mark.parametrize("state", ["confirmed", "active", "revoked"])
def test_an_agent_is_admitted_to_head_exactly_where_to_get(hub: HubHarness, state: str) -> None:
    for route in hub.app.router.routes:
        spec = route_spec(route)
        if isinstance(route, APIRoute) and spec is not None and "HEAD" in (route.methods or set()):
            assert agent_allowed("HEAD", route.path, spec.auth, state) == agent_allowed(
                "GET", route.path, spec.auth, state
            ), route.path
