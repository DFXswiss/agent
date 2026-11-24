"""Test kit of the KOR-50 model fallback suites (ADR-081): the model kit (credential hub, fake guest, real store) with
the operator's pool, a deliverer, one wall clock for the limits and the session area, a usage double that answers
each subscription token with its own quota document, and helpers that feed the gateway's `usage` records through the
real journal handlers.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_runner import registry
from agent_runner.model import exhaustion, limits, operator, pauses
from agent_runner.model import session_request as model_request
from agent_runner.model.operator import Pool
from agent_runner.prompts.deliver import Deliverer, install
from agent_runner.sessions import records
from agent_runner.sessions.consume import VmJournal
from agent_runner.sessions.records import time_text
from agent_runner.sessions.start import Starter
from agent_runner.vm import records as vm_records

from ..prompts.kit import PromptGuest
from ..quota.double import USAGE_PATH, Answer, UsageDouble
from ..sessions.kit import desired, session_id, wire
from .kit import OPUS, TOKEN, Env, Kit, attach, model_kit, store_record, subscription_body
from .test_operator import Clock, put, token

T0 = 1_795_000_000.0
"""The wall time of the suites (the credential kit's hub time)."""
HOUR = 3600.0
AT = T0 + 20.0
"""The first whole minute after `T0`: quota release times are whole minutes (`quota.parsers`)."""


def window(used: float, resets: float | None) -> dict[str, Any]:
    return {"utilization": used, "resets_at": None if resets is None else time_text(resets)}


def usage_document(five: float = 10.0, five_resets: float | None = AT + HOUR, seven: float = 5.0) -> dict[str, Any]:
    """An OAuth usage document: the five-hour window `five` % until `five_resets` (rounded to the minute by the
    parser: use whole minutes), the weekly one `seven` %."""
    return {
        "five_hour": window(five, five_resets),
        "seven_day": window(seven, AT + 7 * 24 * HOUR),
        "limits": [],
    }


class Quotas(UsageDouble):
    """The usage endpoint for several tokens: `documents[token]` is that subscription's answer (a document, or an
    `Answer` with another status); an unknown token is refused (`401`)."""

    def __init__(self, *, offset: int = 0) -> None:
        super().__init__("unused", offset=offset)
        self.documents: dict[str, Any] = {}

    def __enter__(self) -> Quotas:
        super().__enter__()
        return self

    def answer(self, path: str, headers: Mapping[str, str]) -> Answer:
        given = headers.get("authorization", "").removeprefix("Bearer ")
        found = self.documents.get(given)
        if path != USAGE_PATH or found is None:
            return Answer(401, b'{"error":{"type":"authentication_error"}}')
        if isinstance(found, Answer):
            return found
        return Answer(200, json.dumps(found).encode())


@dataclass
class Wall:
    now: float = T0

    def __call__(self) -> float:
        return self.now


@dataclass
class FKit:
    env: Env
    kit: Kit
    guest: PromptGuest
    deliverer: Deliverer
    pool: Pool
    clock: Clock
    wall: Wall
    quotas: Quotas
    lines: list[str] = field(default_factory=list)

    @property
    def context(self) -> Any:
        return self.env.context

    def log(self) -> str:
        return "\n".join([*self.kit.lines, *self.lines, self.env.context.log.getvalue()])

    async def start(self, n: int = 1, **overrides: Any) -> Any:
        return await Starter(self.kit.session_env).start(desired(n, **overrides))

    async def state(self, sid: str, state: str) -> None:
        async with self.env.pool.connection() as conn:
            await records.update(conn, sid, state=state)

    async def stored(self, sid: str) -> records.LocalSession:
        return await self.kit.stored(sid)

    def emit(self, status: int, sid: str | None = None, model: str = OPUS) -> None:
        """The gateway's `usage` record of one request the upstream answered with `status`."""
        zero = dict.fromkeys(("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"), 0)
        body = {
            "request_id": "req_1",
            "model": model,
            **zero,
            "thinking_tokens": 0,
            "context_window_tokens": 0,
            "request_bytes": 10,
            "response_bytes": 20,
            "status": status,
        }
        self.guest.emit(sid or session_id(), "usage", body)

    async def consume(self) -> None:
        """The journal of the VM through the real handlers, then the exhaustion checks it started."""
        row = await vm_records.get(self.env.pool, "vm-00000001")
        assert row is not None
        reader = VmJournal(self.kit.session_env, row, handlers=registry.journal_handlers())
        stopping = asyncio.Event()
        task = asyncio.ensure_future(reader.run(stopping))
        last = self.guest.records[-1].seq
        for _ in range(6000):
            if self.guest.acked >= last or task.done():
                break
            await asyncio.sleep(0.005)
        stopping.set()
        await task
        await exhaustion.settle(self.context)

    async def limit(self, key: str) -> tuple[Any, ...] | None:
        rows = await self.env.rows(
            "SELECT kind, until, readable, identity FROM runner.access_limit WHERE access_key = %s", key
        )
        if not rows:
            return None
        kind, until, readable, identity = rows[0]
        return kind, None if until is None else until.timestamp(), readable, identity

    async def pause_row(self, sid: str) -> pauses.Pause | None:
        async with self.env.pool.connection() as conn:
            return await pauses.get(conn, sid)

    def credentials(self) -> list[tuple[str, dict[str, Any]]]:
        return [value for name, value in self.guest.calls if name == "put_credential"]

    def set_desired(self, state: dict[str, Any]) -> None:
        self.kit.session_env.desired = state


def entry(n: int = 1, *, own_to_dfx: bool = False, to_platform: bool = True, consent: bool = False, **over: Any) -> Any:
    """The session as the device state lists it, with the owner's fallback settings and the task's consent."""
    return {
        **wire(desired(n, **over)),
        "fallback": {"own_to_dfx": own_to_dfx, "operator_to_platform": to_platform},
        "model_consent": consent,
    }


def state(*entries: Any, api: bool = True, **extra: Any) -> dict[str, Any]:
    found: dict[str, Any] = {"sessions": list(entries), **extra}
    if api:
        found["model_fallback"] = {"api": 1}
    return found


@asynccontextmanager
async def fallback_kit(
    env: Env,
    directory: Path | None,
    *,
    files: tuple[str, ...] = ("a", "b"),
    own: bool = True,
    desired_state: Mapping[str, Any] | None = None,
    **options: Any,
) -> AsyncIterator[FKit]:
    """The kit (module docstring). `directory`: the operator's pool with `files` (None: no pool)."""
    if own:
        store_record(env, "claude_subscription", subscription_body())
    clock = Clock()
    pool = Pool(directory=directory or Path("/nonexistent"), orgs=frozenset({desired().org_id}), clock=clock)
    lines: list[str] = []
    if directory is not None:
        for n, name in enumerate(files, 1):
            put(directory, name, token(n))
        pool.log = lines.append
        env.context.config.operator_subscription_dir = directory
        env.context.config.operator_subscription_orgs = pool.orgs
        operator.attach(env.context, pool)
        setattr(env.context, operator.ATTRIBUTE, pool)
    guest = PromptGuest()
    found = await model_kit(env, guest=guest, desired_state=dict(desired_state or state()), **options)
    attach(found)
    wall = Wall()
    found.session_env.wall = wall
    limits.limits_for(env.context).wall = wall
    found.session_env.amenders = list(model_request.AMENDERS)
    deliverer = install(env.context, Deliverer(found.session_env, idle_seconds=3600.0))
    with Quotas() as quotas:
        env.context.config.anthropic_usage_url = quotas.url + USAGE_PATH
        quotas.documents[TOKEN] = usage_document()
        for n in range(1, len(files) + 1):
            quotas.documents[token(n)] = usage_document()
        yield FKit(env, found, guest, deliverer, pool, clock, wall, quotas, lines)
        await exhaustion.settle(env.context)


__all__ = ["AT", "HOUR", "T0", "FKit", "entry", "fallback_kit", "session_id", "state", "usage_document"]
