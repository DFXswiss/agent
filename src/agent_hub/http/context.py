"""The per-process context of the hub app and the request-scoped values the pipeline sets for the routes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any

import psycopg
from fastapi import Depends
from psycopg_pool import ConnectionPool
from starlette.requests import HTTPConnection

from agent_core.signing import HubSigner
from agent_hub.auth.keys import ANONYMOUS, KeyResolver, Principal
from agent_hub.clock import Clock
from agent_hub.config import HubConfig
from agent_hub.http.limits import RateLimiter
from agent_hub.http.readlog import ReadLog

PRINCIPAL_KEY = "agent_hub.principal"
SIGNED_KEY = "agent_hub.signed"
PENDING_KEY = "agent_hub.pending"


@dataclass(frozen=True)
class HubContext:
    """Everything a route, job or resolver of one hub process needs; `app.state.hub`."""

    config: HubConfig
    pool: ConnectionPool[psycopg.Connection[Any]]
    clock: Clock
    signer: HubSigner
    resolvers: Mapping[str, KeyResolver]
    limiter: RateLimiter
    read_log: ReadLog


def hub_context(request: HTTPConnection) -> HubContext:
    """FastAPI dependency: the context of the app that serves the request."""
    context: HubContext = request.app.state.hub
    return context


def current_principal(request: HTTPConnection) -> Principal:
    """FastAPI dependency: who sent the request (set by the pipeline; anonymous when unsigned)."""
    principal: Principal = request.scope.get(PRINCIPAL_KEY, ANONYMOUS)
    return principal


Context = Annotated[HubContext, Depends(hub_context)]
CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
