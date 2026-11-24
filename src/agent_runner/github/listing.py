"""`github.list_identities` (KOR-47, ADR-078 section 3; 03-api.md section 9; `contracts/ops/objects.json`
`result_github_list_identities`): which GitHub identities the human the session works for may use in this task, which
one applies now and which humans may be named as co-authors.

The human is the operation's acting user (`credentials.acting`, HOT-14); an operation without one is refused
`github_not_connected` like every `github.*` type (the pipeline rejects it before).

Pre-check (`check`, no connection needed, refuses only an operation without acting user): the acting user
(`acting_user`) and the identity the resolution picks now (`identity.resolve`; its data, so the registration names
it); none when nothing resolves or the hub cannot be asked.

Executor (`execute`, no GitHub request):

- `acting_user`, `current` (the identity of the pre-check, null when none);
- `identities`: every identity the device knows that the acting user may use (`identity.usable`) and that opens with
  a valid login (`credentials.acting`): the user's own primary record, the records named by the session's and task's
  choice and by the workgroup's and organization's default, and every cached identity record; the acting user's own
  records and the task organization's company records are listed at the hub first when none of them is cached
  (`identities.known`). `source` says why it is usable (`own`, `task_org`, `operator`), `signing` whether the body
  holds a signing key;
- `co_authors`: every author of a delivered prompt of the task stored on this device (`acting.instructors`; the task
  owner through the prompt that created the task) whose own primary identity is no copy of the operator's account
  (`people.account`: login and account ID only, never the token), with its login.

Both lists hold at most 100 entries (the contract's bound), in the order above.
"""

from __future__ import annotations

from typing import Any

from agent_runner.credentials import acting, identities
from agent_runner.credentials.cache import CredentialUnavailable, RecordRefused
from agent_runner.credentials.identities import Identity
from agent_runner.github import identity, people, selection
from agent_runner.operations.extensions import (
    CheckFailed,
    OperationContext,
    OperationFailed,
    PrecheckRequest,
    PrecheckResult,
)
from agent_runner.orgs.db import session as store_session
from agent_runner.sync.http import HubRefusal, HubTransportError

MAX_ENTRIES = 100


async def check(request: PrecheckRequest) -> PrecheckResult:
    user = request.operation.actor_id
    if user is None:
        raise CheckFailed("github_not_connected", message=acting.NO_ACTOR)
    try:
        found = await identity.resolve(request)
    except (CheckFailed, HubTransportError):
        return PrecheckResult(data={"acting_user": user})
    return PrecheckResult(data=found.data())


def _entry(found: Identity, source: str, body: dict[str, Any]) -> dict[str, Any] | None:
    named = people.login_of(body)
    if named is None or not identity.valid_token(body):
        return None
    return {
        "id": found.id,
        "login": named[1],
        "owner_kind": found.owner_kind,
        "owner_id": found.owner,
        "primary": found.primary,
        "signing": isinstance(body.get("signing_key"), dict),
        "source": source,
    }


async def _own_primary(ctx: OperationContext) -> dict[str, Any] | None:
    found = await identity.primary(ctx.context, ctx.operation)
    if found is None:
        return None
    try:
        async with acting.opened(ctx.context, ctx.operation, "github") as body:
            meta = Identity(found.identity_id, found.owner, identities.USER, True, found.version, {})
            return _entry(meta, identity.OWN, body)
    except (CredentialUnavailable, HubRefusal):
        return None


async def _named(ctx: OperationContext, user: str, identity_id: str) -> dict[str, Any] | None:
    session = ctx.session
    try:
        found = await identities.meta(ctx.context, identity_id)
        if found is None:
            return None
        book = await identity.loaded(ctx.context)
        source = identity.usable(ctx.context, book, found.owner, found.owner_kind, user=user, org_id=session.org_id)
        if source is None:
            return None
        async with acting.opened_identity(ctx.context, ctx.operation, found) as body:
            return _entry(found, source, body)
    except (CredentialUnavailable, HubRefusal, RecordRefused):
        return None


async def execute(ctx: OperationContext) -> dict[str, Any]:
    session, user = ctx.session, ctx.operation.actor_id
    if user is None:
        raise OperationFailed("github_not_connected", message=acting.NO_ACTOR)
    current = ctx.data.get("github_identity")
    for owner in (user, session.org_id):
        await identities.known(ctx.context, owner)  # listed at the hub when nothing of the owner is cached
    async with store_session(ctx.context.pool) as conn:
        picked = await selection.choices(conn, session)
        wg_default, org_default = await selection.defaults(conn, session.org_id, session.wg_id)
        known = await identities.cached_ids(conn)
        humans = await acting.instructors(conn, session.task_id)
    named = [] if picked is None else [picked.session, picked.task]
    listed: list[dict[str, Any]] = []
    own = await _own_primary(ctx)
    if own is not None:
        listed.append(own)
    for identity_id in (*named, wg_default, org_default, *known):
        if identity_id is None or any(item["id"] == identity_id for item in listed):
            continue
        entry = await _named(ctx, user, identity_id)
        if entry is not None:
            listed.append(entry)
    co_authors = []
    for human in humans:
        found = await people.account(ctx.context, human)
        if found is not None:
            co_authors.append({"user_id": human, "login": found[1]})
    return {
        "acting_user": user,
        "current": current if isinstance(current, str) else None,
        "identities": listed[:MAX_ENTRIES],
        "co_authors": co_authors[:MAX_ENTRIES],
    }
