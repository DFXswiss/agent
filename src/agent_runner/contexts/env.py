"""Collaborators of the context area, bundled so the tests inject every one of them (07-testing.md section 1).

`ContextEnv` holds the session area's environment (store, outbox, VMs and guest clients, verified logs, clocks), the
signed hub client, the device signing key, the encrypted cache and scratch directories, the settings of ADR-072
sections 4 and 6 and `wait_s` (how long a restore waits for the final checkpoint of the session it continues and
retries the hub, review KOR-33 nos. 1 and 3), the wake-up events of the export and upload loops and one lock per
session (one checkpoint of a session at a time, also when a VM is flushed). `contexts_for(context)` builds the
production instance once per daemon context (None while the device is not enrolled) and keeps it on the context.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_runner.contexts.cache import Cache
from agent_runner.sessions.env import SessionEnv

ATTRIBUTE = "context_env"
MIB = 1024 * 1024


class Hub(Protocol):
    """The part of `sync.http.HubClient` this area uses."""

    async def request(self, method: str, target: str, body: object | None = None) -> dict[str, object]: ...


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


@dataclass
class ContextEnv:
    sessions: SessionEnv
    hub: Hub
    signing_key: Ed25519PrivateKey
    cache: Cache
    scratch: Path
    """Directory for plaintext archives (the runner's `tmp/`, 0700, wiped at start); each use removes its files."""
    interval_s: float = 600.0
    cache_bytes: int = 20_480 * MIB
    workspace_bytes: int = 4096 * MIB
    transcript_bytes: int = 4096 * MIB
    wait_s: float = 180.0
    sleep: Callable[[float], Awaitable[None]] = _sleep
    clock: Callable[[], float] = time.monotonic
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when a checkpoint became due (the export loop runs at once instead of at its next poll)."""
    upload_wake: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when an object was sealed (the upload loop runs at once)."""
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def session_lock(self, session_id: str) -> asyncio.Lock:
        """The lock of one session's checkpoints."""
        return self.locks.setdefault(session_id, asyncio.Lock())

    @property
    def db(self) -> Any:
        return self.sessions.db

    @property
    def device_id(self) -> str:
        return self.sessions.device_id

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.sessions.wall(), UTC)

    def line(self, text: str) -> None:
        self.sessions.log(f"runner.contexts {text}")


def contexts_for(context: Any) -> ContextEnv | None:
    """The context environment of a daemon; None while the device is not enrolled."""
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, ContextEnv):
        return found
    from agent_runner.sessions.service import service_for  # noqa: PLC0415 - the service imports the extension points
    from agent_runner.sync.runtime import runtime  # noqa: PLC0415

    service = service_for(context)
    sync = runtime(context)
    if service is None or sync is None:
        return None
    config = context.config
    built = ContextEnv(
        sessions=service.env,
        hub=sync.client,
        signing_key=context.identity.keys.signing_key(),
        cache=Cache(config.contexts_dir),
        scratch=config.tmp_dir,
        interval_s=float(config.context_interval_seconds),
        cache_bytes=config.context_cache_mib * MIB,
        workspace_bytes=config.context_workspace_mib * MIB,
        transcript_bytes=config.context_transcript_mib * MIB,
        wait_s=float(config.context_wait_seconds),
        sleep=context.host.sleep,
        clock=context.host.clock,
    )
    return install(context, built)


def install(context: Any, env: ContextEnv) -> ContextEnv:
    setattr(context, ATTRIBUTE, env)
    return env


def existing(context: Any) -> ContextEnv | None:
    found = getattr(context, ATTRIBUTE, None)
    return found if isinstance(found, ContextEnv) else None


def audit_body(
    *, action: str, result: str, task_id: str, session_id: str, detail: Mapping[str, object]
) -> dict[str, object]:
    """An outbox `audit` row about a session (metadata only)."""
    return {
        "action": action,
        "result": result,
        "task_id": task_id,
        "session_id": session_id,
        "target_kind": "session",
        "target_id": session_id,
        "detail": dict(detail),
    }
