"""U-HUB-agents (route parity, 11-agent-access.md section 6, ADR-056 section 4): an active agent is admitted on every
route a browser session of its user is admitted to - checked against every route the app registers, so a route added
later is covered without a change here - except exactly the WebAuthn ceremonies; the confirmation of the main
orchestrator is admitted and decided by its handler (KOR-62: an agent of an account without a passkey confirms itself,
one of an account with a passkey gets `403 forbidden`, concept idea 34); a confirmed agent may only claim.
Through the real pipeline an agent gets the same answer as a browser session of a user in the same situation."""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi.routing import APIRoute, APIWebSocketRoute

from agent_hub.agents import scope
from agent_hub.auth.keys import Principal
from agent_hub.errors import HubError
from agent_hub.http.pipeline import UNROUTED, check_auth
from agent_hub.http.routing import AUTH_CLASSES, RouteSpec, route_spec
from tests.platform.hub.agents.support import stored_agent
from tests.platform.hub.conftest import HubHarness, uuid7

WEBAUTHN = {
    ("POST", "/v1/passkeys/register/begin"),
    ("POST", "/v1/passkeys/register/finish"),
    ("POST", "/v1/me/recovery/begin"),
}
EXCEPTED = WEBAUTHN
BROWSER_CLASSES = {"browser", "anon_or_browser", "anon"}
NO_CONTRACT = {"contract": False}


def routes(hub: HubHarness) -> list[tuple[str, str, RouteSpec]]:
    """`(method, path, spec)` of every registered route with a spec (HTTP and WebSocket)."""
    found = []
    for route in hub.app.router.routes:
        spec = route_spec(route)
        if spec is None:
            continue
        if isinstance(route, APIWebSocketRoute):
            found.append(("GET", route.path, spec))
        elif isinstance(route, APIRoute):
            found.extend((method, route.path, spec) for method in sorted(route.methods or ()))
    return found


def principal(state: str) -> Principal:
    return Principal(
        kind="agent",
        key_id="agent." + "0" * 32,
        principal_id=uuid.uuid4(),
        actor_kind="user",
        attributes={"agent_id": uuid.uuid4(), "state": state},
    )


def admitted(spec: RouteSpec, who: Principal, method: str, path: str | None) -> bool:
    try:
        check_auth(spec, who, signed=who.kind != "anonymous", method=method, path=path)
    except HubError:
        return False
    return True


def test_the_exceptions_are_exactly_the_webauthn_ceremonies(hub: HubHarness) -> None:
    assert set(scope.EXCEPTIONS) == EXCEPTED
    registered = {(method, path): spec.auth for method, path, spec in routes(hub)}
    for key in EXCEPTED:
        assert registered[key] in BROWSER_CLASSES | {"browser"}, key
        assert scope.EXCEPTIONS[key]  # every exception names its reason
    # KOR-62: the confirmation of a main orchestrator is no exception any more; its handler decides
    assert ("POST", "/v1/me/agents") not in scope.EXCEPTIONS


def test_every_route_a_browser_passes_admits_an_active_agent(hub: HubHarness) -> None:
    found = routes(hub)
    assert len(found) > 80
    assert {spec.auth for _m, _p, spec in found} <= AUTH_CLASSES
    session = Principal(kind="session", key_id="session." + "0" * 32, principal_id=uuid.uuid4(), actor_kind="user")
    active, confirmed = principal("active"), principal("confirmed")
    checked = 0
    for method, path, spec in found:
        browser = admitted(spec, session, method, path)
        agent = admitted(spec, active, method, path)
        if (method, path) in EXCEPTED:
            assert browser, path
            assert not agent, path
        elif spec.auth == "agent":
            assert agent, path
            assert not browser, path
        else:
            assert agent == browser, (method, path, spec.auth)
        assert scope.agent_allowed(method, path, spec.auth, "active") == agent
        assert admitted(spec, confirmed, method, path) == ((method, path) in scope.CONFIRMED_ROUTES), path
        checked += browser
    assert checked > 60
    # a request that matches no route: the agent gets the browser's 404 later, a confirmed agent nothing
    assert admitted(UNROUTED, active, "GET", None)
    assert not admitted(UNROUTED, confirmed, "GET", None)
    assert not scope.agent_allowed("GET", "/v1/me", "browser", "revoked")


PATH_PARAM = re.compile(r"\{([a-z_]+)\}")


def fill(path: str, hub: HubHarness) -> str:
    return PATH_PARAM.sub(lambda _match: str(uuid7(hub.clock())), path)


def answer(response: Any) -> tuple[int, object]:
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
    return response.status_code, body.get("error") if isinstance(body, dict) else None


def test_an_agent_gets_the_answer_of_a_browser_session_on_every_browser_route(hub: HubHarness) -> None:
    """A fresh user's browser session and a fresh user's agent send the same request to every browser route (path
    IDs nobody knows, an empty JSON body): the status and error code are equal, except for the exceptions."""
    compared = 0
    for method, path, spec in routes(hub):
        if spec.auth != "browser" or spec.streaming:
            continue
        url = fill(path, hub)
        kwargs: dict[str, Any] = {"extensions": NO_CONTRACT}
        if method in ("POST", "PUT"):
            kwargs["json"] = {}
        browser = answer(hub.browser().request(method, url, **kwargs))
        agent = answer(stored_agent(hub).client(hub).request(method, url, **kwargs))
        if (method, path) in EXCEPTED:
            assert agent == (403, "forbidden"), path
            continue
        assert agent == browser, (method, path, browser, agent)
        compared += 1
    assert compared > 50
