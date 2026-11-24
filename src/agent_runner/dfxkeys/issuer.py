"""The DFX key issuer of a platform device (ADR-080 section 7; contract sheet of KOR-50).

Everything here is sent only to a hub whose device state carries `model_fallback: {api: 1}` or higher (an older hub
knows none of the items and lists no requests).

- **Announcement** (`observe`, reconcile step `dfx_keys`): the item `dfx_key_state {issuer}`; `issuer` is true while
  the management key file is configured and usable. `true` is sent once per daemon and whenever it becomes true,
  `false` only when it ends a `true` this device announced (the last announcement is kept in `sync_state`
  `dfx_key_issuer`, so a restart without the key file still withdraws it); a device that never was an issuer sends
  nothing. The hub elects the online issuer with the smallest device ID and lists the work in that device's state
  only.
- **Requests** (`dfx_keys.requests: [{request_id, org_id, device_id, device_kem}]`, task `dfx_keys`): a request this
  device answered already (`runner.dfx_key_issued`) is skipped until the hub stops listing it. Otherwise the issuer
  deletes every key named for the request (a crash between creating a key and committing its item left it behind),
  creates one key named `dfx-<request_id>` with the limit `min(headroom_usd, cap)` (the request's optional
  `headroom_usd`, else 0: the first poll of the key sets the limit the hub grants), seals it to `device_kem`
  (`model.dfxkey.seal_envelope`, signed with this device's signing key), forgets the plaintext and writes the row and
  the item `dfx_key_issued {request_id, org_id, device_id, key_ref, envelope}` in one transaction. The whole request
  ID names the key: its first 8 characters are the time part of a UUIDv7 and are shared by requests made within about
  a minute, so a shorter name would let the cleanup of one request delete another request's key.
- **Keys** (`dfx_keys.keys: [{key_ref, org_id, device_id, headroom_usd, disabled, revoke}]`, task `dfx_keys`): each
  key is read every `POLL_S` (at once when its entry changes). Its cumulative spend (`usage`) is reported as
  `dfx_key_usage {key_ref, org_id, device_id, spent_micro_usd, at}` when it differs from the last report of this
  daemon (the hub books only the difference to the highest figure it booked). Then:
  - `revoke`: the final spend is read and reported (always, with `at` = now, so after the revocation) and the key is
    deleted. The hub closes the key once it processed a report with `at` at or after the revocation; while it still
    lists the key, the issuer repeats the last known cumulative spend every `POLL_S` with a fresh `at` and calls the
    provider no more (a repeated figure books nothing). A revoked key the provider no longer knows (deleted before a
    restart, `404`) is reported the same way with the last known spend of this daemon, else 0;
  - `disabled`: the key is switched off at the provider (`PATCH disabled: true`);
  - otherwise the key is switched on and its limit set to `spent + min(headroom_usd, cap)`, with `cap` =
    `AGENT_RUNNER_DFX_KEY_MAX_HEADROOM_USD` (default 5 USD): a compromised hub cannot raise a limit beyond it.
  A malformed entry is skipped (logged); a malformed headroom counts as 0. A key the provider does not know is logged
  once per daemon (`key_missing`), not at every poll.

Log lines `runner.dfxkeys ...` carry IDs, key references (the provider's key hash, not a secret), closed reasons and
amounts, never a key, the management key or an envelope.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_runner.config import DEFAULT_DFX_KEY_MAX_HEADROOM_MICRO_USD
from agent_runner.crypto.encoding import b64u_decode
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.keys import RAW_KEY_BYTES
from agent_runner.dfxkeys.openrouter import KeyMeta, Management, ManagementError, to_micro, to_usd
from agent_runner.model.dfxkey import KEY_REF_RE, UUID_RE, EnvelopeRefused, seal_envelope
from agent_runner.model.provision import read_platform_key
from agent_runner.orgs.db import session
from agent_runner.quota.subscription import ClientFactory, default_client
from agent_runner.sessions.records import time_text
from agent_runner.sync.outbox import get_state, set_state
from agent_runner.sync.runtime import runtime

POLL_S = 60.0
STATE_KEY = "dfx_key_issuer"
DEFAULT_UPSTREAM = "https://openrouter.ai/api"
NAME_PREFIX = "dfx-"
ATTRIBUTE = "dfx_key_issuer"
REQUEST_KEYS = frozenset({"request_id", "org_id", "device_id", "device_kem"})
KEY_KEYS = frozenset({"key_ref", "org_id", "device_id", "headroom_usd", "disabled", "revoke"})


def fallback_api(desired: Mapping[str, Any] | None) -> bool:
    """The hub accepts the items of ADR-080 (`model_fallback.api >= 1`)."""
    section = None if desired is None else desired.get("model_fallback")
    value = section.get("api") if isinstance(section, Mapping) else None
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


@dataclass(frozen=True)
class Request:
    request_id: str
    org_id: str
    device_id: str
    device_kem: str
    headroom_micro: int


@dataclass(frozen=True)
class Directive:
    key_ref: str
    org_id: str
    device_id: str
    headroom_micro: int
    disabled: bool
    revoke: bool


def _uuids(raw: Mapping[str, Any], *names: str) -> bool:
    return all(isinstance(raw[name], str) and UUID_RE.fullmatch(raw[name]) for name in names)


def parse_request(raw: object) -> Request | None:
    """A request entry (an optional `headroom_usd` is the initial room), None when malformed."""
    if not isinstance(raw, Mapping) or not REQUEST_KEYS <= set(raw) <= REQUEST_KEYS | {"headroom_usd"}:
        return None
    if not _uuids(raw, "request_id", "org_id", "device_id"):
        return None
    try:
        b64u_decode(raw["device_kem"], RAW_KEY_BYTES)
    except CryptoError:
        return None
    headroom = to_micro(raw.get("headroom_usd", 0)) or 0
    return Request(raw["request_id"], raw["org_id"], raw["device_id"], raw["device_kem"], headroom)


def parse_directive(raw: object) -> Directive | None:
    """A key entry, None when malformed (a malformed `headroom_usd` is 0: fail closed)."""
    if not isinstance(raw, Mapping) or set(raw) != KEY_KEYS or not _uuids(raw, "org_id", "device_id"):
        return None
    key_ref, disabled, revoke = raw["key_ref"], raw["disabled"], raw["revoke"]
    if not isinstance(key_ref, str) or not KEY_REF_RE.fullmatch(key_ref):
        return None
    if not isinstance(disabled, bool) or not isinstance(revoke, bool):
        return None
    headroom = to_micro(raw["headroom_usd"]) or 0
    return Directive(key_ref, raw["org_id"], raw["device_id"], headroom, disabled, revoke)


def _entries(section: Mapping[str, Any], name: str) -> list[Any]:
    found = section.get(name)
    return found if isinstance(found, list) else []


@dataclass
class KeyState:
    next_at: float = 0.0
    directive: Directive | None = None
    reported: int | None = None
    revoked: bool = False
    missing: bool = False


@dataclass
class Issuer:
    context: Any
    device_id: str
    signer: Ed25519PrivateKey
    management: Management
    cap_micro: int
    wall: Callable[[], float] = time.time
    api: bool = False
    section: Mapping[str, Any] = field(default_factory=dict)
    announced: bool | None = None
    keys: dict[str, KeyState] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def line(self, text: str) -> None:
        self.context.log.write(f"runner.dfxkeys {text}\n")
        self.context.log.flush()

    async def enqueue(self, conn: Any, kind: str, body: Mapping[str, object]) -> None:
        sync = runtime(self.context)
        assert sync is not None
        await sync.enqueue(conn, kind, body)

    def notify(self) -> None:
        sync = runtime(self.context)
        assert sync is not None
        sync.pusher.notify()

    # --- the device state ------------------------------------------------------------------------------------------

    async def observe(self, desired: Mapping[str, Any]) -> None:
        """Take the issuer's part of a desired state and announce this device (module docstring)."""
        self.api = fallback_api(desired)
        found = desired.get("dfx_keys")
        self.section = found if isinstance(found, Mapping) and self.api else {}
        if not self.api:
            return
        issuer = self.management.key() is not None
        known = self.announced
        if known is None and not issuer:
            async with session(self.context.pool) as conn:
                known = await get_state(conn, STATE_KEY) == "1"
        if issuer == known:
            self.announced = issuer
            return
        async with session(self.context.pool) as conn, conn.transaction():
            await self.enqueue(conn, "dfx_key_state", {"issuer": issuer})
            await set_state(conn, STATE_KEY, "1" if issuer else "0")
        self.notify()
        self.announced = issuer
        self.line(f"announced issuer={str(issuer).lower()}")

    # --- the work with the provider -------------------------------------------------------------------------------

    async def tick(self, now: float) -> None:
        """Answer the listed requests and poll the listed keys that are due."""
        async with self.lock:
            if not self.api or self.announced is not True:
                return
            await self.requests(now)
            await self.poll_keys(now)

    async def requests(self, now: float) -> None:
        listed = [found for found in map(parse_request, _entries(self.section, "requests")) if found is not None]
        skipped = len(_entries(self.section, "requests")) - len(listed)
        if skipped:
            self.line(f"requests_invalid count={skipped}")
        async with session(self.context.pool) as conn:
            cursor = await conn.execute("SELECT request_id::text FROM runner.dfx_key_issued")
            issued = {str(row[0]) for row in await cursor.fetchall()}
            gone = sorted(issued - {request.request_id for request in listed})
            if gone:
                await conn.execute("DELETE FROM runner.dfx_key_issued WHERE request_id = ANY(%s::uuid[])", (gone,))
        for request in listed:
            if request.request_id not in issued:
                await self.issue(request, now)

    async def issue(self, request: Request, now: float) -> None:
        name = NAME_PREFIX + request.request_id
        try:
            for orphan in await self.management.named(name):
                await self.management.delete(orphan.hash)
                self.line(f"orphan_deleted request_id={request.request_id} key_ref={orphan.hash}")
            meta, secret = await self.management.create(name, min(request.headroom_micro, self.cap_micro))
        except ManagementError as exc:
            self.line(f"issue_failed request_id={request.request_id} reason={exc.reason}")
            return
        header = {
            "request_id": request.request_id,
            "org_id": request.org_id,
            "device_id": request.device_id,
            "device_kem": request.device_kem,
            "key_ref": meta.hash,
            "issued_at": time_text(now),
            "issuer_device_id": self.device_id,
        }
        try:
            envelope = seal_envelope(header, secret, self.signer)
        except (EnvelopeRefused, CryptoError):
            # an unusable KEM key (a small-order point) or a key that is no printable token (at most 4096
            # characters, so the envelope stays below `MAX_ENVELOPE`)
            envelope = ""
        del secret
        if not envelope:
            await self.discard(meta.hash, request.request_id)
            return
        body = {
            "request_id": request.request_id,
            "org_id": request.org_id,
            "device_id": request.device_id,
            "key_ref": meta.hash,
            "envelope": envelope,
        }
        async with session(self.context.pool) as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO runner.dfx_key_issued (request_id, org_id, device_id, key_ref) VALUES (%s, %s, %s, %s)",
                (request.request_id, request.org_id, request.device_id, meta.hash),
            )
            await self.enqueue(conn, "dfx_key_issued", body)
        self.notify()
        self.line(f"issued request_id={request.request_id} key_ref={meta.hash} device_id={request.device_id}")

    async def discard(self, key_ref: str, request_id: str) -> None:
        """A created key that cannot be sealed (an unusable KEM key or key) is deleted again."""
        self.line(f"issue_failed request_id={request_id} reason=seal")
        try:
            await self.management.delete(key_ref)
        except ManagementError as exc:
            self.line(f"delete_failed key_ref={key_ref} reason={exc.reason}")

    async def poll_keys(self, now: float) -> None:
        listed = [found for found in map(parse_directive, _entries(self.section, "keys")) if found is not None]
        skipped = len(_entries(self.section, "keys")) - len(listed)
        if skipped:
            self.line(f"keys_invalid count={skipped}")
        for key_ref in set(self.keys) - {directive.key_ref for directive in listed}:
            del self.keys[key_ref]
        for directive in listed:
            state = self.keys.setdefault(directive.key_ref, KeyState())
            if now < state.next_at and (state.revoked or state.directive == directive):
                continue
            state.next_at = now + POLL_S
            if state.revoked:
                await self.report(directive, state.reported or 0, now)
                continue
            state.directive = directive
            await self.poll(directive, state, now)

    async def poll(self, directive: Directive, state: KeyState, now: float) -> None:
        key_ref = directive.key_ref
        try:
            meta = await self.management.get(key_ref)
        except ManagementError as exc:
            self.line(f"poll_failed key_ref={key_ref} reason={exc.reason}")
            return
        if meta is None:
            if not state.missing:
                self.line(f"key_missing key_ref={key_ref}")
            state.missing = True
            if directive.revoke:
                await self.report(directive, state.reported or 0, now)
                state.revoked = True
            return
        state.missing = False
        if meta.usage_micro != state.reported or directive.revoke:
            await self.report(directive, meta.usage_micro, now)
            state.reported = meta.usage_micro
        try:
            if directive.revoke:
                await self.management.delete(key_ref)
                state.revoked = True
                self.line(f"revoked key_ref={key_ref} spent_micro_usd={meta.usage_micro}")
                return
            await self.adjust(directive, meta)
        except ManagementError as exc:
            self.line(f"update_failed key_ref={key_ref} reason={exc.reason}")

    async def adjust(self, directive: Directive, meta: KeyMeta) -> None:
        changes: dict[str, Any] = {}
        if meta.disabled != directive.disabled:
            changes["disabled"] = directive.disabled
        limit = meta.usage_micro + min(directive.headroom_micro, self.cap_micro)
        if not directive.disabled and meta.limit_micro != limit:
            changes["limit"] = to_usd(limit)
        if not changes:
            return
        await self.management.patch(directive.key_ref, changes)
        shown = " ".join(f"{name}={str(value).lower()}" for name, value in sorted(changes.items()))
        self.line(f"updated key_ref={directive.key_ref} {shown}")

    async def report(self, directive: Directive, spent: int, now: float) -> None:
        body = {
            "key_ref": directive.key_ref,
            "org_id": directive.org_id,
            "device_id": directive.device_id,
            "spent_micro_usd": spent,
            "at": time_text(now),
        }
        async with session(self.context.pool) as conn, conn.transaction():
            await self.enqueue(conn, "dfx_key_usage", body)
        self.notify()


@dataclass
class IssuerOptions:
    """Test injection of the provider's HTTP client."""

    client: ClientFactory = default_client


def issuer_for(context: Any) -> Issuer | None:
    """The issuer of an enrolled platform device (built once per daemon), else None. A configuration without the
    issuer's settings (an embedding with a partial configuration) counts as not configured."""
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Issuer):
        return found
    identity = getattr(context, "identity", None)
    if identity is None or identity.device.kind != "platform":
        return None
    sync = runtime(context)
    assert sync is not None  # enrolled
    config = context.config
    options = getattr(context, "dfx_key_options", None)
    path = getattr(config, "openrouter_management_key_file", None)
    created = Issuer(
        context=context,
        device_id=identity.device.device_id,
        signer=identity.keys.signing_key(),
        management=Management(
            upstream=getattr(config, "model_upstream_openrouter", DEFAULT_UPSTREAM),
            key=lambda: read_platform_key(path),
            client=options.client if isinstance(options, IssuerOptions) else default_client,
        ),
        cap_micro=getattr(config, "dfx_key_max_headroom_micro_usd", DEFAULT_DFX_KEY_MAX_HEADROOM_MICRO_USD),
        wall=sync.wall,
    )
    setattr(context, ATTRIBUTE, created)
    return created
