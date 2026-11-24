"""Collaborators of the session area, bundled so the tests inject every one of them (07-testing.md section 1).

`SessionEnv` holds the store pool, the outbox (RUN-02), the VM manager (VM-02), the guest client factory, the
verified logs (RUN-03 `OrgBook`), the device kind, the model catalog and strategies, the platform key reader, the
OpenRouter upstream, the first-prompt provider and the request amenders (RUN-05), the profile refresh (inbox pull)
and the clocks.
`service.build_env(context)` builds the production instance from a daemon context.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from psycopg import AsyncConnection

from agent_runner.guestlink.client import GuestClient
from agent_runner.model.selection import Catalog, Strategy
from agent_runner.orgs.db import session
from agent_runner.sessions.extensions import FirstPrompt, RequestAmender, no_first_prompt
from agent_runner.sessions.records import Outbox, time_text
from agent_runner.vm.records import VmRow


class Vms(Protocol):
    """The part of `vm.lifecycle.VmManager` the session area uses."""

    async def ensure_vm(
        self,
        org_id: str,
        wg_id: str,
        *,
        session_id: str | None = None,
        prefer: Sequence[str] = (),
        new_allowed: bool = True,
    ) -> VmRow: ...

    async def client(self, row: VmRow) -> GuestClient: ...

    async def touch(self, vm_id: str) -> None: ...

    async def wait_reconciled(self) -> None: ...


async def _no_refresh() -> None:
    return None


def _no_log(_text: str) -> None:
    return None


def _no_notify() -> None:
    return None


@dataclass
class SessionEnv:
    db: Any
    outbox: Outbox
    vms: Vms
    book: Any
    device_id: str
    device_kind: str
    catalog: Catalog
    strategies: Mapping[str, Strategy]
    storage_key: bytes
    platform_key: Callable[..., str | None]
    """`platform_key(org_id=None)`: the key of the `platform` access for a session of `org_id` - the platform key file
    on a platform device, the opened DFX key of that organization on an org device (ADR-080 section 7), None otherwise
    (`model.dfxkey.platform_key`). Called for each provisioning; the value is never stored or logged."""
    upstream_openrouter: str
    context: Any = None
    """The daemon context handed to extension callables (first prompt, observers)."""
    notify: Callable[[], None] = _no_notify
    """Wakes the outbox push after a commit."""
    first_prompt: FirstPrompt = no_first_prompt
    amenders: list[RequestAmender] = field(default_factory=list)
    """Change the `POST /v1/sessions` request of a start (`session_request.py:AMENDERS`)."""
    refresh_profiles: Callable[[], Awaitable[None]] = _no_refresh
    log: Callable[[str], None] = _no_log
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    desired: Mapping[str, Any] | None = None
    """The last desired state of an active device (platform budget)."""
    watchers: list[Callable[[VmRow], None]] = field(default_factory=list)
    """Called with the VM of a session as soon as it is ready (the journal task starts reading it)."""

    def at(self) -> str:
        """Device time for transitions."""
        return time_text(self.wall())

    def line(self, text: str) -> None:
        self.log(f"runner.session {text}")

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection[Any]]:
        """One real transaction (committed at the end of the block), then the outbox push is woken."""
        async with session(self.db) as conn, conn.transaction():
            yield conn
        self.notify()

    async def client(self, row: VmRow) -> GuestClient:
        return await self.vms.client(row)

    def watch(self, row: VmRow) -> None:
        for watcher in self.watchers:
            watcher(row)
