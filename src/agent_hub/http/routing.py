"""Route declarations of the hub API (03-api.md sections 1 and 3, ADR-027 section 4).

Every route is declared through `HubRouter` with its authentication class (`auth`, the `auth` column of
`contracts/api/INDEX.json`), its right (`right`, 03-api.md section 3; the check of the right itself belongs to the
area's handler), the read-log resource of an authenticated `GET` (`read_resource`, written as `read.<resource>`;
required unless the response streams, allowed for a streaming one: ADR-063 section 3), the body limit and whether the
response streams. The request pipeline (`agent_hub.http.pipeline`) reads the declaration
before the route runs: it enforces the authentication class, the body limit, the rate limit and the read log, and it
signs every non-streaming response to a signed request. A route without a declaration cannot be registered with the
app (`agent_hub.app` refuses it).

The app factory adds the routes of every discovered router to the app as they are (no `include_router`, which would
wrap them); the declaration is an attribute of the route object. The app never publishes an OpenAPI document.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

from fastapi import APIRouter
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import BaseRoute, Match
from starlette.types import Scope

from agent_hub.audit.actions import AUDIT_ACTIONS
from agent_hub.config import LIMITS

Auth = Literal["anon", "browser", "anon_or_browser", "device", "device_enroll", "agent", "root"]
AUTH_CLASSES = frozenset(get_args(Auth))
# 03-api.md section 3 plus the endpoint-specific rights of contracts/api/INDEX.json.
RIGHTS = frozenset(
    {
        "anon",
        "user",
        "org:member",
        "org:admin",
        "wg:read",
        "wg:write",
        "task:read",
        "task:write",
        "platform:inviter",
        "device:any",
        "device",
        "device:wg",
        "device:org",
        "device:session",
        "device_eligible",
        "device_addressed",
        "invite_proof",
        "owner",
        "per_entry_type",
        "per_event_kind",
        "root",
        "agent:confirmed",
        "agent",
    }
)
SPEC_ATTRIBUTE = "agent_hub_spec"
API_PREFIX = "/v1/"
REQUEST_BODY_BYTES = LIMITS["request_body_bytes"]
EVENT_BATCH_BYTES = LIMITS["event_batch_bytes"]
_RESOURCE_RE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")


@dataclass(frozen=True)
class RouteSpec:
    """The declaration of one route."""

    auth: Auth
    rights: tuple[str, ...]
    read_resource: str | None
    max_body: int
    streaming: bool

    @property
    def signed(self) -> bool:
        """The route requires a verified signature (every class except `anon`, `anon_or_browser` and enrolment)."""
        return self.auth in ("browser", "device", "agent", "root")


def make_spec(
    method: str,
    path: str,
    *,
    auth: Auth,
    right: str | Sequence[str],
    read_resource: str | None,
    max_body: int,
    streaming: bool,
) -> RouteSpec:
    """Check a declaration; a broken one stops the import of the area (fail closed)."""
    rights = (right,) if isinstance(right, str) else tuple(right)
    if auth not in AUTH_CLASSES:
        raise ValueError(f"{method} {path}: unknown auth class")
    if not rights or not set(rights) <= RIGHTS:
        raise ValueError(f"{method} {path}: right must name rights of 03-api.md section 3")
    if not 0 <= max_body <= EVENT_BATCH_BYTES:
        raise ValueError(f"{method} {path}: max_body must be between 0 and {EVENT_BATCH_BYTES}")
    authenticated_get = method == "GET" and path.startswith(API_PREFIX) and auth != "anon"
    if read_resource is None:
        if authenticated_get and not streaming:
            raise ValueError(f"{method} {path}: an authenticated GET must declare read_resource")
    elif not authenticated_get:
        raise ValueError(f"{method} {path}: only an authenticated GET has a read_resource")
    elif _RESOURCE_RE.fullmatch(read_resource) is None or f"read.{read_resource}" not in AUDIT_ACTIONS:
        raise ValueError(f"{method} {path}: read.{read_resource} is not an audit action")
    return RouteSpec(auth=auth, rights=rights, read_resource=read_resource, max_body=max_body, streaming=streaming)


class HubRouter(APIRouter):
    """An `APIRouter` whose route decorators require the hub declaration.

    Usage in `agent_hub/<area>/routes.py`::

        router = HubRouter()

        @router.get("/v1/orgs/{org}", auth="browser", right="org:member", read_resource="org")
        async def get_org(org: str, principal: Principal = Depends(current_principal)) -> dict[str, Any]: ...
    """

    def hub_route(
        self,
        method: str,
        path: str,
        *,
        auth: Auth,
        right: str | Sequence[str],
        read_resource: str | None = None,
        max_body: int = REQUEST_BODY_BYTES,
        streaming: bool = False,
        **kwargs: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        spec = make_spec(
            method,
            path,
            auth=auth,
            right=right,
            read_resource=read_resource,
            max_body=max_body,
            streaming=streaming,
        )
        decorator = self.api_route(path, methods=[method], **kwargs)

        def register(endpoint: Callable[..., Any]) -> Callable[..., Any]:
            result = decorator(endpoint)
            setattr(self.routes[-1], SPEC_ATTRIBUTE, spec)
            return result

        return register

    def hub_websocket(
        self, path: str, *, auth: Auth, right: str | Sequence[str]
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """A WebSocket route; its upgrade request passes the same gate (never response-signed, 03-api.md section 1)."""
        spec = make_spec("GET", path, auth=auth, right=right, read_resource=None, max_body=0, streaming=True)
        decorator = self.websocket(path)

        def register(endpoint: Callable[..., Any]) -> Callable[..., Any]:
            result = decorator(endpoint)
            setattr(self.routes[-1], SPEC_ATTRIBUTE, spec)
            return result

        return register

    def get(self, path: str, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:  # type: ignore[override]
        return self.hub_route("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:  # type: ignore[override]
        return self.hub_route("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:  # type: ignore[override]
        return self.hub_route("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:  # type: ignore[override]
        return self.hub_route("DELETE", path, **kwargs)


def route_spec(route: BaseRoute) -> RouteSpec | None:
    """The declaration of a registered route, None for a route that was not declared through `HubRouter`."""
    spec = getattr(route, SPEC_ATTRIBUTE, None)
    return spec if isinstance(spec, RouteSpec) else None


@dataclass(frozen=True)
class RouteMatch:
    """The outcome of matching a request against the registered routes."""

    route: BaseRoute | None
    spec: RouteSpec | None
    path_params: dict[str, Any]


def match_route(routes: Iterable[BaseRoute], scope: Scope) -> RouteMatch:
    """The first route that fully matches path and method (Starlette's order), else no route."""
    for route in routes:
        if not isinstance(route, APIRoute | APIWebSocketRoute):
            continue
        match, child = route.matches(scope)
        if match is Match.FULL:
            return RouteMatch(route=route, spec=route_spec(route), path_params=dict(child.get("path_params", {})))
    return RouteMatch(route=None, spec=None, path_params={})
