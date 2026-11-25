"""Quota polling of further Claude subscriptions (KOR-69, ADR-090 section 4; 05-runner.md section 10.6).

`FurtherPoller.tick(desired, now)` (task `quota`, beside `subscription.SubscriptionPoller` for the primary records)
measures every further subscription of the owners this device polls (`quota_poll_owners`), only towards a hub whose
device state carries `subscriptions.api >= 1` (an older hub would refuse the item form):

- **which**: the owner's records are listed at the hub (`credentials.subscriptions.sync_owner`) when the owner is new
  here and then every `POLL_S`; every cached non-primary record of the owner is measured. Before it is measured, a
  record under an older `CK` epoch than the newest one this device holds is re-sealed (`subscriptions.reseal`), so a
  rotation of the owner's key by a client that does not know further subscriptions leaves none behind (a try that
  wrote nothing is not repeated within `POLL_S`);
- **when**: every `POLL_S` per record; after `429` not before `max(Retry-After, 300 s)`; a record whose token the usage
  endpoint refused is not polled again for `UNAVAILABLE_S` unless its version changed (a new login);
- **how**: as for the primary record (`Authorization: Bearer`, `anthropic-beta: oauth-2025-04-20`, 8 s, no redirects),
  the token decrypted for this request only and never logged; a record without a token (not logged in) or that does
  not open is skipped and logged by ID;
- **result**: `quota {owner_id, subscription_id, windows, fetched_at}` for a valid document with windows,
  `quota {owner_id, subscription_id, unavailable: true, fetched_at}` for `401`/`403`; nothing else is sent. The
  readings mark no limit: further subscriptions serve no session of the owner.
- **renewal** (KOR-71, ADR-091 section 2; `renewal.Renewer`): on every tick, first every renewal this device stored
  but the hub has not confirmed is written again (`renewal.flush`, also for owners this device no longer measures);
  then a full login (`kind: "oauth_login"` with both tokens) of a measured owner is renewed `RENEW_BEFORE_S` before
  its `expires_at`, and a `401` of the usage endpoint for such a record renews it at once instead of marking it not
  readable (a record that was renewed is measured at the next tick, with the new token). The expiry of each record
  version is read once (`expiry`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from agent_runner.credentials import subscriptions
from agent_runner.credentials.cache import CredentialUnavailable
from agent_runner.orgs.db import session as store_session
from agent_runner.quota import renewal
from agent_runner.quota.parsers import UsageInvalid, windows_snapshot
from agent_runner.quota.reader import ClientFactory, default_client
from agent_runner.quota.subscription import (
    OAUTH_BETA,
    POLL_S,
    RATE_LIMIT_MIN_S,
    UNAVAILABLE_S,
    OwnerState,
    retry_after,
)
from agent_runner.sessions.records import time_text
from agent_runner.sync.runtime import runtime

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def api(desired: Mapping[str, Any] | None) -> bool:
    """The hub announces `subscriptions.api >= 1` in its device state."""
    found = None if desired is None else desired.get("subscriptions")
    version = found.get("api") if isinstance(found, Mapping) else None
    return isinstance(version, int) and not isinstance(version, bool) and version >= 1


def owners(desired: Mapping[str, Any] | None) -> list[str]:
    """The well-formed owners of `quota_poll_owners`, sorted."""
    listed = [] if desired is None else desired.get("quota_poll_owners")
    if not isinstance(listed, list):
        return []
    return sorted({item for item in listed if isinstance(item, str) and _UUID_RE.fullmatch(item)})


@dataclass
class FurtherPoller:
    context: Any
    url: str
    client: ClientFactory = default_client
    listed: dict[str, float] = field(default_factory=dict)
    """Owner -> time of its last list."""
    records: dict[str, OwnerState] = field(default_factory=dict)
    """Record ID -> its schedule."""
    reseal_after: dict[str, float] = field(default_factory=dict)
    """Record ID -> when a re-seal may be tried again (one try per `POLL_S` that wrote nothing)."""
    expiry: dict[str, tuple[int, float | None]] = field(default_factory=dict)
    """Record ID -> (version, expiry of its access token or None when it is no renewable full login)."""
    renewer: renewal.Renewer | None = None

    def line(self, text: str) -> None:
        self.context.log.write(f"runner.quota {text}\n")
        self.context.log.flush()

    def renewals(self) -> renewal.Renewer:
        if self.renewer is None:
            self.renewer = renewal.Renewer(context=self.context)
        self.renewer.client = self.client
        return self.renewer

    async def renew_due(self, item: subscriptions.Subscription, now: float, *, force: bool = False) -> bool:
        """Renew a full login when it is due (module docstring); True when a new version was written."""
        known = self.expiry.get(item.id)
        if known is None or known[0] != item.version:
            try:
                async with subscriptions.opened(self.context, item) as body:
                    known = (item.version, renewal.expires(body))
            except CredentialUnavailable:
                known = (item.version, None)
            self.expiry[item.id] = known
        if known[1] is None or not (force or now >= known[1] - renewal.RENEW_BEFORE_S):
            return False
        return await self.renewals().renew(item, now, force=force)

    async def tick(self, desired: Mapping[str, Any] | None, now: float) -> None:
        named = owners(desired) if api(desired) else []
        if api(desired):
            for waiting in await renewal.pending(self.context):
                await self.renewals().renew(
                    subscriptions.Subscription(waiting.id, waiting.owner, False, waiting.base_version, {}), now
                )
        for owner in [name for name in self.listed if name not in named]:
            del self.listed[owner]
        seen: set[str] = set()
        for owner in named:
            if now - self.listed.get(owner, -POLL_S) >= POLL_S:
                self.listed[owner] = now
                await subscriptions.sync_owner(self.context, owner)
            async with store_session(self.context.pool) as conn:
                found = [item for item in await subscriptions.cached_of(conn, owner) if not item.primary]
            for item in found:
                seen.add(item.id)
                if now >= self.reseal_after.get(item.id, float("-inf")):
                    if await subscriptions.reseal(self.context, item):
                        continue  # measured at the next tick, with the re-sealed version
                    self.reseal_after[item.id] = now + POLL_S
                if await self.renew_due(item, now):
                    continue  # measured at the next tick, with the new token
                await self.due(item, now)
        for record_id in [name for name in self.records if name not in seen]:
            del self.records[record_id]
        for record_id in [name for name in self.reseal_after if name not in seen]:
            del self.reseal_after[record_id]
        for record_id in [name for name in self.expiry if name not in seen]:
            del self.expiry[record_id]

    async def due(self, item: subscriptions.Subscription, now: float) -> None:
        state = self.records.setdefault(item.id, OwnerState())
        if state.unavailable_version is not None:
            if state.unavailable_version == item.version and now < state.unavailable_until:
                return
        elif now < state.next_at:
            return
        await self.poll(item, state, now)

    async def _token(self, item: subscriptions.Subscription) -> str | None:
        try:
            async with subscriptions.opened(self.context, item) as body:
                token = body.get("token")
        except CredentialUnavailable as exc:
            self.line(f"subscription_unusable id={item.id} reason={exc.reason}")
            return None
        if not isinstance(token, str) or not token:
            self.line(f"subscription_logged_out id={item.id}")
            return None
        return token

    async def poll(self, item: subscriptions.Subscription, state: OwnerState, now: float) -> None:
        state.next_at = now + POLL_S
        state.unavailable_version = None
        token = await self._token(item)
        if token is None:
            return
        headers = {"Authorization": f"Bearer {token}", "anthropic-beta": OAUTH_BETA, "Accept": "application/json"}
        try:
            async with self.client() as client:
                response = await client.get(self.url, headers=headers)
        except httpx.HTTPError as exc:
            self.line(f"poll_failed id={item.id} error={type(exc).__name__}")
            return
        finally:
            headers.clear()
        await self.answer(item, state, response, now)

    async def answer(
        self, item: subscriptions.Subscription, state: OwnerState, response: httpx.Response, now: float
    ) -> None:
        status = response.status_code
        base = {"owner_id": item.owner, "subscription_id": item.id}
        if status == 401 and await self.renew_due(item, now, force=True):
            state.next_at = now  # measured again at the next tick, with the new token
            return
        if status in (401, 403):
            state.unavailable_version = item.version
            state.unavailable_until = now + UNAVAILABLE_S
            self.line(f"quota_unavailable id={item.id} status={status}")
            await self.enqueue({**base, "unavailable": True, "fetched_at": time_text(now)})
            return
        if status == 429:
            state.next_at = now + max(retry_after(response.headers.get("retry-after"), now), RATE_LIMIT_MIN_S)
            self.line(f"rate_limited id={item.id}")
            return
        if status != 200:
            self.line(f"poll_failed id={item.id} status={status}")
            return
        try:
            windows = windows_snapshot(response.json())
        except (UsageInvalid, ValueError) as exc:
            detail = exc.field if isinstance(exc, UsageInvalid) else "json"
            self.line(f"usage_invalid id={item.id} field={detail}")
            return
        if not windows:
            self.line(f"no_windows id={item.id}")
            return
        await self.enqueue({**base, "windows": windows, "fetched_at": time_text(now)})
        self.line(f"reported id={item.id} windows={len(windows)}")

    async def enqueue(self, item: dict[str, Any]) -> None:
        sync = runtime(self.context)
        assert sync is not None  # the task runs on an enrolled device only
        async with store_session(self.context.pool) as conn, conn.transaction():
            await sync.enqueue(conn, "quota", item)
        sync.pusher.notify()
