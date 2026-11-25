"""Renewal of further Claude subscriptions by the device that measures their owner (KOR-71, ADR-091 section 2;
05-runner.md section 10.6).

A further subscription logged in with the full login (`kind: "oauth_login"`, `login/full_login.py`) holds a
short-lived access token (`token`, `expires_at`) and a refresh token. The `quota` task of the device that measures
the owner (`further.FurtherPoller`, owners of `quota_poll_owners` - never two devices at once) calls `Renewer.renew`
for such a record `RENEW_BEFORE_S` before `expires_at` and at once after the usage endpoint answered `401`:

1. **Fresh record.** The record is fetched anew (`subscriptions.fetch`); a record without both tokens (not logged
   in, logged out by its owner, refused before) or whose fresh version is not due (a newer login) is not renewed.
2. **Token endpoint.** `POST CLAUDE_TOKEN_URL` with JSON `{grant_type: "refresh_token", refresh_token, client_id:
   CLAUDE_OAUTH_CLIENT_ID, scope}` (the record's scopes), `TIMEOUT_S`, no redirects - the request the pinned Claude
   Code renews with. Neither the request nor the answer is logged; a failure logs its status or error type only.
3. **Store first.** A `200` with an access token and `expires_in` gives the next body (new `token`, the new
   `refresh_token` or the old one when the answer has none, `expires_at`, `scopes` when the answer names them;
   `relogin` dropped). It is sealed as the next record version (`subscriptions.seal`) and kept in
   `runner.subscription_renewal` together with the sealed record it replaces - before anything else happens. Only
   then is it written (`flush`).
4. **Write** (`flush`, also on every tick while a row exists - the token endpoint is not called again for the record
   meanwhile): the opened row is sealed anew under the newest epoch and written with `PUT
   /v1/device/subscriptions/{id}` without a request (`subscriptions._put`). Written → the row goes. `version_conflict`
   → the current record decides: does it still hold the refresh token the renewal replaced (an owner changed its
   name, note or use), or was it refused meanwhile (`relogin`, a renewal of another device that lost the race), the
   new tokens are merged into it (its other fields win) and the row is kept for the next try; otherwise a newer login
   won and the row goes. A gone record (`404`) drops the row; a transport error, `forbidden` (the device no longer
   measures the owner and did not write the stored version) or a missing key keep it for the next tick.
5. **`invalid_grant`.** A `400`/`401` whose JSON names `error: "invalid_grant"` means the login is dead: the record is
   written as its next version without `token` and `refresh_token`, with `relogin: "renewal_refused"` ("log in
   again"), but only while the current record still holds the refused refresh token. Any other answer, a malformed
   one or a network error is tried again after `RETRY_S`.

The tokens exist in memory only for the moment of a request or a seal; the store holds ciphertext only. A guest never
gets the refresh token (`model.operator` hands out `token` alone).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from psycopg.types.json import Jsonb

from agent_runner.credentials import subscriptions
from agent_runner.credentials.cache import PUT_ATTEMPTS, CredentialUnavailable, RecordRefused
from agent_runner.credentials.subscriptions import Subscription
from agent_runner.login.full_login import LoginUnusable, scopes_of
from agent_runner.orgs.db import session as store_session
from agent_runner.quota.reader import ClientFactory, default_client
from agent_runner.sessions.records import time_text
from agent_runner.sync.http import HubRefusal, HubTransportError

KIND = "oauth_login"
RENEW_BEFORE_S = 1800.0
"""Renew this long before the access token expires (sessions on other devices learn of it through the hint)."""
RETRY_S = 60.0
TIMEOUT_S = 30.0
MAX_EXPIRES_IN = 366 * 86400
TOKEN_RE = re.compile(r"^[!-~]{1,4096}$")
TOKEN_FIELDS = ("token", "refresh_token", "expires_at", "scopes")
RELOGIN = "renewal_refused"


@dataclass(frozen=True)
class Pending:
    """A renewal not yet confirmed by the hub (a row of `runner.subscription_renewal`)."""

    id: str
    owner: str
    base_version: int
    base_record: Mapping[str, Any]
    record: Mapping[str, Any]


def renewable(body: Mapping[str, object]) -> bool:
    """A full login that holds both tokens."""
    return (
        body.get("kind") == KIND
        and isinstance(body.get("token"), str)
        and isinstance(body.get("refresh_token"), str)
        and isinstance(body.get("expires_at"), str)
    )


def expires(body: Mapping[str, object]) -> float | None:
    """The expiry of the access token (seconds), None when the body is no renewable login."""
    if not renewable(body):
        return None
    try:
        return datetime.fromisoformat(str(body["expires_at"])).timestamp()
    except ValueError:
        return None


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.renewal {text}\n")
    context.log.flush()


async def pending(context: Any, record_id: str | None = None) -> list[Pending]:
    """The rows of not yet confirmed renewals (of one record), by ID."""
    query = "SELECT id::text, owner_id::text, base_version, base_record, record FROM runner.subscription_renewal"
    params: tuple[object, ...] = ()
    if record_id is not None:
        query, params = query + " WHERE id = %s", (record_id,)
    async with store_session(context.pool) as conn:
        rows = await (await conn.execute(query + " ORDER BY id", params)).fetchall()
    return [Pending(str(row[0]), str(row[1]), int(row[2]), row[3], row[4]) for row in rows]


async def _keep(context: Any, item: Pending) -> None:
    async with store_session(context.pool) as conn:
        await conn.execute(
            "INSERT INTO runner.subscription_renewal (id, owner_id, base_version, base_record, record)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET base_version = EXCLUDED.base_version,"
            " base_record = EXCLUDED.base_record, record = EXCLUDED.record",
            (item.id, item.owner, item.base_version, Jsonb(dict(item.base_record)), Jsonb(dict(item.record))),
        )


async def _drop(context: Any, record_id: str, reason: str) -> None:
    async with store_session(context.pool) as conn:
        await conn.execute("DELETE FROM runner.subscription_renewal WHERE id = %s", (record_id,))
    _line(context, f"pending_dropped id={record_id} reason={reason}")


def _record(item: Pending, *, base: bool) -> Subscription:
    record = item.base_record if base else item.record
    version = item.base_version if base else item.base_version + 1
    return Subscription(item.id, item.owner, False, version, record)


async def _body(context: Any, found: Subscription) -> dict[str, Any]:
    async with subscriptions.opened(context, found) as body:
        return dict(body)


def _merge(current: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    merged = {name: value for name, value in current.items() if name != "relogin"}
    merged.update({name: new[name] for name in TOKEN_FIELDS if name in new})
    return merged


async def flush(context: Any, record_id: str) -> bool:
    """Write the pending renewal of the record (module docstring, step 4); True when no row remains."""
    for _attempt in range(PUT_ATTEMPTS):
        found = await pending(context, record_id)
        if not found:
            return True
        item = found[0]
        body: dict[str, Any] = {}
        try:
            body = await _body(context, _record(item, base=False))
            stored = await subscriptions._put(context, item.owner, item.id, item.base_version, body, request_id=None)
            if stored is not None:
                await _drop(context, item.id, "written")
                _line(context, f"renewed id={item.id} version={stored}")
                return True
            if not await _rebase(context, item, body):
                return True
        except (CredentialUnavailable, HubTransportError, HubRefusal, RecordRefused) as exc:
            return await _failed(context, item.id, exc)
        finally:
            body.clear()
    return False


async def _failed(context: Any, record_id: str, exc: Exception) -> bool:
    """A write that failed: a gone record drops the row (True), anything else keeps it (False)."""
    if isinstance(exc, CredentialUnavailable) and exc.code == "not_found":
        await _drop(context, record_id, "gone")
        return True
    detail = f"reason={exc.reason} code={exc.code}" if isinstance(exc, CredentialUnavailable) else ""
    _line(context, f"pending_kept id={record_id} error={type(exc).__name__} {detail}".rstrip())
    return False


async def _rebase(context: Any, item: Pending, body: Mapping[str, Any]) -> bool:
    """After a `version_conflict`: merge into the current record (True, the row is kept) or drop the row (False)."""
    current = await subscriptions.fetch(context, item.id)
    if current is None or current.primary or current.owner != item.owner:
        await _drop(context, item.id, "gone")
        return False
    base, now = await _body(context, _record(item, base=True)), await _body(context, current)
    try:
        replaced = base.get("refresh_token")
        if now.get("refresh_token") != replaced and not (now.get("relogin") == RELOGIN and "refresh_token" not in now):
            await _drop(context, item.id, "newer_login")
            return False
        merged = _merge(now, body)
        sealed = await subscriptions.seal(context, item.owner, item.id, current.version, merged)
        merged.clear()
        await _keep(context, Pending(item.id, item.owner, current.version, current.record, sealed))
        return True
    finally:
        base.clear()
        now.clear()


@dataclass
class Renewer:
    """The renewals of one device (module docstring)."""

    context: Any
    client: ClientFactory = default_client
    retry_at: dict[str, float] = field(default_factory=dict)
    """Record ID -> when a failed renewal may be tried again."""

    async def renew(self, item: Subscription, now: float, *, force: bool = False) -> bool:
        """Renew the record when its fresh version is still due (`force`: after a `401`) (module docstring); True when
        a new version was written or the record was marked."""
        if now < self.retry_at.get(item.id, float("-inf")):
            return False
        if await pending(self.context, item.id):
            return await self._flush(item.id, now)
        try:
            current = await subscriptions.fetch(self.context, item.id)
            if current is None or current.primary:
                return False
            body = await _body(self.context, current)
        except (CredentialUnavailable, RecordRefused, HubRefusal, HubTransportError) as exc:
            _line(self.context, f"renew_failed id={item.id} stage=record error={type(exc).__name__}")
            self.retry_at[item.id] = now + RETRY_S
            return False
        try:
            expiry = expires(body)
            if expiry is None or not (force or now >= expiry - RENEW_BEFORE_S):
                return False
            return await self._exchange(current, body, now)
        finally:
            body.clear()

    async def _flush(self, record_id: str, now: float) -> bool:
        if await flush(self.context, record_id):
            self.retry_at.pop(record_id, None)
            return True
        self.retry_at[record_id] = now + RETRY_S
        return False

    async def _exchange(self, current: Subscription, body: dict[str, Any], now: float) -> bool:
        config = self.context.config
        request = {
            "grant_type": "refresh_token",
            "refresh_token": body["refresh_token"],
            "client_id": config.claude_oauth_client_id,
            "scope": " ".join(body.get("scopes") or []),
        }
        try:
            async with self.client() as client:
                response = await client.post(config.claude_token_url, json=request, timeout=TIMEOUT_S)
        except httpx.HTTPError as exc:
            _line(self.context, f"renew_failed id={current.id} error={type(exc).__name__}")
            self.retry_at[current.id] = now + RETRY_S
            return False
        finally:
            request.clear()
        answer = _json(response)
        try:
            if response.status_code in (400, 401) and answer.get("error") == "invalid_grant":
                return await self._refused(current, body["refresh_token"], now)
            fields = _fields(answer, now) if response.status_code == 200 else None
            if fields is None:
                _line(self.context, f"renew_failed id={current.id} status={response.status_code}")
                self.retry_at[current.id] = now + RETRY_S
                return False
            fields.setdefault("refresh_token", body["refresh_token"])
            new_body = _merge(body, fields)
            fields.clear()
            try:
                sealed = await subscriptions.seal(self.context, current.owner, current.id, current.version, new_body)
            except CredentialUnavailable as exc:
                _line(self.context, f"renew_failed id={current.id} stage=seal reason={exc.reason}")
                self.retry_at[current.id] = now + RETRY_S
                return False
            finally:
                new_body.clear()
            await _keep(self.context, Pending(current.id, current.owner, current.version, current.record, sealed))
            _line(self.context, f"renewal_stored id={current.id} base={current.version}")
            return await self._flush(current.id, now)
        finally:
            answer.clear()

    async def _refused(self, current: Subscription, refused: str, now: float) -> bool:
        """Mark the record "log in again" while it still holds the refused refresh token (module docstring, step 5)."""
        _line(self.context, f"renew_refused id={current.id}")
        for _attempt in range(PUT_ATTEMPTS):
            body = await _body(self.context, current)
            try:
                if body.get("refresh_token") != refused:
                    return False
                marked = {name: value for name, value in body.items() if name not in ("token", "refresh_token")}
                marked["relogin"] = RELOGIN
                stored = await subscriptions._put(
                    self.context, current.owner, current.id, current.version, marked, request_id=None
                )
                marked.clear()
            except (CredentialUnavailable, HubTransportError, HubRefusal) as exc:
                _line(self.context, f"relogin_failed id={current.id} error={type(exc).__name__}")
                self.retry_at[current.id] = now + RETRY_S
                return False
            finally:
                body.clear()
            if stored is not None:
                _line(self.context, f"relogin id={current.id} version={stored}")
                return True
            found = await subscriptions.fetch(self.context, current.id)
            if found is None:
                return False
            current = found
        return False


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        found = response.json()
    except ValueError:
        return {}
    return dict(found) if isinstance(found, dict) else {}


def _fields(answer: Mapping[str, Any], now: float) -> dict[str, Any] | None:
    """The token fields of a `200` answer, None when it is out of form."""
    token, refresh, expires_in = answer.get("access_token"), answer.get("refresh_token"), answer.get("expires_in")
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        return None
    if not isinstance(expires_in, int) or isinstance(expires_in, bool) or not 0 < expires_in <= MAX_EXPIRES_IN:
        return None
    found: dict[str, Any] = {"token": token, "expires_at": time_text(now + expires_in)}
    if refresh is not None:
        if not isinstance(refresh, str) or not TOKEN_RE.fullmatch(refresh):
            return None
        found["refresh_token"] = refresh
    if "scope" in answer:
        try:
            found["scopes"] = list(scopes_of(answer["scope"]))
        except LoginUnusable:
            return None
    return found
