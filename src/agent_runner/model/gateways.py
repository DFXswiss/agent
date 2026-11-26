"""The credentials of running sessions' gateways kept current (05-runner.md sections 2.6, 10.1, 10.2; 03-api.md sections
6.1 `platform_budget` and 7.1 `PUT /v1/sessions/{id}/credential`; ADR-010, ADR-026).

A session's gateway holds the credential it was provisioned with at the start (`model.access`). Afterwards:

- **Renewal** (`refresh_all`, reconcile step `model` after the credential step): for every session whose guest holds
  a credential (`RENEWED` states; not while its start or stop runs, not while it resumes - the resume provisions
  itself) the provisioning it should have now is compared with the last one (`access.Stamp`): the owner's record
  version for `subscription`/`api_key` (the record is decrypted only when the version changed), the platform key and
  the organization's daily budget (`platform_budget` of the desired state: limit and spent amount of today, so the
  gateways of several VMs of one organization learn what the others spent; plus the cost of this device's `usage`
  items the hub has not counted yet and the journal position of the session's VM, `model.uncounted`, KOR-10) for
  `platform`. A difference is provisioned again (`PUT .../credential` replaces the credential and restarts the
  gateway's observed cost). A guest that cannot be reached, a platform key file that cannot be read right now or a
  record that does not open on this device yet (its `CK` epoch has not arrived) keep the current provisioning; the
  next run tries again.
- **Withdrawal** (`withdraw`, 05 section 2.6: deleted credentials only): when the owner's record was deleted (purged
  from the cache - the listener registered with `credentials.cache.subscribe` acts at once, the renewal finds a purge
  it missed or one that hit a session whose start was running) the gateway gets a provisioning without secret and
  without allowed models (`provision.withdrawn_credential`: every further request is refused), the session fails
  with `model_access_unavailable` (with its `session.status` event) and its guest session is stopped.
- Renewal and withdrawal of one session run under one lock (`Provisions.lock`) and a withdrawn session is never
  renewed again, so a renewal that decrypted the old record before the purge cannot put it back afterwards.
- **Lost credential** (`credential_missing`, AUD-01 A-04): the gateway's credential lives in the memory of the guest's
  API process, so a restart of `agent-guest-api` loses it and the gateway answers `503` with the journal record
  `guest.error credential_missing`. The journal handler (`sessions/journal.py`) calls `credential_missing`: the
  session's stamp is dropped and the credential provisioned again at once (`reprovision`, a task of its own, under the
  session's lock); a deleted credential withdraws the session (`model_access_unavailable`). A credential that cannot be
  used right now, or a guest that does not take it, is tried again by the next renewal, which sees no stamp. Records
  within `COOLDOWN_S` of a provisioning are stale and ignored.
- **Operator subscriptions** (`model.operator`, KOR-23, ADR-052): a session bound to a token file of the operator's
  pool is renewed from the file, not from an owner record: its stamp carries the file's name and identity, so a
  replaced file (a renewed short-lived token) is provisioned again at the next renewal (the task
  `operator_subscriptions` runs it every few seconds for bound sessions). A bound file that is blocked (`401`/`403`
  for the content the session holds), gone or no longer usable moves the session to another usable file; when none is
  usable the gateway keeps what it holds. A file deferred by a `429` keeps its sessions. When the organization is no
  longer approved the access is withdrawn. The owner's record does not matter for a bound session (a purge of it does
  not withdraw it).
- **Refused access** (HOT-26): every provisioning of a running session (renewal, reprovisioning) is noted for
  `model.revocation`, so a session whose access the provider refused (`401`) is continued once it holds a valid one.
- **Hand-over of a renewal** (`hand_over`, HOT-26, ADR-092 section 5): a renewal on this device puts the new token of
  a shared subscription into the pool and provisions the sessions bound to it at once, before the hub has the new
  record version.
- **Budget refusal of new work** (`platform_budget_spent`): the organization's daily platform budget is spent; used by
  the pre-check of `subagent.request` (operation code `budget_exceeded`); new platform sessions are refused by
  `model.access` (session reason `budget_exceeded`).

Secrets exist only in the request body for one call; stamps keep the record version and a SHA-256 digest of the
platform key, never a secret.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_runner.credentials import cache
from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestError, GuestRefused
from agent_runner.model import access, operator, provision, revocation
from agent_runner.model.access import Stamp, provisions
from agent_runner.orgs.db import session as store_session
from agent_runner.sessions import records
from agent_runner.sessions.env import SessionEnv
from agent_runner.sessions.events import fail
from agent_runner.sessions.records import LocalSession
from agent_runner.sessions.service import ATTRIBUTE as SERVICE_ATTRIBUTE
from agent_runner.sessions.service import SessionService
from agent_runner.sessions.stop import stop_guest
from agent_runner.vm import records as vm_records

LIVE = frozenset({"sandbox_ready", "agent_starting", "prompt_delivering", "working", "idle", "resuming"})
# a resume provisions the session itself (with a possibly different kind): it is withdrawn, never renewed
RENEWED = LIVE - {"resuming"}
KEEP = "keep"
ACCESS_OF = {credential: kind for kind, credential in provision.CREDENTIAL_KINDS.items()}


def _line(env: SessionEnv, text: str) -> None:
    env.log(f"runner.model {text}")


async def target(env: SessionEnv, session: LocalSession) -> tuple[Stamp, dict[str, Any] | None] | str | None:
    """The stamp the session's provisioning should have now and the body when it must be built; None when the access
    is gone (the owner's record was deleted); `KEEP` when it cannot be used right now (platform key file unreadable,
    record that does not open on this device yet, fetch failure): the gateway keeps its credential and the next run
    tries again (05 section 2.6 withdraws deleted credentials only). For an owner credential whose version did not
    change the body is not built (no decryption)."""
    kind, owner = session.model_access, session.owner_id or ""
    if kind == "platform":
        key = env.platform_key(session.org_id)
        if key is None:
            _line(env, f"platform_key_unusable session_id={session.id}")
            return KEEP
        body, budget = await access.platform_body(env, session.org_id, key, session.vm_id)
        stamp = Stamp(
            access=kind, owner_id=owner, key_digest=access.key_digest(key), budget=access.budget_stamp(budget)
        )
        return stamp, body
    name = await operator.bound(env.context, session.id) if kind == "subscription" else None
    if name is not None:
        return await operator_target(env, session, name)
    return await owner_target(env, session)


async def owner_target(env: SessionEnv, session: LocalSession) -> tuple[Stamp, dict[str, Any] | None] | str | None:
    """`target` of a session on the owner's own `subscription` or `api_key` credential."""
    kind, owner = session.model_access, session.owner_id or ""
    credential_kind = provision.CREDENTIAL_KINDS[str(kind)]
    async with store_session(env.db) as conn:
        found = await cache.cached(conn, owner, credential_kind)
    if found is None:
        return None
    known = provisions(env.context).stamps.get(session.id)
    if known is not None and known.version == found[0]:
        return known, None
    opened = await access.owner_secret(env, owner, credential_kind)
    if opened is None:
        return KEEP
    secret, version = opened
    upstream = access.upstream_anthropic(env.context)
    body = provision.owner_credential(kind=str(kind), secret=secret, upstream=upstream, catalog=env.catalog)
    return Stamp(access=str(kind), owner_id=owner, version=version), body


async def operator_target(
    env: SessionEnv, session: LocalSession, name: str
) -> tuple[Stamp, dict[str, Any] | None] | str | None:
    """`target` of a session bound to the operator's token file `name` (module docstring)."""
    pool = await operator.refreshed(env.context)
    assert pool is not None  # `operator.bound` found a binding
    if not pool.serves(session.org_id):
        _line(env, f"operator_access_revoked session_id={session.id}")
        return None
    current = pool.current(name) if pool.allowed(name, session.org_id) else None
    if current is None:
        other = pool.choose(exclude=(name,), org_id=session.org_id)
        if other is None:
            pool.note(f"exhausted:{session.id}", name, f"operator_pool_exhausted session_id={session.id} file={name}")
            return KEEP
        await operator.bind(env.context, pool, session.id, other)
        _line(env, f"operator_file_switched session_id={session.id} from={name} to={other}")
        name, current = other, pool.current(other)
        assert current is not None
    pool.reported.pop(f"exhausted:{session.id}", None)
    token, identity = current
    stamp = Stamp(access="subscription", owner_id=session.owner_id or "", operator=(name, identity))
    known = provisions(env.context).stamps.get(session.id)
    if known == stamp:
        return known, None
    upstream = access.upstream_anthropic(env.context)
    return stamp, provision.owner_credential(kind="subscription", secret=token, upstream=upstream, catalog=env.catalog)


async def put(env: SessionEnv, session: LocalSession, body: Mapping[str, Any]) -> bool:
    """`PUT .../credential` to the session's guest; False when it could not be delivered now."""
    row = None if session.vm_id is None else await vm_records.get(env.db, session.vm_id)
    if row is None or row.state != "running":
        return False
    try:
        async with await env.client(row) as guest:
            await guest.put_credential(session.id, body)
    except GuestRefused as exc:
        budget = body.get("budget")
        if exc.error == "request_invalid" and isinstance(budget, Mapping) and "journal_through" in budget:
            # a guest built from contracts before KOR-10 refuses the field (closed object): without it the gateway
            # keeps its `old spent + observed` bound
            _line(env, f"credential_without_journal_through session_id={session.id}")
            without = {name: value for name, value in budget.items() if name != "journal_through"}
            return await put(env, session, {**body, "budget": without})
        _line(env, f"credential_refused session_id={session.id} error={exc.error}")
        return False
    except (GuestError, RunnerError) as exc:
        _line(env, f"credential_unreachable session_id={session.id} code={exc.code}")
        return False
    return True


async def withdraw(env: SessionEnv, session: LocalSession, cause: str) -> None:
    """Take the access away from a running session (module docstring); serialized with its renewal."""
    found = provisions(env.context)
    async with found.lock(session.id):
        found.withdrawn.add(session.id)
        await _withdraw(env, session, cause)


async def _withdraw(env: SessionEnv, session: LocalSession, cause: str) -> None:
    kind = str(session.model_access)
    upstream = env.upstream_openrouter if kind == "platform" else access.upstream_anthropic(env.context)
    await put(env, session, provision.withdrawn_credential(kind=kind, upstream=upstream))
    provisions(env.context).stamps.pop(session.id, None)
    async with env.transaction() as conn:
        moved = await fail(conn, env.outbox, session.id, access.UNAVAILABLE, at=env.at())
    _line(env, f"access_withdrawn session_id={session.id} access={kind} cause={cause}")
    if moved is not None:
        await stop_guest(env, session.id)


async def refresh(env: SessionEnv, session: LocalSession) -> None:
    found = provisions(env.context)
    async with found.lock(session.id):
        if session.id in found.withdrawn:
            return
        wanted = await target(env, session)
        if wanted is None:
            found.withdrawn.add(session.id)
            await _withdraw(env, session, "credential_deleted")
            return
        if wanted != KEEP:
            assert not isinstance(wanted, str)
            await _renew(env, session, *wanted)


async def _renew(env: SessionEnv, session: LocalSession, stamp: Stamp, body: dict[str, Any] | None) -> None:
    stamps = provisions(env.context).stamps
    if stamps.get(session.id) == stamp:
        return
    assert body is not None  # `target` builds the body whenever the stamp changed
    if await put(env, session, body):
        stamps[session.id] = stamp
        if stamp.operator is not None:
            operator.delivered(env.context, session.id)
        revocation.provisioned(env.context, session.id, env.clock())
        _line(env, f"credential_renewed session_id={session.id} access={stamp.access}")


def running_service(context: Any) -> SessionService | None:
    """The session service of the daemon once the session area built it (never builds one)."""
    found = getattr(context, SERVICE_ATTRIBUTE, None)
    return found if isinstance(found, SessionService) else None


async def live_sessions(env: SessionEnv, busy: set[str], states: frozenset[str] = LIVE) -> list[LocalSession]:
    async with env.transaction() as conn:
        found = await records.listed(conn)
    return [s for s in found if s.state in states and s.model_access is not None and s.id not in busy]


async def refresh_all(env: SessionEnv, busy: set[str]) -> None:
    """Renew or withdraw the credential of every live session; the state kept for other sessions is dropped. A
    session whose start runs (`busy`) is renewed - or withdrawn when its record was deleted meanwhile - on the first
    run after its start ended."""
    live = await live_sessions(env, busy, RENEWED)
    found = provisions(env.context)
    keep = {s.id for s in await live_sessions(env, set())} | busy
    for stale in [sid for sid in found.stamps if sid not in keep]:
        del found.stamps[stale]
    done_at = reprovisions(env.context).done_at
    for stale in [sid for sid in done_at if sid not in keep]:
        del done_at[stale]
    for stale in [sid for sid in found.locks if sid not in keep and not found.locks[sid].locked()]:
        del found.locks[stale]
    found.withdrawn &= keep
    await operator.prune(env.context, keep)
    for session in live:
        try:
            await refresh(env, session)
        except Exception as exc:
            _line(env, f"refresh_failed session_id={session.id} error={type(exc).__name__}")


async def on_purge(context: Any, owner: str, credential_kind: str) -> None:
    """Listener of the credential cache: a deleted owner credential is withdrawn from that owner's sessions."""
    kind = ACCESS_OF.get(credential_kind)
    service = running_service(context)
    if kind is None or service is None:
        return
    for session in await live_sessions(service.env, set(service.tasks)):
        if session.owner_id != owner or session.model_access != kind:
            continue
        if await operator.bound(context, session.id) is None:
            await withdraw(service.env, session, "credential_deleted")


def subscribe(context: Any) -> None:
    """Register `on_purge` once per daemon."""
    found = provisions(context)
    if found.subscribed:
        return

    async def listener(owner: str, credential_kind: str) -> None:
        await on_purge(context, owner, credential_kind)

    cache.subscribe(context, listener)
    found.subscribed = True


async def hand_over(context: Any, record_id: str, version: int, token: str) -> None:
    """A renewal of this device (`quota.renewal`, module docstring): the new token of the shared subscription
    `sub-<record_id>` becomes the pool's entry with the identity of the record version that will hold it, and the
    sessions bound to it are provisioned at once. Without a pool, without that entry, with a token the pool would not
    take, or without the session area nothing happens: the pool reads the record once the hub has it."""
    pool = operator.pool_for(context)
    service = running_service(context)
    name = operator.SHARED_PREFIX + record_id
    if pool is None or service is None or name not in pool.shared or operator.parse_token(token.encode()) is None:
        return
    pool.shared[name] = operator.Entry((0, 0, version, 0), token, None)
    env = service.env
    for session in await live_sessions(env, set(service.tasks), RENEWED):
        if pool.bindings.get(session.id) == name:
            await refresh(env, session)


def platform_budget_spent(context: Any, org_id: str) -> bool:
    """The organization's daily platform budget (last desired state) is spent."""
    service = running_service(context)
    desired = None if service is None else service.env.desired
    return provision.budget_exhausted(provision.platform_budget(desired, org_id))


# -- a gateway without credential (guest restart) --------------------------------------------------------------------

REPROVISIONS = "_model_reprovisions"
COOLDOWN_S = 10.0
"""After a provisioning the guest's API process takes the credential with the session's next request; the records of
the requests it refused before (Claude Code retries `503`) are stale and change nothing for this long."""


@dataclass
class Reprovisions:
    """Per daemon: the running provisioning task and the time of the last provisioning of each session."""

    tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    done_at: dict[str, float] = field(default_factory=dict)


def reprovisions(context: Any) -> Reprovisions:
    found = getattr(context, REPROVISIONS, None)
    if isinstance(found, Reprovisions):
        return found
    created = Reprovisions()
    setattr(context, REPROVISIONS, created)
    return created


def credential_missing(env: SessionEnv, session: LocalSession) -> None:
    """The session's gateway reported `guest.error credential_missing` (its API process restarted and lost the
    credential, which lives in its memory only): provision it again at once, in a task of its own - never inside the
    journal's transaction, whose locks the failure path would wait for. One task per session at a time; a record
    within `COOLDOWN_S` of the last provisioning is stale (the guest is untrusted: it cannot force repeated
    decryption and provisioning)."""
    state = reprovisions(env.context)
    last = state.done_at.get(session.id)
    if session.id in state.tasks or (last is not None and env.clock() - last < COOLDOWN_S):
        return
    task = asyncio.ensure_future(reprovision(env, session.id))
    state.tasks[session.id] = task

    def done(finished: asyncio.Task[None]) -> None:
        state.tasks.pop(session.id, None)
        if not finished.cancelled() and finished.exception() is not None:
            _line(env, f"reprovision_failed session_id={session.id} error={type(finished.exception()).__name__}")

    task.add_done_callback(done)


async def reprovision(env: SessionEnv, session_id: str) -> None:
    """Drop the session's stamp and provision the credential it should have now. A deleted credential withdraws the
    session (`model_access_unavailable`); one that cannot be used right now (`KEEP`: platform key unreadable, record
    not opening or not fetched now) and a guest that does not take it now leave the stamp dropped, so the next renewal
    provisions it. Sessions whose start or stop runs, that were withdrawn or that are not in a renewed state are left
    alone."""
    service = running_service(env.context)
    found = provisions(env.context)
    async with found.lock(session_id):
        if service is not None and session_id in service.tasks:
            return
        async with env.transaction() as conn:
            session = await records.get(conn, session_id)
        if session_id in found.withdrawn or session is None or session.state not in RENEWED:
            return
        if session.model_access is None:
            return
        found.stamps.pop(session_id, None)
        wanted = await target(env, session)
        if wanted is None:
            found.withdrawn.add(session_id)
            await _withdraw(env, session, "credential_missing")
            return
        if isinstance(wanted, str):
            _line(env, f"reprovision_deferred session_id={session_id}")
            return
        stamp, body = wanted
        assert body is not None  # no stamp: the body is built
        if await put(env, session, body):
            found.stamps[session_id] = stamp
            if stamp.operator is not None:
                operator.delivered(env.context, session_id)
            reprovisions(env.context).done_at[session_id] = env.clock()
            revocation.provisioned(env.context, session_id, env.clock())
            _line(env, f"credential_reprovisioned session_id={session_id} access={stamp.access}")
