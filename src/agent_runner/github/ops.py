"""Shared parts of the GitHub executors and pre-checks (05-runner.md section 12.1, 03-api.md section 9, ADR-015).

- `owner_client(context, owner)`: a client with the task owner's token (operations use the credential of the **task
  owner**, ADR-015), decrypted from the credential cache only for the duration of the block. No owner, no cached or
  fetchable record, a record that does not open or a body without a usable token is `NotConnected`; a hub that cannot
  be asked for a missing record is `GitHubError("unreachable")` (retryable).
- `failure(error, not_found=...)`: the operation failure of a `GitHubError`: `401` -> `github_not_connected`, `404`
  -> the type's code, retryable errors (network, `5xx`, `429`, secondary and primary rate limits) -> `upstream_error`
  with `retryable` and `retry_after`, anything else -> `upstream_error`.
- validators of the GitHub answer fields a result carries (`contracts/ops/objects.json`): an answer that does not fit
  the result contract is `upstream_error`, never passed on.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from agent_runner.credentials.cache import CredentialUnavailable, opened
from agent_runner.github.client import GitHubClient, GitHubError, settings_for
from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.sync.http import HubRefusal, HubTransportError

TOKEN_RE = re.compile(r"^[!-~]{1,4096}$")
URL_RE = re.compile(r"^https://[!-~]{1,2048}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}(\[bot\])?$")
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
TIME_RE = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,6})?Z$"
)
MAX_GITHUB_ID = 9_007_199_254_740_991
MAX_NUMBER = 2_147_483_647
NOT_CONNECTED = "The task owner's GitHub connection is missing or was refused; connect GitHub again."


class NotConnected(Exception):
    """The task owner has no usable GitHub credential on this device."""


@asynccontextmanager
async def owner_client(context: Any, owner: str | None) -> AsyncIterator[GitHubClient]:
    if owner is None:
        raise NotConnected
    try:
        async with opened(context, owner, "github") as body:
            token = body.get("token")
            if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
                raise NotConnected
            client = settings_for(context).client(token, owner)
            del token
            async with client:
                yield client
    except (CredentialUnavailable, HubRefusal):
        raise NotConnected from None
    except HubTransportError:
        raise GitHubError("unreachable", retryable=True) from None


def failure(error: GitHubError, *, not_found: str = "not_found") -> OperationFailed:
    if error.kind == "unauthorized":
        return OperationFailed("github_not_connected", message=NOT_CONNECTED)
    if error.kind == "not_found":
        return OperationFailed(not_found)
    if error.retryable:
        return OperationFailed("upstream_error", retryable=True, retry_after=error.retry_after)
    return OperationFailed("upstream_error", message="GitHub refused the request.")


Body = Callable[[OperationContext, GitHubClient], Awaitable[dict[str, Any]]]


def executor(body: Body, *, not_found: str = "not_found") -> Callable[[OperationContext], Awaitable[dict[str, Any]]]:
    """An executor that runs `body` with the task owner's client and maps the GitHub failures."""

    async def run(ctx: OperationContext) -> dict[str, Any]:
        try:
            async with owner_client(ctx.context, ctx.session.owner_id) as client:
                return await body(ctx, client)
        except NotConnected:
            raise OperationFailed("github_not_connected", message=NOT_CONNECTED) from None
        except GitHubError as error:
            raise failure(error, not_found=not_found) from None

    return run


def invalid() -> GitHubError:
    return GitHubError("invalid_response")


def text(value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise invalid()
    return value


def integer(value: object, maximum: int = MAX_GITHUB_ID) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise invalid()
    return value


def obj(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise invalid()
    return value


def flag(value: object) -> bool:
    if not isinstance(value, bool):
        raise invalid()
    return value
