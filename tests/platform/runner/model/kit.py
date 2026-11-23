"""Test kit of the RUN-12 model suites: the credential hub of RUN-09 (`credentials/kit.py`: verified logs, owner
records sealed under `CK`, `GET /v1/device/credentials/...`) under the session kit of RUN-04 (`sessions/kit.py`: fake
guest and VMs, a `SessionEnv` with the real sync runtime), so a session start opens real credential records.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from agent_runner.sessions.service import ATTRIBUTE as SERVICE_ATTRIBUTE
from agent_runner.sessions.service import SessionService

from ..credentials.kit import ALICE, Env, kit_env, make_record
from ..sessions.kit import KEY, Kit, kit

ANTHROPIC = "https://api.anthropic.com"
API_KEY = "test-api-key-APIKEYMARKER-0123456789abcdef"
TOKEN = "test-oauth-token-TOKENMARKER-0123456789abcdef"
MARKERS = ("APIKEYMARKER", "TOKENMARKER", "SECRETMARKER")
FLASH = "deepseek/deepseek-v4.1-flash-1017"
OPUS = "claude-opus-5-5"
__all__ = ["ALICE", "KEY", "Env", "Kit"]


def api_key_body(key: str = API_KEY) -> dict[str, Any]:
    return {"key": key}


def subscription_body(token: str = TOKEN) -> dict[str, Any]:
    return {"token": token, "email": None, "kind": "setup_token"}


def store_record(env: Env, kind: str, body: Mapping[str, Any], *, version: int = 1, owner: str = ALICE) -> None:
    """The owner's credential record at the hub (sealed under `CK` epoch 1 by the owner)."""
    env.hub.records[(owner, kind)] = make_record(dict(body), kind=kind, version=version, owner=owner)


@asynccontextmanager
async def model_env(dsn: str, **kwargs: Any) -> AsyncIterator[Env]:
    async with kit_env(dsn, **kwargs) as env:
        env.context.config.model_upstream_anthropic = ANTHROPIC
        await env.add_ck()
        yield env


async def model_kit(env: Env, **kwargs: Any) -> Kit:
    found = await kit(env, **kwargs)  # type: ignore[arg-type]
    return found


def attach(found: Kit, service: SessionService | None = None) -> SessionService:
    """A session service over the kit's env, attached to the daemon context as the session area does."""
    created = service or SessionService(found.session_env)
    setattr(found.env.context, SERVICE_ATTRIBUTE, created)
    return created


def credentials(found: Kit) -> list[tuple[str, dict[str, Any]]]:
    return [value for name, value in found.guest.calls if name == "put_credential"]
