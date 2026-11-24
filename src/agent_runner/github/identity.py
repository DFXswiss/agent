"""The GitHub identity an operation acts with (KOR-47, ADR-078 sections 2 and 3; 03-api.md section 9; 05-runner.md
section 12 step 3).

The human H of ADR-078 is the operation's **acting user** of HOT-14 (ADR-070): the verified author of the prompt that
started the session's turn, stamped at the receipt (`operation.actor_id`, `credentials.acting`). An operation without
acting user acts with no identity at all (`github_not_connected`; the pipeline rejects it at once). Every opening of a
record goes through `credentials.acting` (`acting.opened`, `acting.opened_identity`).

**Usable** (`usable`): for an operation of a task in organization O, an identity record is usable by H when it is H's
own (owner = H, source `own`), a company record of O while H is an active user member of O in the verified membership
log (`task_org`), or a company record of the operator's organization (`config.operator_github_org`, ADR-078 section
7) while H is an active user member of that organization (`operator`). Never another person's personal record and
never another organization's. The owner and its kind come from the verified record (`credentials.identities`); a hub
cannot change them.

**Resolution** (`resolve`, run first by every `github.*` pre-check, `resolved(check)`): the first of

1. the operation's parameter `github_identity`, 2. the session's identity, 3. the task's identity (`selection`):
   an explicit choice. A hub that refuses the record with `403` (the device is no reader of its owner) or a record H
   may not use is `identity_forbidden` (not held); a record that is gone (`404`), refused by the verification or does
   not open with a token is `github_not_connected` (held like a missing connection, KOR-25);
4. the workgroup's default, 5. the organization's default: a default H may not use (a personal record, or a record of
   another organization than the task's) is skipped; a default H may use, or whose record cannot be read at all
   (refused by the hub, gone, refused by the verification, or not opening - e.g. while the wrap of the organization's
   `cred` epoch has not reached this device), **holds** the operation (`github_not_connected`): it never falls through
   to H's personal account or the operator's;
6. H's own primary record (`acting.opened`, fetched on demand);
7. the operator's account: the newest company record (header time) of the operator's organization whose body has
   `source:"operator"`, among the cached records of that organization (listed at the hub first when none is cached,
   `identities.known`).

Nothing usable is `github_not_connected` (held). A hub that cannot be reached while a record is needed is
`upstream_error` (a pre-check is not retried; the session may ask again).

**Account check** (`resolved`, ADR-078 section 4): before the type's own pre-check, `GET /user` with the resolved
identity's token must name the GitHub account the record says (`user_id`); another account, or a record without
`user_id`, is `identity_unavailable` (not held: the record must be connected again), `401` is `github_not_connected`
(held), any other failure `upstream_error`. A check that passed is remembered per daemon for the record's version
(`CHECKED`), so a record is asked once per version.

The result (`Resolved.data()`, kept in the pre-check data) names the identity (`github_identity`, its owner and kind,
whether it is H's primary record) and H (`acting_user`); the executors open exactly that record
(`ops.identity_client`), and the registration carries `github_identity` (`operations.register`).

`available(conn, operation, session)` is the formal check's cheap answer whether any GitHub identity may exist for the
operation on this device (a cached `github` record of the acting user, any cached identity record, a choice or a
default); the resolution decides.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection

from agent_runner.credentials import acting, identities
from agent_runner.credentials.cache import CredentialUnavailable, RecordRefused
from agent_runner.credentials.identities import ORG, USER, Identity
from agent_runner.github import ops, selection
from agent_runner.github.client import TOKEN_RE, GitHubError
from agent_runner.operations.extensions import CheckFailed, PrecheckRequest, PrecheckResult
from agent_runner.orgs.book import OrgBook, book_for
from agent_runner.orgs.db import session as store_session
from agent_runner.sync.http import HubRefusal, HubTransportError

FORBIDDEN = "The GitHub identity may not be used for the person who gave this instruction."
HUB_FAILED = "The GitHub identity could not be read from the hub."
OTHER_ACCOUNT = "The GitHub identity's token belongs to another GitHub account than the identity; connect it again."
ACCOUNT_FAILED = "The GitHub account of the identity could not be checked."
OWN, TASK_ORG, OPERATOR = "own", "task_org", "operator"
CHECKED = "github_identity_checked"

Check = Callable[[PrecheckRequest], Awaitable[PrecheckResult]]


@dataclass(frozen=True)
class Resolved:
    identity_id: str
    owner: str
    owner_kind: str
    primary: bool
    """The acting user's primary record (`acting.opened`)."""
    source: str
    acting_user: str
    version: int = 0
    """The record's version (the key of the account check)."""
    account: object = None
    """The GitHub account ID the primary record names (`user_id`; `primary` only)."""

    def data(self) -> dict[str, Any]:
        return {
            "github_identity": self.identity_id,
            "identity_owner": self.owner,
            "identity_kind": self.owner_kind,
            "identity_primary": self.primary,
            "acting_user": self.acting_user,
        }


CURRENT: ContextVar[Resolved] = ContextVar("github_identity")
"""The identity resolved for the pre-check that is running (`resolved`)."""


class Held(Exception):
    """A default that H may use but whose record cannot be read: the operation waits (module docstring)."""


def not_connected() -> CheckFailed:
    return CheckFailed("github_not_connected", message=ops.NOT_CONNECTED)


def forbidden() -> CheckFailed:
    return CheckFailed("identity_forbidden", message=FORBIDDEN)


def operator_org(context: Any) -> str | None:
    """`OPERATOR_GITHUB_ORG` (ADR-078 section 7), None while unset."""
    found: str | None = context.config.operator_github_org
    return found


async def loaded(context: Any) -> OrgBook:
    book = book_for(context)
    async with store_session(context.pool) as conn:
        await book.ensure_loaded(conn)
    return book


def member(book: OrgBook, org: str, user: str) -> bool:
    """`user` is an active user member of the acting org `org` (decertified devices are never users)."""
    log = book.log(org)
    found = None if log is None else log.state.member(user)
    return found is not None and found.kind == "user"


def usable(context: Any, book: OrgBook, owner: str, owner_kind: str, *, user: str, org_id: str) -> str | None:
    """Why `user` may use a record of `owner` in a task of `org_id` (`own`, `task_org`, `operator`), else None."""
    if owner_kind == USER:
        return OWN if owner == user else None
    if owner == org_id and member(book, org_id, user):
        return TASK_ORG
    operator = operator_org(context)
    if operator is not None and owner == operator and member(book, operator, user):
        return OPERATOR
    return None


def valid_token(body: Mapping[str, Any]) -> bool:
    token = body.get("token")
    return isinstance(token, str) and TOKEN_RE.fullmatch(token) is not None


async def opens(context: Any, operation: Any, found: Identity, *, operator: bool = False) -> bool:
    """The record opens for the operation with a token (and, with `operator`, is the operator's token)."""
    try:
        async with acting.opened_identity(context, operation, found) as body:
            return valid_token(body) and (not operator or body.get("source") == "operator")
    except CredentialUnavailable:
        return False


async def _explicit(context: Any, book: OrgBook, request: PrecheckRequest, user: str, identity_id: str) -> Resolved:
    try:
        found = await identities.meta(context, identity_id)
    except HubRefusal as refusal:
        raise (forbidden() if refusal.status == 403 else not_connected()) from None
    except RecordRefused:
        raise not_connected() from None
    if found is None:
        raise not_connected()
    source = usable(context, book, found.owner, found.owner_kind, user=user, org_id=request.session.org_id)
    if source is None:
        raise forbidden()
    if not await opens(context, request.operation, found):
        raise not_connected()
    return Resolved(found.id, found.owner, found.owner_kind, False, source, user, found.version)


async def _default(
    context: Any, book: OrgBook, request: PrecheckRequest, user: str, identity_id: str
) -> Resolved | None:
    """The default's identity; None when H may not use it (skipped); `Held` when it cannot be read."""
    try:
        found = await identities.meta(context, identity_id)
    except (HubRefusal, RecordRefused):
        raise Held from None
    if found is None:
        raise Held
    org = request.session.org_id
    source = usable(context, book, found.owner, found.owner_kind, user=user, org_id=org)
    if found.owner_kind != ORG or found.owner != org or source is None:
        return None
    if not await opens(context, request.operation, found):
        raise Held
    return Resolved(found.id, found.owner, found.owner_kind, False, source, user, found.version)


async def primary(context: Any, operation: Any) -> Resolved | None:
    """The acting user's own primary record when it opens with a token."""
    try:
        async with acting.opened(context, operation, "github") as body:
            if not valid_token(body):
                return None
            account = body.get("user_id")
    except (CredentialUnavailable, HubRefusal):
        return None
    async with store_session(context.pool) as conn:
        record_id = await acting.primary_record_id(conn, operation)
        version = await acting.cached_version(conn, operation, "github")
    if record_id is None or version is None:  # purged in between
        return None
    return Resolved(record_id, operation.actor_id, USER, True, OWN, operation.actor_id, version, account)


async def _operator(context: Any, book: OrgBook, request: PrecheckRequest, user: str) -> Resolved | None:
    org = operator_org(context)
    if org is None or not member(book, org, user):
        return None
    found = await identities.known(context, org)
    for item in sorted(found, key=lambda item: str(item.record["header"]["ts"]), reverse=True):
        if await opens(context, request.operation, item, operator=True):
            # a company record of an organization H is a member of: `task_org` when it is the task's own organization
            source = usable(context, book, item.owner, item.owner_kind, user=user, org_id=request.session.org_id)
            return Resolved(item.id, item.owner, item.owner_kind, False, source or OPERATOR, user, item.version)
    return None


async def resolve(request: PrecheckRequest) -> Resolved:
    """The identity of a `github.*` operation (module docstring); `CheckFailed` otherwise."""
    context, operation, session = request.context, request.operation, request.session
    user = operation.actor_id
    if user is None:
        raise CheckFailed("github_not_connected", message=acting.NO_ACTOR)
    async with store_session(context.pool) as conn:
        picked = await selection.choices(conn, session)
        wg_default, org_default = await selection.defaults(conn, session.org_id, session.wg_id)
    if picked is None:
        raise not_connected()  # a subagent session whose identity is not known yet: wait for it
    book = await loaded(context)
    for explicit in (request.params.get("github_identity"), picked.session, picked.task):
        if explicit is not None:
            return await _explicit(context, book, request, user, str(explicit))
    try:
        for default in (wg_default, org_default):
            found = None if default is None else await _default(context, book, request, user, default)
            if found is not None:
                return found
    except Held:
        raise not_connected() from None
    found = await primary(context, operation) or await _operator(context, book, request, user)
    if found is None:
        raise not_connected()
    return found


async def check_account(context: Any, operation: Any, data: Mapping[str, Any], version: int) -> None:
    """`GET /user` with the identity's token names the record's GitHub account (module docstring)."""
    key = (str(data["github_identity"]), version)
    checked: set[tuple[str, int]] | None = getattr(context, CHECKED, None)
    if checked is None:
        checked = set()
        setattr(context, CHECKED, checked)
    if key in checked:
        return
    try:
        async with ops.identity_body(context, operation, data) as body:
            account = body.get("user_id")
            async with ops.client_of(context, body, str(data["identity_owner"])) as client:
                answer = await client.request("GET", "/user")
        named = ops.obj(answer.data).get("id")
    except ops.NotConnected:
        raise not_connected() from None
    except GitHubError as error:
        if error.kind == "unauthorized":
            raise not_connected() from None
        raise CheckFailed("upstream_error", message=ops.SSO_REQUIRED if error.sso else ACCOUNT_FAILED) from None
    if not same_account(named, account):
        raise CheckFailed("identity_unavailable", message=OTHER_ACCOUNT)
    checked.add(key)


def same_account(named: object, account: object) -> bool:
    """GitHub's answer names the account ID a record holds (a record without one never matches)."""
    return isinstance(account, int) and not isinstance(account, bool) and named == account


def current() -> Resolved:
    """The identity of the running pre-check (`resolved`)."""
    return CURRENT.get()


def resolved(check: Check) -> Check:
    """`check` after the resolution and the account check; its data gains `Resolved.data()` (`__wrapped__` is
    `check`)."""

    @functools.wraps(check)
    async def run(request: PrecheckRequest) -> PrecheckResult:
        try:
            found = await resolve(request)
        except HubTransportError:
            raise CheckFailed("upstream_error", message=HUB_FAILED) from None
        await check_account(request.context, request.operation, found.data(), found.version)
        token = CURRENT.set(found)
        try:
            result = await check(request)
        finally:
            CURRENT.reset(token)
        return PrecheckResult(dedupe=result.dedupe, data={**result.data, **found.data()})

    return run


async def available(conn: AsyncConnection[Any], operation: Any, session: Any) -> bool:
    """See the module docstring (the formal check of a `github.*` type, `operations.checks` step 5)."""
    if await acting.cached_version(conn, operation, "github") is not None:
        return True
    cur = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM runner.github_identity_cache)"
        " OR EXISTS (SELECT 1 FROM runner.github_selection WHERE (session_id = %s OR task_id = %s)"
        " AND (github_identity IS NOT NULL OR task_github_identity IS NOT NULL))"
        " OR EXISTS (SELECT 1 FROM runner.github_default WHERE (scope = 'wg' AND scope_id = %s)"
        " OR (scope = 'org' AND scope_id = %s))",
        (session.id, session.task_id, session.wg_id, session.org_id),
    )
    return bool((await cur.fetchone() or (False,))[0])
