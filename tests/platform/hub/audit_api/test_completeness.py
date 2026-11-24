"""Completeness of the write audit (KOR-40, ADR-063 section 1): every route with POST, PUT, PATCH or DELETE - as the app
discovers them, so every later package's route is checked without a list to maintain - leaves a committed audit row
for a refused request, in its own transaction after the route's rollback, and for a successful one.

Each route is called twice as the principal its authentication class needs: with a query parameter it does not declare
(the pipeline refuses it after the principal is known) and with a body that is no JSON (the route refuses it, or an
earlier check of rights or state does). A refusal must leave a row whose result is not `ok`; should a route accept the
broken body, the success must leave a row. The success side of every route is guarded in every test by the fixture
`every_write_audited` of the hub suite (`unaudited_since`), which is itself checked here."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from fastapi.routing import APIRoute

from agent_hub.http.routing import API_PREFIX, route_spec
from agent_hub.http.writelog import UNAUDITED, WRITE_METHODS
from tests.platform.hub.agents.support import agent_client
from tests.platform.hub.conftest import HubHarness, SignedClient, unaudited_since
from tests.platform.hub.orgs.support import new_id

_PARAM_RE = re.compile(r"\{([a-z_]+)\}")


def write_routes(hub: HubHarness) -> list[tuple[str, str, str]]:
    """`(method, path, auth class)` of every write route under `/v1/`."""
    found = []
    for route in hub.app.router.routes:
        spec = route_spec(route)
        if not isinstance(route, APIRoute) or spec is None or not route.path.startswith(API_PREFIX):
            continue
        for method in sorted((route.methods or set()) & WRITE_METHODS):
            found.append((method, route.path, spec.auth))
    return sorted(found)


def client_for(hub: HubHarness, auth: str) -> SignedClient:
    if auth in ("browser", "anon_or_browser"):
        return hub.browser()
    if auth in ("device", "device_enroll"):  # an enrollment is always signed (with the new device's key)
        return hub.device()
    if auth == "agent":
        return agent_client(hub)
    if auth == "root":
        return hub.root
    return hub.anon


def head(hub: HubHarness) -> int:
    with hub.pool.connection() as connection:
        row = connection.execute("SELECT COALESCE(max(seq), 0) FROM hub.audit").fetchone()
    assert row is not None
    return int(row[0])


def new_rows(hub: HubHarness, after: int) -> list[tuple[str, str, dict[str, Any]]]:
    with hub.pool.connection() as connection:
        return [
            (str(action), str(result), dict(detail))
            for action, result, detail in connection.execute(
                "SELECT action, result, detail FROM hub.audit WHERE seq > %s ORDER BY seq", (after,)
            )
        ]


def test_every_write_route_records_refusals_and_successes(hub: HubHarness) -> None:
    routes = write_routes(hub)
    assert len(routes) > 30
    unrecorded: list[str] = []
    for method, path, auth in routes:
        client = client_for(hub, auth)
        concrete = _PARAM_RE.sub(lambda _match: new_id(), path)
        before = head(hub)
        refused = client.request(method, concrete, params={"kor40_undeclared": "1"}, extensions={"contract": False})
        found = new_rows(hub, before)
        expected = ("request.refused", "rejected")
        if refused.status_code != 400 or [(a, r) for a, r, _d in found] != [expected] or found[0][2]["route"] != path:
            unrecorded.append(f"{method} {path} (undeclared query: {refused.status_code}, {found})")
        before = head(hub)
        broken = client.request(
            method,
            concrete,
            content=b"\x00 no json",
            headers={"Content-Type": "application/json"},
            extensions={"contract": False},
        )
        found = new_rows(hub, before)
        if broken.status_code >= 400:
            recorded = any(result != "ok" for _action, result, _detail in found)
        else:
            recorded = bool(found)
        if not recorded:
            unrecorded.append(f"{method} {path} (broken body: {broken.status_code}, {found})")
    assert unrecorded == []


def test_a_route_without_its_own_row_fails_the_suite() -> None:
    before = Counter(UNAUDITED)
    UNAUDITED["POST /v1/a/new/route"] += 1
    UNAUDITED["POST /v1/test/declared/by/a/test"] += 1
    try:
        # the fixture `every_write_audited` fails a test with exactly this result
        assert unaudited_since(before) == {"POST /v1/a/new/route": 1}
    finally:
        UNAUDITED.subtract({"POST /v1/a/new/route": 1, "POST /v1/test/declared/by/a/test": 1})
    assert unaudited_since(before) == {}
