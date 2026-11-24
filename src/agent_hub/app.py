"""The hub app factory (00-overview.md section 5.1, ADR-027 section 4).

`create_app(config)` builds the FastAPI app: the routers of every area (`agent_hub/<area>/routes.py:router`, each a
`HubRouter`; a route without declaration or a method and path registered twice stops the build), the web build as
the last routes (`agent_hub.static_routes`), the key resolvers of the signature gate (`<area>/keys.py:RESOLVERS`), the
jobs (`<area>/jobs.py:JOBS`) and the signal subscribers (`<area>/signals.py:SUBSCRIBERS`). The middleware stack is
`SecurityHeaders` → `HubPipeline` → routes. Only `/v1/…` (API), `/assets/…` and the SPA routes exist; none of the
legacy routes (`/api/…`, `/sync/…`, `/auth/…`, `/pair…`, `/health`).

The lifespan opens the writer pool (unless one is given), starts the read-log writer, the live service of the process
(`agent_hub.stream.service`, `app.state.stream`: browser streams, device sockets, the LISTEN/NOTIFY bus) and the job
runner, and on shutdown closes the live channels, stops the jobs, writes the remaining read-log rows and closes the
pool it opened.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from fastapi import FastAPI
from psycopg_pool import ConnectionPool
from starlette.routing import BaseRoute

from agent_hub import __version__, signals, static_routes
from agent_hub.auth.keys import KeyResolver, check_resolvers
from agent_hub.clock import Clock, system_clock
from agent_hub.config import HubConfig
from agent_hub.db.pool import open_pool
from agent_hub.http import handlers
from agent_hub.http.context import HubContext
from agent_hub.http.headers import SecurityHeaders
from agent_hub.http.limits import RateLimiter
from agent_hub.http.pipeline import HubPipeline
from agent_hub.http.readlog import READ_WINDOW_SECONDS, ReadLog
from agent_hub.http.routing import HubRouter, route_spec
from agent_hub.jobs import Job, JobRunner, check_jobs
from agent_hub.registry import JOBS, KEY_RESOLVERS, ROUTERS, RegistryError, concat_sequences, discover, merge_mappings
from agent_hub.stream.service import StreamService

Pool = ConnectionPool[psycopg.Connection[Any]]
# How long the start waits for the first database connection before it fails (uvicorn then exits).
POOL_OPEN_TIMEOUT_SECONDS = 30.0


def discovered_routers() -> list[tuple[str, Any]]:
    """`(area, router)` of every area with a `routes.py`."""
    return discover(ROUTERS)


def collect_routes(routers: Iterable[tuple[str, Any]]) -> list[BaseRoute]:
    """The routes of the routers in order, followed by the web build; refuses undeclared and duplicate routes."""
    routes: list[BaseRoute] = []
    seen: set[tuple[str, str]] = set()
    for area, router in [*routers, ("static_routes", static_routes.router)]:
        if not isinstance(router, HubRouter):
            raise RegistryError(f"{area}: router must be a HubRouter")
        for route in router.routes:
            if route_spec(route) is None:
                raise RegistryError(f"{area}: route {getattr(route, 'path', '?')} has no hub declaration")
            path = re.sub(r"\{[^}]*\}", "{}", str(getattr(route, "path", "")))
            for method in sorted(getattr(route, "methods", None) or {"WEBSOCKET"}):
                if (method, path) in seen:
                    raise RegistryError(f"{area}: {method} {path} is registered twice")
                seen.add((method, path))
            routes.append(route)
    return routes


def create_app(
    config: HubConfig,
    *,
    pool: Pool | None = None,
    clock: Clock = system_clock,
    routers: Sequence[tuple[str, Any]] | None = None,
    resolvers: Mapping[str, KeyResolver] | None = None,
    jobs: Iterable[Job] | None = None,
    run_jobs: bool = True,
    read_log_window: float = READ_WINDOW_SECONDS,
) -> FastAPI:
    """The hub app; the defaults discover every extension point (tests pass their own)."""
    key_resolvers = dict(merge_mappings(KEY_RESOLVERS) if resolvers is None else resolvers)
    check_resolvers(key_resolvers)
    job_list = check_jobs(concat_sequences(JOBS) if jobs is None else jobs)
    routes = collect_routes(discovered_routers() if routers is None else routers)
    signals.configure()
    signer = config.signer()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        own_pool = pool is None
        if pool is None:
            active = await asyncio.to_thread(open_pool, config.database, timeout=POOL_OPEN_TIMEOUT_SECONDS)
        else:
            active = pool
        read_log = ReadLog(active, window=read_log_window, clock=clock)
        context = HubContext(
            config=config,
            pool=active,
            clock=clock,
            signer=signer,
            resolvers=key_resolvers,
            limiter=RateLimiter(clock),
            read_log=read_log,
        )
        app.state.hub = context
        runner = JobRunner(job_list, context)
        try:
            await runner.prepare()
        except BaseException:  # also a cancelled start closes the own pool
            if own_pool:
                await asyncio.to_thread(active.close)
            raise
        stream = StreamService(context)
        app.state.stream = stream
        await read_log.start()
        await stream.start()
        if run_jobs:
            runner.start()
        try:
            yield
        finally:
            await stream.stop()
            await runner.stop()
            await read_log.stop()
            if own_pool:
                await asyncio.to_thread(active.close)

    app = FastAPI(
        title="agent-hub",
        version=__version__,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.router.routes.extend(routes)
    handlers.install(app)
    app.add_middleware(HubPipeline, hub=app)
    app.add_middleware(SecurityHeaders)
    return app
