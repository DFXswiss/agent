"""Fixtures of the platform suites (HUB-04): a platform log written through the test hub with the test root key.

`plat` keeps a local copy of the appended log and builds the next entries on its head; `enroll` writes what the
enrollment endpoint (HUB-08) stores for a platform device (a `pending_certification` device row and the
`device_enrollment` row with the signed request), so that certification can be tested before HUB-08 exists.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from psycopg.types.json import Jsonb

from agent_hub.crypto import enroll as enrollment
from agent_hub.crypto import invite as invite_codes
from agent_hub.crypto.encoding import b64u_encode, format_time
from agent_hub.crypto.keys import ed25519_public_raw, x25519_public_raw
from agent_hub.log.entries import entry_hash, sign_platform_entry
from agent_hub.platform import log as platform_log
from agent_hub.stream import publish
from tests.platform.hub.auth.authenticator import remember_invite
from tests.platform.hub.conftest import HubHarness, root_private_key, uuid7

CAPACITY = {
    "vm_memory_budget_mib": 65536,
    "vm_memory_used_mib": 0,
    "vm_slots_total": 8,
    "vm_slots_free": 8,
    "pool_ready": 2,
}


@dataclass
class Enrolled:
    """A platform device that enrolled with a device invitation (what HUB-08 stores)."""

    device_id: str
    sig: Ed25519PrivateKey
    kem: X25519PrivateKey
    invite: invite_codes.InviteSecrets
    request: dict[str, object]

    @property
    def pub(self) -> dict[str, str]:
        return {"kem": b64u_encode(x25519_public_raw(self.kem)), "sig": b64u_encode(ed25519_public_raw(self.sig))}

    def certify_fields(self) -> dict[str, object]:
        """The fields of the `device_certify` entry for this enrollment."""
        request = self.request
        return {
            "device": self.device_id,
            "pub": request["pub"],
            "invite_id": request["invite_id"],
            "enroll_sig": request["enroll_sig"],
            "inv_sig": request["inv_sig"],
            "agent_version": request["agent_version"],
            "os": request["os"],
            "arch": request["arch"],
            "capacity": request["capacity"],
        }


@dataclass
class Platform:
    """The platform log of one test hub."""

    hub: HubHarness
    root: Ed25519PrivateKey = field(default_factory=root_private_key)
    entries: list[dict[str, object]] = field(default_factory=list)

    def body(self, entry_type: str, **fields: object) -> dict[str, object]:
        """A body without `seq` and `prev` (`chain` sets them; a field given here overrides)."""
        return {"type": entry_type, "by": "platform-root", "ts": self.ts(), "v": 1, **fields}

    def ts(self) -> str:
        return format_time(self.hub.clock())

    def chain(self, *bodies: dict[str, object], signer: Ed25519PrivateKey | None = None) -> list[dict[str, object]]:
        """Sign bodies as a chain on the current head (`seq` and `prev` are set here unless the body has them)."""
        out: list[dict[str, object]] = []
        prev = entry_hash(self.entries[-1]) if self.entries else ""
        seq = len(self.entries)
        for body in bodies:
            full = {"seq": seq, "prev": prev, **body}
            entry = sign_platform_entry(full, signer or self.root)
            out.append(entry)
            prev = entry_hash(entry)
            seq += 1
        return out

    def post(self, entries: list[dict[str, object]], expected_seq: int | None = None, **kwargs: Any) -> httpx.Response:
        """`POST /v1/platform/log`; a success extends the local copy."""
        expected = len(self.entries) if expected_seq is None else expected_seq
        response = self.hub.root.post("/v1/platform/log", json={"expected_seq": expected, "entries": entries}, **kwargs)
        if response.status_code == 200:
            self.entries.extend(entries)
        return response

    def append(self, entry_type: str, **fields: object) -> int:
        """Append one entry (and `root` first while empty); its `seq`."""
        if not self.entries and entry_type != "root":
            self.init()
        body = self.body(entry_type, **fields)
        response = self.post(self.chain(body))
        assert response.status_code == 200, response.text
        return len(self.entries) - 1

    def init(self) -> None:
        response = self.post(self.chain(self.body("root", pub=b64u_encode(ed25519_public_raw(self.root)))))
        assert response.status_code == 200, response.text

    def expires(self, hours: float = 24) -> str:
        return format_time(self.hub.clock() + timedelta(hours=hours))

    def invite_device(self, hours: float = 24) -> invite_codes.InviteSecrets:
        """A `device_invite` for a new code."""
        found = invite_codes.invite_secrets(invite_codes.generate_code())
        self.append("device_invite", invite_id=found.invite_id, pk_inv=found.pk_inv, expires=self.expires(hours))
        return found

    def invite_user(self, hours: float = 72) -> str:
        """A `user_invite` for a new code (remembered for `FakeBrowser.register`)."""
        found = remember_invite(invite_codes.invite_secrets(invite_codes.generate_code()))
        self.append("user_invite", invite_id=found.invite_id, pk_inv=found.pk_inv, expires=self.expires(hours))
        return found.invite_id

    def enroll(
        self, invite: invite_codes.InviteSecrets | None = None, *, device_id: str | None = None, store: bool = True
    ) -> Enrolled:
        """A device that enrolls with `invite` (a new device invitation by default)."""
        found = invite or self.invite_device()
        sig = Ed25519PrivateKey.generate()
        kem = X25519PrivateKey.generate()
        device = device_id or str(uuid7(self.hub.clock()))
        body = {
            "device_id": device,
            "kind": "platform",
            "invite_id": found.invite_id,
            "pub": {"kem": b64u_encode(x25519_public_raw(kem)), "sig": b64u_encode(ed25519_public_raw(sig))},
            "agent_version": "0.1.0",
            "os": "macos",
            "arch": "arm64",
            "capacity": dict(CAPACITY),
        }
        signed = enrollment.sign_enrollment(body, sig, found.signing)
        enrolled = Enrolled(device_id=device, sig=sig, kem=kem, invite=found, request=signed)
        if store:
            self.store_enrollment(enrolled)
        return enrolled

    def store_enrollment(self, enrolled: Enrolled, request: dict[str, object] | None = None) -> None:
        now = self.hub.clock()
        with self.hub.pool.connection() as connection, connection.transaction():
            connection.execute(
                "INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'device', %s) ON CONFLICT DO NOTHING",
                (enrolled.device_id, now),
            )
            connection.execute(
                "INSERT INTO hub.device (id, kind, kem_pub, sig_pub, state, enrolled_at, agent_version, os, arch,"
                " capacity, updated_at) VALUES (%s, 'platform', %s, %s, 'pending_certification', %s, '0.1.0',"
                " 'macos', 'arm64', %s, %s) ON CONFLICT DO NOTHING",
                (
                    enrolled.device_id,
                    x25519_public_raw(enrolled.kem),
                    ed25519_public_raw(enrolled.sig),
                    now,
                    Jsonb(CAPACITY),
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO hub.device_enrollment (invite_id, device_id, request, created_at) VALUES (%s, %s, %s, %s)",
                (enrolled.invite.invite_id, enrolled.device_id, Jsonb(request or enrolled.request), now),
            )

    def certify(self, enrolled: Enrolled) -> int:
        return self.append("device_certify", **enrolled.certify_fields())

    def query(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
        with self.hub.pool.connection() as connection:
            return list(connection.execute(sql, params).fetchall())


@pytest.fixture(autouse=True)
def fresh_cache() -> Iterator[None]:
    """Every test starts without a cached platform log."""
    platform_log.CACHE.clear()
    yield
    platform_log.CACHE.clear()


@pytest.fixture
def published() -> Iterator[publish.MemoryPublisher]:
    """A fresh in-process publisher (the fake of `agent_hub.stream.publish` until HUB-07)."""
    fake = publish.MemoryPublisher()
    previous = publish.set_publisher(fake)
    yield fake
    publish.set_publisher(previous)


@pytest.fixture
def plat(hub: HubHarness) -> Platform:
    """An empty platform log on the test hub."""
    return Platform(hub)


def new_org(hub: HubHarness, *, private: bool = False) -> str:
    """An org row as HUB-05 creates it (head after genesis)."""
    org = uuid7(hub.clock())
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.org (id, private, platform_access, default_wg, created_by, created_at, log_head_seq,"
            " log_head_hash) VALUES (%s, %s, %s, %s, %s, %s, 0, %s)",
            (org, private, private, uuid7(hub.clock()), uuid7(hub.clock()), hub.clock(), "h" + secrets.token_hex(8)),
        )
    return str(org)


def new_user(hub: HubHarness) -> uuid.UUID:
    return uuid7(hub.clock())
