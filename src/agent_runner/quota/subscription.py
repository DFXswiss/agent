"""Quota polling of Claude subscriptions (05-runner.md section 10.6, 03-api.md section 6.1 `quota` and
`quota_poll_owners`; ADR-010).

`SubscriptionPoller.tick(now)` (task `quota`) polls the usage endpoint `AGENT_RUNNER_ANTHROPIC_USAGE_URL` for every
owner that is due:

- **who**: owners named in `quota_poll_owners` of the last desired state (the hub names this device only when it is
  the online eligible device with the smallest ID; the election is authoritative - the owner's sessions may run on
  another device); an owner whose `claude_subscription` record is not cached here is fetched from the hub (at most
  once per `POLL_S`; `404` or a failed fetch skips the owner until then) after the desired state and the logs were
  refreshed (the owner may be ahead of the device's log copy; while the refresh fails nothing is fetched);
- **when**: every 900 s per owner; after `429` not before `max(Retry-After, 300 s)` (a `Retry-After` above 24 h counts
  as 24 h);
- **how**: `GET` with `Authorization: Bearer <token>`, `anthropic-beta: oauth-2025-04-20`, `Accept:
  application/json`, 8 s timeout, no redirects (a redirect is a failure); the token is decrypted for this request
  only (`credentials.cache.opened`) and never logged, stored or put into an error;
- **feature detection**: the first `401`/`403` marks the credential `quota_unavailable`: that record version is not
  polled again for 24 h (setup tokens may not be accepted by the endpoint) and one outbox item `quota {owner_id,
  unavailable: true, fetched_at}` tells the hub, which drops the owner's older windows, so the web shows "not
  available" instead of an old snapshot (KOR-05). A new version of the credential (a new login) is polled at once;
- **result**: `200` with a valid document -> `parsers.windows_snapshot` -> one outbox item `quota {owner_id, windows,
  fetched_at}` (none when the document reports no window); a malformed document, another status or a transport
  error is logged (status or error type only) and polled again after the interval.
- **limits** (KOR-50, ADR-080 sections 2 and 6): every reading marks the owner's subscription used up, or releases
  it, by the rule of a readable quota (`model.limits.apply`, also without any `429`); towards a hub whose device
  state carries `model_fallback.api` each item carries the latest observed limit of this device (`limited: {at,
  until, readable}`, `runner.access_limit`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from agent_runner.credentials import cache
from agent_runner.model import exhaustion, limits, policy
from agent_runner.model.provision import owner_secret
from agent_runner.orgs.book import book_for
from agent_runner.orgs.db import session as store_session
from agent_runner.quota.parsers import UsageInvalid, windows_snapshot
from agent_runner.quota.reader import ClientFactory, Reading, default_client

__all__ = ["POLL_S", "ClientFactory", "SubscriptionPoller", "default_client", "retry_after"]
from agent_runner.reconcile import logs, state, wire
from agent_runner.reconcile.engine import Run
from agent_runner.sessions.records import time_text
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.sync.runtime import runtime

KIND = "claude_subscription"
POLL_S = 900.0
RATE_LIMIT_MIN_S = 300.0
UNAVAILABLE_S = 24 * 3600.0
OAUTH_BETA = "oauth-2025-04-20"
MAX_RETRY_AFTER_S = 24 * 3600.0
_DELTA_RE = re.compile(r"^[0-9]{1,9}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def retry_after(value: str | None, now: float) -> float:
    """Seconds of a `Retry-After` header (delta seconds or HTTP date), 0 when absent or unreadable."""
    if value is None:
        return 0.0
    text = value.strip()
    if _DELTA_RE.fullmatch(text):
        return min(float(text), MAX_RETRY_AFTER_S)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return 0.0
    return min(max(when.timestamp() - now, 0.0), MAX_RETRY_AFTER_S)


@dataclass
class OwnerState:
    next_at: float = 0.0
    unavailable_version: int | None = None
    unavailable_until: float = 0.0


@dataclass
class SubscriptionPoller:
    context: Any
    url: str
    client: ClientFactory = default_client
    owners: dict[str, OwnerState] = field(default_factory=dict)
    # last fetch attempt of a named owner's record that was not cached (the `now` of `tick`, wall seconds)
    fetched: dict[str, float] = field(default_factory=dict)
    desired: Mapping[str, Any] | None = None

    def line(self, text: str) -> None:
        self.context.log.write(f"runner.quota {text}\n")
        self.context.log.flush()

    async def eligible(self, desired: Mapping[str, Any] | None, now: float) -> list[tuple[str, int]]:
        """`(owner, cached record version)` of the owners this device polls now (module docstring)."""
        listed = [] if desired is None else desired.get("quota_poll_owners")
        named = (
            {item for item in listed if isinstance(item, str) and _UUID_RE.fullmatch(item)}
            if isinstance(listed, list)
            else set()
        )
        for owner in [name for name in self.fetched if name not in named]:
            del self.fetched[owner]
        if not named:
            return []
        polled: list[tuple[str, int]] = []
        refreshed: bool | None = None
        for owner in sorted(named):
            async with store_session(self.context.pool) as conn:
                current = await cache.cached(conn, owner, KIND)
            if current is None and now - self.fetched.get(owner, -POLL_S) >= POLL_S:
                if refreshed is None:
                    refreshed = await self._refresh_logs()
                if not refreshed:
                    continue
                self.fetched[owner] = now
                try:
                    if await cache.fetch(self.context, owner, KIND) is not None:
                        async with store_session(self.context.pool) as conn:
                            current = await cache.cached(conn, owner, KIND)
                except Exception as exc:
                    self.line(f"credential_fetch_failed owner={owner} error={type(exc).__name__}")
            if current is not None:
                polled.append((owner, current[0]))
        return polled

    async def _refresh_logs(self) -> bool:
        """The desired state and the logs up to the hub's heads before an unprompted record is judged: a newly elected
        owner may be ahead of the device's log copy, and a record judged against a stale copy would be refused and
        audited although it is rightful (decisions/KOR-17.md, decisions/ACC-FINAL.md #10)."""
        sync = runtime(self.context)
        if sync is None:  # not enrolled: no hub to ask
            return False
        run = Run(context=self.context, sync=sync, book=book_for(self.context))
        try:
            await state.fetch_state(run)
            if run.halted is None:
                await logs.sync_logs(run)
        except (HubRefusal, HubTransportError, wire.ResponseInvalid) as exc:
            self.line(f"logs_refresh_failed error={type(exc).__name__}")
            return False
        return run.halted is None

    async def tick(self, desired: Mapping[str, Any] | None, now: float) -> None:
        self.desired = desired
        polled = await self.eligible(desired, now)
        for owner in [name for name in self.owners if name not in {o for o, _ in polled}]:
            del self.owners[owner]
        for owner, version in polled:
            state = self.owners.setdefault(owner, OwnerState())
            if state.unavailable_version is not None:
                if state.unavailable_version == version and now < state.unavailable_until:
                    continue
                # a new login (record version) or the 24 h are over: detect the feature again now
            elif now < state.next_at:
                continue
            await self.poll(owner, state, now, version)

    async def _token(self, owner: str) -> str | None:
        try:
            async with cache.opened(self.context, owner, KIND) as body:
                return owner_secret(KIND, body)
        except cache.CredentialUnavailable as exc:
            self.line(f"credential_unusable owner={owner} reason={exc.reason}")
            return None

    async def poll(self, owner: str, state: OwnerState, now: float, version: int) -> None:
        state.next_at = now + POLL_S
        state.unavailable_version = None
        token = await self._token(owner)
        if token is None:
            return
        headers = {"Authorization": f"Bearer {token}", "anthropic-beta": OAUTH_BETA, "Accept": "application/json"}
        try:
            async with self.client() as client:
                response = await client.get(self.url, headers=headers)
        except httpx.HTTPError as exc:
            self.line(f"poll_failed owner={owner} error={type(exc).__name__}")
            return
        await self.answer(owner, state, response, now, version)

    async def answer(self, owner: str, state: OwnerState, response: httpx.Response, now: float, version: int) -> None:
        """Act on the endpoint's answer (module docstring); `version` is the record version whose token was sent."""
        status = response.status_code
        if status in (401, 403):
            state.unavailable_version = version
            state.unavailable_until = now + UNAVAILABLE_S
            self.line(f"quota_unavailable owner={owner} status={status}")
            await limits.apply(self.context, limits.own_key(owner), Reading(readable=False, fetched_at=now))
            await self.enqueue({"owner_id": owner, "unavailable": True, "fetched_at": time_text(now)})
            return
        if status == 429:
            state.next_at = now + max(retry_after(response.headers.get("retry-after"), now), RATE_LIMIT_MIN_S)
            self.line(f"rate_limited owner={owner}")
            return
        if status != 200:
            self.line(f"poll_failed owner={owner} status={status}")
            return
        try:
            windows = windows_snapshot(response.json())
        except (UsageInvalid, ValueError) as exc:
            detail = exc.field if isinstance(exc, UsageInvalid) else "json"
            self.line(f"usage_invalid owner={owner} field={detail}")
            return
        await limits.apply(self.context, limits.own_key(owner), Reading(True, tuple(windows), now))
        if not windows:
            self.line(f"no_windows owner={owner}")
            return
        await self.report(owner, windows, now)

    async def report(self, owner: str, windows: list[dict[str, Any]], now: float) -> None:
        if await self.enqueue({"owner_id": owner, "windows": windows, "fetched_at": time_text(now)}):
            self.line(f"reported owner={owner} windows={len(windows)}")

    async def enqueue(self, item: dict[str, Any]) -> bool:
        """One `quota` outbox item; False without a sync runtime (not enrolled)."""
        sync = runtime(self.context)
        if sync is None:
            return False
        limit = limits.limits_for(self.context).rows.get(limits.own_key(item["owner_id"]))
        if limit is not None and policy.api(self.desired):
            item = {**item, "limited": exhaustion.limited_body(limit)}
        async with store_session(self.context.pool) as conn, conn.transaction():
            await sync.enqueue(conn, "quota", item)
        sync.pusher.notify()
        return True
