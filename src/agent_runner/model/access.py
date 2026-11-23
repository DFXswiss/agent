"""Model access of a session start: selection, credential and gateway provisioning body (05-runner.md sections 6.1
step 3, 10.1-10.3, 2.6; 03-api.md section 7.1; ADR-010, ADR-026).

`resolve(env, desired)` is called by the session start (and the resume) of the session area. It walks the kinds the
owner's preference allows (`selection.order`: `auto` = subscription -> API key -> platform, an explicit preference only
its own kind) and takes the first kind this device can serve:

- `subscription` / `api_key`: the owner's `claude_subscription` / `anthropic_api_key` credential opens on this device
  (`credentials.cache.opened`: cached record, else fetched on demand; verified, decrypted for this call only). A
  missing record, a record this device cannot open (no `CK`), a malformed body or a hub/transport failure while
  fetching makes the kind unavailable;
- `platform`: a platform device with a usable `OPENROUTER_KEY_FILE`. When the organization has spent its daily
  platform budget (`platform_budget` of `GET /v1/device/state`) the start is refused with `budget_exceeded` (the
  gateway refuses further requests of running sessions itself with `429 budget_exceeded`).

Nothing available: `AccessRefused("model_access_unavailable")` (no silent fallback for an explicit preference). The
result carries the `Choice` (kind, model, model environment of 05 section 10.3) and the body of `PUT
.../credential` (`model.provision`), which holds the secret: it is handed to the guest and dropped, never logged or
stored. What was provisioned (kind, owner, record version, budget, a digest of the platform key - never the key) is
remembered per daemon (`Provisions`) so the refresh of `model.gateways` sees changes.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from agent_runner.credentials import cache
from agent_runner.errors import RunnerError
from agent_runner.model import provision, selection, uncounted
from agent_runner.model.selection import Choice
from agent_runner.orgs.db import session as store_session
from agent_runner.sessions.desired import Desired

UNAVAILABLE = "model_access_unavailable"
UNKNOWN_VERSION = -1
"""Stamp version when the record changed while it was opened: the next renewal provisions again."""
BUDGET = "budget_exceeded"
DEFAULT_UPSTREAM_ANTHROPIC = "https://api.anthropic.com"
ATTRIBUTE = "model_provisions"


class AccessRefused(Exception):
    """No model access for the session: `reason` is a session failure reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Stamp:
    """What a provisioning was built from (no secret): compared by the refresh."""

    access: str
    owner_id: str
    version: int | None = None
    key_digest: str | None = None
    budget: tuple[float, float] | None = None


@dataclass
class Provisions:
    """Session ID -> `Stamp` of its last provisioning, per daemon."""

    stamps: dict[str, Stamp] = field(default_factory=dict)
    subscribed: bool = False
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    """One lock per session: renewal and withdrawal of a session never interleave (`model.gateways`)."""
    withdrawn: set[str] = field(default_factory=set)

    def lock(self, session_id: str) -> asyncio.Lock:
        return self.locks.setdefault(session_id, asyncio.Lock())


def provisions(context: Any) -> Provisions:
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Provisions):
        return found
    created = Provisions()
    setattr(context, ATTRIBUTE, created)
    return created


@dataclass(frozen=True)
class Resolved:
    choice: Choice
    credential: dict[str, Any]
    stamp: Stamp

    def __repr__(self) -> str:
        return f"Resolved(access={self.choice.access!r}, model_id={self.choice.model_id!r}, credential=<redacted>)"


def upstream_anthropic(context: Any) -> str:
    """`AGENT_RUNNER_MODEL_UPSTREAM_ANTHROPIC` of the daemon configuration."""
    config = getattr(context, "config", None)
    value = getattr(config, "model_upstream_anthropic", None)
    return value if isinstance(value, str) else DEFAULT_UPSTREAM_ANTHROPIC


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def budget_stamp(budget: Mapping[str, Any] | None) -> tuple[float, float] | None:
    """What a renewal compares: the limit and the amount (`journal_through` alone does not renew)."""
    return None if budget is None else (budget["usd_per_day"], budget["spent_usd"])


def _line(env: Any, text: str) -> None:
    env.log(f"runner.model {text}")


async def owner_secret(env: Any, owner: str, credential_kind: str) -> tuple[str, int] | None:
    """`(secret, record version)` of the owner's credential when it opens on this device, else None. The version is
    the cached one before and after opening; when they differ (a new version arrived meanwhile) it is
    `UNKNOWN_VERSION`, so the stamp never claims a version the gateway does not hold."""
    context = env.context
    if context is None:
        return None
    try:
        async with store_session(context.pool) as conn:
            before = await cache.cached(conn, owner, credential_kind)
        async with cache.opened(context, owner, credential_kind) as body:
            secret = provision.owner_secret(credential_kind, body)
        async with store_session(context.pool) as conn:
            found = await cache.cached(conn, owner, credential_kind)
    except cache.CredentialUnavailable as exc:
        if exc.reason != "missing":
            _line(env, f"credential_unusable owner={owner} kind={credential_kind} reason={exc.reason}")
        return None
    except Exception as exc:
        # hub refusal or transport error while fetching on demand: unavailable for this start
        _line(env, f"credential_fetch_failed owner={owner} kind={credential_kind} error={type(exc).__name__}")
        return None
    if secret is None:
        _line(env, f"credential_unusable owner={owner} kind={credential_kind} reason=body")
        return None
    if found is None or (before is not None and before[0] != found[0]):
        return secret, UNKNOWN_VERSION
    return secret, found[0]


def _select(env: Any, request: selection.Request) -> Choice | None:
    try:
        return selection.select(request, catalog=env.catalog, strategies=env.strategies)
    except RunnerError:
        return None


async def platform_body(
    env: Any, org_id: str, key: str, vm_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The platform provisioning with the budget of the organization: the hub's amount plus what it has not counted
    yet, and the journal position of the session's VM (`model.uncounted`, KOR-10)."""
    desired = env.desired  # one snapshot: the reconcile step may replace it while the store is read
    budget = await uncounted.budget(env, desired, provision.platform_budget(desired, org_id), org_id, vm_id)
    body = provision.platform_credential(key=key, upstream=env.upstream_openrouter, catalog=env.catalog, budget=budget)
    return body, budget


async def resolve(env: Any, desired: Desired) -> Resolved:
    """The model access of a session start (module docstring); `AccessRefused` when there is none."""
    try:
        kinds = selection.order(desired.model_access_pref)
    except RunnerError:
        raise AccessRefused(UNAVAILABLE) from None
    base = selection.Request(
        preference=desired.model_access_pref,
        model_pref=desired.model_id_pref,
        device_kind=env.device_kind,
        owner_id=desired.owner_id,
    )
    for kind in kinds:
        if kind == "platform":
            key = env.platform_key() if env.device_kind == "platform" else None
            if key is None:
                continue
            choice = _select(env, replace(base, preference=kind, platform_key_available=True))
            if choice is None:
                continue
            body, budget = await platform_body(env, desired.org_id, key)
            if provision.budget_exhausted(budget):
                raise AccessRefused(BUDGET)
            stamp = Stamp(
                access=kind, owner_id=desired.owner_id, key_digest=key_digest(key), budget=budget_stamp(budget)
            )
        else:
            found = await owner_secret(env, desired.owner_id, provision.CREDENTIAL_KINDS[kind])
            if found is None:
                continue
            secret, version = found
            choice = _select(env, replace(base, preference=kind, credentials=frozenset({kind})))
            if choice is None:
                continue
            upstream = upstream_anthropic(env.context)
            body = provision.owner_credential(kind=kind, secret=secret, upstream=upstream, catalog=env.catalog)
            stamp = Stamp(access=kind, owner_id=desired.owner_id, version=version)
        if env.context is not None:
            provisions(env.context).stamps[desired.session_id] = stamp
        return Resolved(choice=choice, credential=body, stamp=stamp)
    raise AccessRefused(UNAVAILABLE)
