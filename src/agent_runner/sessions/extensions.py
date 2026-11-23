"""Extension points of the session area (ADR-027 section 4 style; ADR-032).

Discovered with `registry.discover` (areas in sorted order), so RUN-05 and later packages add files and never edit
this area:

- `<area>/session_prompt.py:FIRST_PROMPT`: `first_prompt(context, session) -> InitialPrompt | None`, the verified
  first prompt of a new main session (RUN-05); at most one provider. Without one, a session starts without an
  initial prompt and waits in `prompt_delivering` for the prompt delivery.
- `<area>/session_state.py:OBSERVERS`: sequence of `StateObserver(name, handle)`; `handle(journal, conn, session,
  body)` sees every guest `session.state` record of a known session after its transition was written, in the same
  transaction (RUN-05: `prompt_accepted`/`prompt_refused` -> `prompt_delivery`).
- `<area>/session_request.py:AMENDERS`: sequence of `RequestAmender(name, amend)`; `amend(context, session, request)`
  may change the `POST /v1/sessions` request of a session start before it is sent (RUN-05: the harness hint of a
  restarted main session, 03-api.md section 4.6.3).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection

from agent_runner import registry
from agent_runner.errors import RunnerError
from agent_runner.sessions.records import LocalSession

_NAME_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")


@dataclass(frozen=True)
class InitialPrompt:
    """The prompt event's ID and its verified, decrypted text."""

    prompt_id: str
    text: str


FirstPrompt = Callable[[Any, LocalSession], Awaitable[InitialPrompt | None]]
Amend = Callable[[Any, LocalSession, dict[str, Any]], Awaitable[None]]
ObserverHandle = Callable[[Any, AsyncConnection[Any], LocalSession, Mapping[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class StateObserver:
    name: str
    handle: ObserverHandle


@dataclass(frozen=True)
class RequestAmender:
    name: str
    amend: Amend


async def no_first_prompt(_context: Any, _session: LocalSession) -> InitialPrompt | None:
    return None


def first_prompt_provider(package: str = registry.ROOT_PACKAGE) -> FirstPrompt:
    """The registered first-prompt provider, else `no_first_prompt`."""
    found: list[FirstPrompt] = []
    for area, value in registry.discover("session_prompt", "FIRST_PROMPT", package):
        if not callable(value):
            raise RunnerError("registry_invalid", f"{area}.session_prompt")
        found.append(value)
    if len(found) > 1:
        raise RunnerError("registry_duplicate", "FIRST_PROMPT")
    return found[0] if found else no_first_prompt


def state_observers(package: str = registry.ROOT_PACKAGE) -> list[StateObserver]:
    found: list[StateObserver] = []
    seen: set[str] = set()
    for area, value in registry.discover("session_state", "OBSERVERS", package):
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise RunnerError("registry_invalid", f"{area}.session_state")
        for item in value:
            if not isinstance(item, StateObserver) or not _NAME_RE.fullmatch(item.name) or not callable(item.handle):
                raise RunnerError("registry_invalid", f"{area}.session_state")
            if item.name in seen:
                raise RunnerError("registry_duplicate", item.name)
            seen.add(item.name)
            found.append(item)
    return found


def request_amenders(package: str = registry.ROOT_PACKAGE) -> list[RequestAmender]:
    found: list[RequestAmender] = []
    seen: set[str] = set()
    for area, value in registry.discover("session_request", "AMENDERS", package):
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise RunnerError("registry_invalid", f"{area}.session_request")
        for item in value:
            if not isinstance(item, RequestAmender) or not _NAME_RE.fullmatch(item.name) or not callable(item.amend):
                raise RunnerError("registry_invalid", f"{area}.session_request")
            if item.name in seen:
                raise RunnerError("registry_duplicate", item.name)
            seen.add(item.name)
            found.append(item)
    return found
