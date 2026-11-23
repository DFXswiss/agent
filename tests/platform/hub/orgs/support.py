"""Test support of the orgs and wraps suites: actors with real identities, signed log batches, wraps and events.

`World` wraps the running hub (`tests/platform/hub/conftest.py`): users get a registered identity (`user_identity`) and
a browser session, devices a `device` row with their own keys; `OrgDraft` builds the entries of one org (genesis,
batches), the wraps of its scope keys and its events, and submits them through the API (`post`) or the append service
(`append`, for entry types the browser route refuses).
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from agent_hub.crypto import enroll
from agent_hub.crypto import event as event_format
from agent_hub.crypto import invite as invite_format
from agent_hub.crypto import wrap as wrap_format
from agent_hub.crypto.encoding import b64u_encode, body_json, canonical, format_time
from agent_hub.crypto.ids import uuid7
from agent_hub.crypto.keys import Identity
from agent_hub.log.entries import entry_hash, sign_log_entry, sign_platform_entry
from agent_hub.orgs.append import Appender, AppendResult, append_batch
from tests.platform.hub.conftest import HubHarness, SignedClient, root_private_key

Json = dict[str, Any]
Pool = ConnectionPool[psycopg.Connection[Any]]


def new_id() -> str:
    """A fresh UUIDv7 text."""
    return uuid7()


def new_invite_id() -> str:
    """A fresh invitation ID (derived from a random code)."""
    return invite_format.invite_secrets(secrets.token_bytes(16)).invite_id


@dataclass
class Actor:
    """A user or device with its identity; users have a browser client."""

    id: str
    kind: str
    identity: Identity
    client: SignedClient | None = None

    @property
    def pub(self) -> dict[str, str]:
        return self.identity.public()

    @property
    def appender(self) -> Appender:
        return Appender(self.id, self.kind)

    @property
    def browser(self) -> SignedClient:
        assert self.client is not None
        return self.client


def insert_identity(pool: Pool, user_id: str, identity: Identity, now: datetime) -> None:
    """The registered identity of a user (02-data-model.md section 2.1)."""
    with pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'user', %s) ON CONFLICT DO NOTHING",
            (user_id, now),
        )
        connection.execute(
            "INSERT INTO hub.user_identity (user_id, kem_pub, sig_pub, recovery_id, created_at)"
            " VALUES (%s, %s, %s, %s, %s)",
            (user_id, identity.kem_public(), identity.sig_public(), secrets.token_urlsafe(16), now),
        )


def insert_device(pool: Pool, device_id: str, identity: Identity, now: datetime, kind: str = "org") -> None:
    """A device principal with its own keys."""
    with pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'device', %s)", (device_id, now)
        )
        connection.execute(
            "INSERT INTO hub.device (id, kind, kem_pub, sig_pub, owner_org, state, enrolled_at, agent_version, os,"
            " arch, capacity, updated_at) VALUES (%s, %s, %s, %s, %s, 'active', %s, '0.1.0', 'macos', 'arm64', %s, %s)",
            (
                device_id,
                kind,
                identity.kem_public(),
                identity.sig_public(),
                new_id() if kind == "org" else None,
                now,
                Jsonb({}),
                now,
            ),
        )


def insert_platform_entries(pool: Pool, entries: Sequence[Json], now: datetime, first: int = 0) -> None:
    """Platform-log entries as HUB-04 stores them (`platform_entry`)."""
    with pool.connection() as connection, connection.transaction():
        for offset, entry in enumerate(entries):
            body = entry["body"]
            connection.execute(
                "INSERT INTO hub.platform_entry (seq, entry_text, entry_hash, type, subject_id, received_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (first + offset, canonical(entry).decode("ascii"), entry_hash(entry), body["type"], None, now),
            )


def insert_invite(
    pool: Pool,
    invite_id: str,
    now: datetime,
    *,
    scope: str = "platform",
    kind: str = "user",
    org_id: str | None = None,
    registered_user: str | None = None,
    used: bool = False,
    revoked: bool = False,
) -> None:
    """An invitation row (platform invitations come from HUB-04, org invitations from HUB-11)."""
    with pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.invite (id, scope, kind, org_id, created_at, expires_at, registered_user, registered_at,"
            " used_at, revoked_at, package_ct) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                invite_id,
                scope,
                kind,
                org_id,
                now,
                now + timedelta(hours=72),
                registered_user,
                now if registered_user else None,
                now if used else None,
                now if revoked else None,
                b"package" if scope == "org" else None,
            ),
        )


class PlatformLog:
    """A platform log signed by the test platform root (`tests/platform/hub/conftest.py`)."""

    def __init__(self) -> None:
        self.entries: list[Json] = []
        self.root = root_private_key()

    def add(self, entry_type: str, /, **fields: object) -> Json:
        seq = len(self.entries)
        prev = entry_hash(self.entries[-1]) if self.entries else ""
        body = {
            "seq": seq,
            "prev": prev,
            "type": entry_type,
            "by": "platform-root",
            "ts": "2026-11-21T12:00:00.000Z",
            "v": 1,
            **fields,
        }
        entry = sign_platform_entry(body, self.root)
        self.entries.append(entry)
        return entry

    def certify(self, device: Actor) -> int:
        """`device_invite` + `device_certify` of a device; the `seq` of the certification."""
        found = invite_format.invite_secrets(secrets.token_bytes(16))
        if not self.entries:
            self.add("root", pub=b64u_encode(self.root.public_key().public_bytes_raw()))
        self.add("device_invite", invite_id=found.invite_id, pk_inv=found.pk_inv, expires="2026-12-21T12:00:00.000Z")
        capacity = {
            "vm_memory_budget_mib": 1,
            "vm_memory_used_mib": 0,
            "vm_slots_total": 1,
            "vm_slots_free": 1,
            "pool_ready": 0,
        }
        body = {
            "device_id": device.id,
            "kind": "platform",
            "invite_id": found.invite_id,
            "pub": device.pub,
            "agent_version": "0.1.0",
            "os": "macos",
            "arch": "arm64",
            "capacity": capacity,
        }
        signed = enroll.sign_enrollment(body, device.identity.sig, found.signing)
        self.add(
            "device_certify",
            device=device.id,
            pub=device.pub,
            invite_id=found.invite_id,
            enroll_sig=signed["enroll_sig"],
            inv_sig=signed["inv_sig"],
            agent_version="0.1.0",
            os="macos",
            arch="arm64",
            capacity=capacity,
        )
        return len(self.entries) - 1


@dataclass
class OrgDraft:
    """The entries, keys and events of one org as its clients build them."""

    world: World
    founder: Actor
    private: bool = False
    id: str = field(default_factory=new_id)
    default_wg: str = field(default_factory=new_id)
    entries: list[Json] = field(default_factory=list)
    keys: dict[tuple[str, str, int], bytes] = field(default_factory=dict)
    submitted: int = 0

    # --- entries ---

    def body(self, entry_type: str, by: Actor, **fields: object) -> Json:
        seq = len(self.entries)
        return {
            "org": self.id,
            "seq": seq,
            "prev": entry_hash(self.entries[-1]) if self.entries else "",
            "type": entry_type,
            "by": by.id,
            "ts": format_time(self.world.hub.clock()),
            "v": 1,
            **fields,
        }

    def add(self, entry_type: str, by: Actor, *, signer: Identity | None = None, **fields: object) -> Json:
        entry = sign_log_entry(self.body(entry_type, by, **fields), (signer or by.identity).sig)
        self.entries.append(entry)
        return entry

    def genesis(self) -> Json:
        return self.add(
            "genesis",
            self.founder,
            subject=self.founder.id,
            pub=self.founder.pub,
            default_wg=self.default_wg,
            private=self.private,
        )

    def invite(
        self,
        by: Actor,
        kind: str = "user",
        role: str = "member",
        wgs: Mapping[str, str] | None = None,
        expires: str = "2026-11-24T12:00:00.000Z",
    ) -> invite_format.InviteSecrets:
        found = invite_format.invite_secrets(secrets.token_bytes(16))
        fields: Json = {"invite_id": found.invite_id, "pk_inv": found.pk_inv, "kind": kind}
        if kind == "user":
            fields["role"] = role
        fields["wgs"] = dict(wgs or {})
        fields["expires"] = expires
        self.add("invite", by, **fields)
        return found

    def accept(self, actor: Actor, found: invite_format.InviteSecrets, identity: Identity | None = None) -> Json:
        """`accept` of `actor` with its own keys (or the keys of `identity`), signed by them and by `sk_inv`."""
        keys = identity or actor.identity
        body = self.body(
            "accept", actor, invite_id=found.invite_id, subject=actor.id, kind=actor.kind, pub=keys.public()
        )
        entry = sign_log_entry(body, keys.sig, found.signing)
        self.entries.append(entry)
        return entry

    def pending(self) -> list[Json]:
        return self.entries[self.submitted :]

    def drop_pending(self) -> None:
        del self.entries[self.submitted :]

    # --- keys, wraps, events ---

    def key(self, scope: str, scope_id: str, epoch: int) -> bytes:
        return self.keys.setdefault((scope, scope_id, epoch), secrets.token_bytes(32))

    def wrap(self, wrapper: Actor, scope: str, scope_id: str, epoch: int, recipient: Actor) -> Json:
        sealed = wrap_format.wrap_key(
            recipient.identity.kem_public(),
            scope=scope,
            scope_id=scope_id,
            epoch=epoch,
            recipient=recipient.id,
            key=self.key(scope, scope_id, epoch),
        )
        signature = wrap_format.sign_wrap(
            wrapper.identity.sig, scope=scope, scope_id=scope_id, epoch=epoch, recipient=recipient.id, wrap=sealed
        )
        return {
            "scope": scope,
            "scope_id": scope_id,
            "epoch": epoch,
            "recipient_id": recipient.id,
            "wrap": sealed,
            "sig": signature,
        }

    def wraps(self, wrapper: Actor, scope: str, scope_id: str, epoch: int, recipients: Iterable[Actor]) -> list[Json]:
        return [self.wrap(wrapper, scope, scope_id, epoch, recipient) for recipient in recipients]

    def event(
        self,
        author: Actor,
        kind: str,
        *,
        scope: str = "org",
        scope_id: str | None = None,
        epoch: int = 1,
        lh: int | None = None,
        **extra: str,
    ) -> Json:
        target = scope_id or self.id
        header = event_format.new_header(
            id=new_id(),
            org=self.id,
            scope=scope,
            scope_id=target,
            epoch=epoch,
            author=author.id,
            kind=kind,
            ts=format_time(self.world.hub.clock()),
            lh=len(self.entries) - 1 if lh is None else lh,
            **extra,
        )
        return event_format.seal_event(
            self.key(scope, target, epoch), header, body_json({"name": "Grüße"}), author.identity.sig
        )

    def profiles(self, lh: int = 0) -> list[Json]:
        """`org.profile`, `wg.profile` (default workgroup) and `member.profile` of the founder."""
        founder = self.founder
        return [
            self.event(founder, "org.profile", lh=lh),
            self.event(founder, "wg.profile", lh=lh, wg=self.default_wg),
            self.event(founder, "member.profile", lh=lh, ref=founder.id),
        ]

    # --- submission ---

    def create_body(self, platform_invite_id: str | None = None) -> Json:
        if not self.entries:
            self.genesis()
        body: Json = {
            "genesis": self.entries[0],
            "wraps": [
                self.wrap(self.founder, "org", self.id, 1, self.founder),
                self.wrap(self.founder, "wg", self.default_wg, 1, self.founder),
            ],
            "events": self.profiles(),
        }
        if platform_invite_id is not None:
            body["platform_invite_id"] = platform_invite_id
        return body

    def create(self, platform_invite_id: str | None = None, client: SignedClient | None = None) -> httpx.Response:
        response = (client or self.founder.browser).post("/v1/orgs", json=self.create_body(platform_invite_id))
        if response.status_code == 200:
            self.submitted = 1
        return response

    def post(
        self,
        by: Actor,
        wraps: Sequence[Json] = (),
        events: Sequence[Json] = (),
        *,
        expected_seq: int | None = None,
        client: SignedClient | None = None,
    ) -> httpx.Response:
        """Submit the pending entries through `POST /v1/orgs/{org}/log`."""
        body = {
            "expected_seq": self.submitted if expected_seq is None else expected_seq,
            "entries": self.pending(),
            "wraps": list(wraps),
            "events": list(events),
        }
        response = (client or by.browser).post(f"/v1/orgs/{self.id}/log", json=body)
        if response.status_code == 200:
            self.submitted = len(self.entries)
        return response

    def append(
        self, by: Actor, wraps: Sequence[Json] = (), events: Sequence[Json] = (), *, now: datetime | None = None
    ) -> AppendResult:
        """Submit the pending entries through the append service (any entry type)."""
        with self.world.hub.pool.connection() as connection, connection.transaction():
            result = append_batch(
                connection,
                org_id=uuid.UUID(self.id),
                appender=by.appender,
                expected_seq=self.submitted,
                entries=self.pending(),
                wraps=list(wraps),
                events=list(events),
                now=now or self.world.hub.clock(),
                root_key=self.world.hub.config.platform_root_key,
            )
        self.submitted = len(self.entries)
        return result


class World:
    """Actors and orgs on one running hub."""

    def __init__(self, hub: HubHarness) -> None:
        self.hub = hub

    def user(self, *, registered: bool = True) -> Actor:
        user_id = new_id()
        identity = Identity.generate()
        client = self.hub.browser(uuid.UUID(user_id))
        if registered:
            insert_identity(self.hub.pool, user_id, identity, self.hub.clock())
        return Actor(user_id, "user", identity, client)

    def device(self, kind: str = "org") -> Actor:
        device_id = new_id()
        identity = Identity.generate()
        insert_device(self.hub.pool, device_id, identity, self.hub.clock(), kind)
        return Actor(device_id, "device", identity)

    def org(self, founder: Actor | None = None, *, private: bool = False) -> OrgDraft:
        return OrgDraft(self, founder or self.user(), private=private)

    def platform_invite(self, user: Actor, **options: Any) -> str:
        invite_id = new_invite_id()
        insert_invite(self.hub.pool, invite_id, self.hub.clock(), registered_user=user.id, **options)
        return invite_id

    def private_org(self, founder: Actor | None = None) -> OrgDraft:
        """A created private org (platform access)."""
        draft = self.org(founder, private=True)
        response = draft.create(self.platform_invite(draft.founder))
        assert response.status_code == 200, response.text
        return draft

    def public_org(self, founder: Actor | None = None) -> OrgDraft:
        """A created non-private org."""
        draft = self.org(founder)
        response = draft.create()
        assert response.status_code == 200, response.text
        return draft

    def rows(self, query: str, *params: object) -> list[tuple[Any, ...]]:
        with self.hub.pool.connection() as connection:
            return connection.execute(query, params).fetchall()

    def join(
        self, draft: OrgDraft, actor: Actor, *, role: str = "member", wgs: Mapping[str, str] | None = None
    ) -> None:
        """Invite (service) and accept: `actor` becomes a member (users) or an assigned device."""
        found = draft.invite(draft.founder, kind=actor.kind, role=role, wgs=wgs)
        draft.append(draft.founder)
        draft.accept(actor, found)
        draft.append(actor)
