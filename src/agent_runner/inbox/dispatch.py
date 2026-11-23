"""Consumers of verified inbound events (05-runner.md sections 2.3 and 2.4; ADR-039).

Other areas receive the events the inbox pulled **after** they passed verification (E1-E5 with the author's right at
`lh`, decryption with a held scope key) by registering consumers in `agent_runner/<area>/consumers.py:CONSUMERS`, a
sequence of `Consumer(name, kinds, handle)`; areas are discovered in sorted order (`registry.discover`), names are
unique and every kind must be an event kind of `crypto.log.events.KIND_RULES`.

`handle(context, conn, event)` runs inside the transaction that also advances the scope's cursor, so a consumer's own
writes and the cursor commit together (at-least-once; a consumer must tolerate a repeat after a crash before the
commit). A consumer that raises rolls the transaction back: the cursor stays and the scope is retried at the next
pull, later events of that scope wait (strict order per scope).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection

from agent_runner import registry
from agent_runner.crypto.log.events import KIND_RULES
from agent_runner.errors import RunnerError

EVENT_KINDS = frozenset(rule.kind for rule in KIND_RULES)
_NAME_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")


@dataclass(frozen=True)
class VerifiedEvent:
    """An inbound event that passed every check, with its decrypted body."""

    org_id: str
    scope: str
    scope_id: str
    scope_seq: int
    received_at: str
    header: Mapping[str, object]
    body: Mapping[str, object]
    sealed: Mapping[str, object]
    """The event as received (header, ct, sig), e.g. to forward or to store."""

    @property
    def kind(self) -> str:
        return str(self.header["kind"])

    @property
    def event_id(self) -> str:
        return str(self.header["id"])

    @property
    def author(self) -> str:
        return str(self.header["author"])


Handle = Callable[[Any, AsyncConnection[Any], VerifiedEvent], Awaitable[None]]


@dataclass(frozen=True)
class Consumer:
    name: str
    kinds: frozenset[str]
    handle: Handle


def consumers(package: str = registry.ROOT_PACKAGE) -> list[Consumer]:
    """The registered consumers in area order (`<area>/consumers.py:CONSUMERS`)."""
    found: list[Consumer] = []
    seen: set[str] = set()
    for area, value in registry.discover("consumers", "CONSUMERS", package):
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise RunnerError("registry_invalid", f"{area}.consumers")
        for item in value:
            valid = (
                isinstance(item, Consumer)
                and _NAME_RE.fullmatch(item.name) is not None
                and bool(item.kinds)
                and item.kinds <= EVENT_KINDS
            )
            if not valid:
                raise RunnerError("registry_invalid", f"{area}.consumers")
            if item.name in seen:
                raise RunnerError("registry_duplicate", item.name)
            seen.add(item.name)
            found.append(item)
    return found
