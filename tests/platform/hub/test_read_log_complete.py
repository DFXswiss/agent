"""U-HUB-readlog (completeness, R-8.9): every authenticated `GET` route of the hub - as the app discovers them, so every
later package's route is checked by this test - declares a `read_resource` (the one of `contracts/api/INDEX.json`) and
leaves a `read.<resource>` audit row with the caller as actor, written before the answer left the hub, whatever the
call's outcome (03-api.md section 1: "every authenticated GET appends an audit row"; ADR-063 section 3). A streaming
route may declare its read (the browser live channel `read.stream`, ADR-063); the list of streaming routes is pinned
here."""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi.routing import APIRoute

from agent_hub.http.routing import API_PREFIX, route_spec
from tests.platform.contracts.harness import CONTRACTS
from tests.platform.hub.agents.support import agent_client
from tests.platform.hub.conftest import HubHarness, SignedClient
from tests.platform.hub.orgs.support import new_id

STREAMING_GETS = {"/v1/stream"}
_PARAM_RE = re.compile(r"\{([a-z_]+)\}")


def authenticated_gets(hub: HubHarness) -> list[tuple[str, Any]]:
    """`(path, spec)` of every authenticated `GET` route under `/v1/`."""
    found = []
    for route in hub.app.router.routes:
        spec = route_spec(route)
        if not isinstance(route, APIRoute) or spec is None or "GET" not in (route.methods or set()):
            continue
        if route.path.startswith(API_PREFIX) and spec.auth != "anon":
            found.append((route.path, spec))
    return sorted(found, key=lambda pair: pair[0])


def index_resources() -> dict[str, str | None]:
    rows = json.loads((CONTRACTS / "api" / "INDEX.json").read_text(encoding="utf-8"))["endpoints"]
    return {row["path"]: row["read_resource"] for row in rows if row["method"] == "GET"}


def client_for(hub: HubHarness, auth: str) -> SignedClient:
    if auth == "agent":
        return agent_client(hub)
    if auth == "device":
        return hub.device()
    if auth == "root":
        return hub.root
    return hub.browser()


def test_every_authenticated_get_declares_its_read_resource(hub: HubHarness) -> None:
    routes = authenticated_gets(hub)
    assert len(routes) > 30
    index = index_resources()
    streaming = {path for path, spec in routes if spec.streaming}
    assert streaming == STREAMING_GETS
    for path, spec in routes:
        assert spec.streaming or spec.read_resource is not None, path
        assert index[path] == spec.read_resource, path
    assert [spec.read_resource for path, spec in routes if path == "/v1/stream"] == ["stream"]


def test_every_authenticated_get_writes_its_read_row(hub: HubHarness) -> None:
    expected: list[tuple[str, str, str | None]] = []
    for path, spec in authenticated_gets(hub):
        if spec.streaming:
            continue
        client = client_for(hub, spec.auth)
        key = client.key
        assert key is not None
        concrete = _PARAM_RE.sub(lambda _match: new_id(), path)
        response = client.get(concrete, extensions={"contract": False})
        assert response.status_code < 500, (path, response.status_code)
        actor = key.principal.principal_id
        expected.append((f"read.{spec.read_resource}", key.principal.actor_kind, None if actor is None else str(actor)))
    with hub.pool.connection() as connection:
        rows = connection.execute(
            "SELECT action, actor_kind, actor_id FROM hub.audit WHERE action LIKE 'read.%' ORDER BY seq"
        ).fetchall()
    written = [(str(action), str(kind), None if actor is None else str(actor)) for action, kind, actor in rows]
    assert written == expected
