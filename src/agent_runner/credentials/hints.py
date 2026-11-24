"""WebSocket hint `credentials {owner_id}` (03-api.md section 6.2, 05-runner.md section 2.6): re-read the owner's
records (the legacy kinds of a user and every cached GitHub identity record of the owner, `identities.refresh`;
KOR-47). The hint carries no content and is never trusted: an owner ID that is not a canonical UUID is logged and
dropped, and every record read is verified (`cache.fetch`)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from agent_runner.credentials.identities import refresh
from agent_runner.reconcile import wire


async def on_credentials(context: Any, hint: Mapping[str, object]) -> None:
    try:
        owner = wire.uuid_text(hint.get("owner_id"), "credentials")
    except wire.ResponseInvalid:
        context.log.write("runner.credentials hint_invalid\n")
        context.log.flush()
        return
    await refresh(context, owner)


HANDLERS: dict[str, Callable[..., Any]] = {"credentials": on_credentials}
