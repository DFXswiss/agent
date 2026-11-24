"""Shared parts of the GitHub executors and pre-checks (05-runner.md section 12.1, 03-api.md section 9, ADR-015).

- `actor_client(context, operation)`: a client with the token of the operation's **acting user** - the person whose
  instruction started the turn (`credentials.acting`, the only place that decides it and opens the credential;
  HOT-14, ADR-070, replacing the task owner's credential of ADR-015) - decrypted from the credential cache only for
<<<<<<< HEAD
  the duration of the block. No acting user, no cached or fetchable record, a record that does not open or a body
  without a usable token is `NotConnected`; a hub that cannot be asked for a missing record is
  `GitHubError("unreachable")` (retryable).
- `executor(body)`: a `403` that refuses access (SAML single sign-on, an organisation's restriction, a right the token
  lacks) is `repo_not_accessible` with the one next step in its message (`reach`, KOR-65); everything else is
=======
  the duration of the block.
- `identity_body(context, operation, data)` / `identity_client(context, operation, data)`: the body of, and a client
  with the token of, the GitHub identity a pre-check resolved for the acting user (`identity.Resolved.data()` in the
  pre-check data, KOR-47, ADR-078), opened through `credentials.acting` as well: the acting user's primary record
  (its record ID must still be the resolved one: a primary record changed in between is not used), any other record
  by its ID (`acting.opened_identity`; a personal record only of the acting user). `data_client` takes the resolved
  identity when the data names one, else the acting user's primary record (`actor_client`).
- No acting user, no cached or fetchable record, a record that does not open or a body without a usable token is
  `NotConnected`; a hub that cannot be asked for a missing record is `GitHubError("unreachable")` (retryable).
>>>>>>> ba248bc71 (Add GitHub identities: several accounts, company accounts, choices and defaults, and the commit's author, committer, co-authors and signature.)
- `failure(error, not_found=...)`: the operation failure of a `GitHubError`: `401` -> `github_not_connected`, `404`
  -> the type's code, `410` (the repository's issues are disabled, KOR-25) -> `not_found` with `ISSUES_DISABLED`,
  retryable errors (network, `5xx`, `429`, secondary and primary rate limits) -> `upstream_error` with `retryable`
  and `retry_after`, a `403` that names an organisation requiring SSO authorisation of the token -> `upstream_error`
  with `SSO_REQUIRED` (GitHub's authorisation URL is never passed on), anything else -> `upstream_error`.
- validators of the GitHub answer fields a result carries (`contracts/ops/objects.json`): an answer that does not fit
  the result contract is `upstream_error`, never passed on.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

<<<<<<< HEAD
from agent_runner.credentials import acting
from agent_runner.credentials.cache import CredentialUnavailable
from agent_runner.github import reach
=======
from agent_runner.credentials import acting, identities
from agent_runner.credentials.cache import CredentialUnavailable, RecordRefused
>>>>>>> ba248bc71 (Add GitHub identities: several accounts, company accounts, choices and defaults, and the commit's author, committer, co-authors and signature.)
from agent_runner.github.client import TOKEN_RE, GitHubClient, GitHubError, settings_for
from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.operations.records import Operation
from agent_runner.orgs.db import session
from agent_runner.sync.http import HubRefusal, HubTransportError

URL_RE = re.compile(r"^https://[!-~]{1,2048}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}(\[bot\])?$")
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
TIME_RE = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,6})?Z$"
)
MAX_GITHUB_ID = 9_007_199_254_740_991
MAX_NUMBER = 2_147_483_647
NOT_CONNECTED = (
    "The GitHub connection of the person who gave this instruction is missing or was refused; that person must"
    " connect GitHub again."
)
ISSUES_DISABLED = "Issues are disabled in this repository."
SSO_REQUIRED = (
    "The organisation of the repository requires SSO authorisation of the GitHub token; the person who gave this "
    "instruction must authorise the token for the organisation at GitHub."
)


class NotConnected(Exception):
    """The acting user has no usable GitHub credential on this device."""


@asynccontextmanager
async def client_of(context: Any, body: Mapping[str, Any], label: str) -> AsyncIterator[GitHubClient]:
    """A client with the token of an opened body (`NotConnected` without a usable token)."""
    token = body.get("token")
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise NotConnected
    client = settings_for(context).client(token, label)
    del token
    async with client:
        yield client


@asynccontextmanager
async def actor_client(context: Any, operation: Operation) -> AsyncIterator[GitHubClient]:
    try:
<<<<<<< HEAD
        async with acting.opened(context, operation, "github") as body:
            token = body.get("token")
            if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
                raise NotConnected
            assert operation.actor_id is not None
            client = settings_for(context).client(token, operation.actor_id)
            source = body.get("source")
            client.source = source if isinstance(source, str) else "device_flow"
            del token
            async with client:
                yield client
=======
        async with (
            acting.opened(context, operation, "github") as body,
            client_of(context, body, str(operation.actor_id)) as client,
        ):
            yield client
>>>>>>> ba248bc71 (Add GitHub identities: several accounts, company accounts, choices and defaults, and the commit's author, committer, co-authors and signature.)
    except (CredentialUnavailable, HubRefusal):
        raise NotConnected from None
    except HubTransportError:
        raise GitHubError("unreachable", retryable=True) from None


@asynccontextmanager
async def _body(context: Any, operation: Operation, data: Mapping[str, Any]) -> AsyncIterator[dict[str, Any]]:
    identity_id, owner = str(data["github_identity"]), str(data["identity_owner"])
    if data.get("identity_primary"):
        async with acting.opened(context, operation, "github") as body:
            async with session(context.pool) as conn:
                current = await acting.primary_record_id(conn, operation)
            if (current, operation.actor_id) != (identity_id, owner):
                raise NotConnected
            yield body
        return
    found = await identities.meta(context, identity_id)
    if found is None or found.owner != owner:
        raise NotConnected
    async with acting.opened_identity(context, operation, found) as body:
        yield body


@asynccontextmanager
async def identity_body(context: Any, operation: Operation, data: Mapping[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """The decrypted body of the resolved identity (module docstring)."""
    try:
        async with _body(context, operation, data) as body:
            yield body
    except (CredentialUnavailable, HubRefusal, RecordRefused):
        raise NotConnected from None
    except HubTransportError:
        raise GitHubError("unreachable", retryable=True) from None


@asynccontextmanager
async def identity_client(context: Any, operation: Operation, data: Mapping[str, Any]) -> AsyncIterator[GitHubClient]:
    """A client with the token of the resolved identity (module docstring)."""
    label = str(data["identity_owner"])
    async with identity_body(context, operation, data) as body, client_of(context, body, label) as client:
        yield client


def data_client(
    context: Any, operation: Operation, data: Mapping[str, Any]
) -> AbstractAsyncContextManager[GitHubClient]:
    """The resolved identity's client when the pre-check data names one, else the acting user's primary record."""
    return identity_client(context, operation, data) if "github_identity" in data else actor_client(context, operation)


def failure(error: GitHubError, *, not_found: str = "not_found") -> OperationFailed:
    if error.kind == "unauthorized":
        return OperationFailed("github_not_connected", message=NOT_CONNECTED)
    if error.kind == "not_found":
        return OperationFailed(not_found)
    if error.kind == "gone":
        return OperationFailed("not_found", message=ISSUES_DISABLED)
    if error.sso:
        return OperationFailed("upstream_error", message=SSO_REQUIRED)
    if error.retryable:
        return OperationFailed("upstream_error", retryable=True, retry_after=error.retry_after)
    return OperationFailed("upstream_error", message="GitHub refused the request.")


Body = Callable[[OperationContext, GitHubClient], Awaitable[dict[str, Any]]]


def executor(body: Body, *, not_found: str = "not_found") -> Callable[[OperationContext], Awaitable[dict[str, Any]]]:
    """An executor that runs `body` with the resolved identity's client and maps the GitHub failures."""

    async def run(ctx: OperationContext) -> dict[str, Any]:
        client: GitHubClient | None = None
        try:
            async with data_client(ctx.context, ctx.operation, ctx.data) as client:
                return await body(ctx, client)
        except NotConnected:
            raise OperationFailed("github_not_connected", message=NOT_CONNECTED) from None
        except GitHubError as error:
            raise _refused(client, ctx, error) or failure(error, not_found=not_found) from None

    return run


def _refused(client: GitHubClient | None, ctx: OperationContext, error: GitHubError) -> OperationFailed | None:
    """`repo_not_accessible` with the one next step for a `403` access refusal at execution time (`reach`, KOR-65)."""
    repo = ctx.params.get("repo")
    if client is None or error.kind != "forbidden" or not isinstance(repo, str):
        return None
    step = reach.next_step(client, repo, error)
    return None if step is None else OperationFailed("repo_not_accessible", message=reach.message(step, repo))


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
