"""Test support of the device suites (U-HUB-device-api): devices with real keys that sign their own requests.

`Fleet` wraps the running hub and the orgs test world (`tests/platform/hub/orgs/support.py`):

- `org_invite(draft, wgs)`: a device invitation of an org (the `invite` entry through the append service and the
  invitation row HUB-11 stores with it);
- `org_enroll_body(...)` / `enroll_org(...)`: the body of an org enrollment (`accept` signed by the device and `sk_inv`)
  and its submission, signed by the new device key;
- `platform_invite()` / `platform_enroll_body(...)` / `enroll_platform(...)`: a `device_invite` in the platform log
  (root-signed through `POST /v1/platform/log`) and a platform enrollment over the enrollment text;
- `client(actor)`: a client that signs with the actor's own Ed25519 key (`device.<32 hex>`), the form the device
  resolver of HUB-08 checks against the `device` row.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx

from agent_hub.auth.keys import Principal
from agent_hub.crypto import enroll as enroll_text
from agent_hub.crypto import invite as invite_format
from agent_hub.crypto.encoding import b64u_encode, format_time
from agent_hub.crypto.keys import Identity
from agent_hub.device.keys import key_id_of
from agent_hub.log.entries import entry_hash, sign_log_entry, sign_platform_entry
from tests.platform.hub.conftest import HubHarness, SignedClient, SigningKey, root_private_key
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import Actor, OrgDraft, World, new_id

Json = dict[str, Any]
CAPACITY = {
    "vm_memory_budget_mib": 65536,
    "vm_memory_used_mib": 0,
    "vm_slots_total": 8,
    "vm_slots_free": 8,
    "pool_ready": 2,
}


def new_device() -> Actor:
    """A device that has keys but is not registered at the hub yet."""
    return Actor(new_id(), "device", Identity.generate())


@dataclass
class PlatformChain:
    """The platform log as the operator builds it (root-signed entries posted through the API)."""

    hub: HubHarness
    entries: list[Json] = field(default_factory=list)

    def post(self, *bodies: Json) -> httpx.Response:
        root = root_private_key()
        signed: list[Json] = []
        prev = entry_hash(self.entries[-1]) if self.entries else ""
        for offset, body in enumerate(bodies):
            full = {
                "seq": len(self.entries) + offset,
                "prev": prev,
                "by": "platform-root",
                "ts": format_time(self.hub.clock()),
                "v": 1,
                **body,
            }
            entry = sign_platform_entry(full, root)
            signed.append(entry)
            prev = entry_hash(entry)
        response = self.hub.root.post("/v1/platform/log", json={"expected_seq": len(self.entries), "entries": signed})
        assert response.status_code == 200, response.text
        self.entries.extend(signed)
        return response

    def ensure_root(self) -> None:
        if not self.entries:
            self.post({"type": "root", "pub": b64u_encode(root_private_key().public_key().public_bytes_raw())})


class Fleet:
    """Devices, invitations and enrollments on one running hub."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.hub = world.hub
        self.platform = PlatformChain(world.hub)

    # --- clients ---

    def client(self, actor: Actor, identity: Identity | None = None) -> SignedClient:
        """A client signing with the actor's key (or with `identity`, e.g. a wrong key)."""
        keys = identity or actor.identity
        principal = Principal(
            kind="device", key_id=key_id_of(actor.id), principal_id=uuid.UUID(actor.id), actor_kind="device"
        )
        return self.hub.client(SigningKey(key_id=key_id_of(actor.id), sign=keys.sig.sign, principal=principal))

    # --- org devices ---

    def org_invite(
        self, draft: OrgDraft, wgs: dict[str, str] | None = None, *, hours: float = 24
    ) -> invite_format.InviteSecrets:
        """A device invitation of the org (log entry and invitation row)."""
        expires = format_time(self.hub.clock() + timedelta(hours=hours))
        found = draft.invite(
            draft.founder, kind="device", wgs={draft.default_wg: "device"} if wgs is None else wgs, expires=expires
        )
        draft.append(draft.founder)
        with self.hub.pool.connection() as connection, connection.transaction():
            connection.execute(
                "INSERT INTO hub.invite (id, scope, kind, org_id, log_seq, created_by, created_at, expires_at,"
                " package_ct) VALUES (%s, 'org', 'device', %s, %s, %s, %s, %s, %s)",
                (
                    found.invite_id,
                    draft.id,
                    len(draft.entries) - 1,
                    draft.founder.id,
                    self.hub.clock(),
                    self.hub.clock() + timedelta(hours=hours),
                    secrets.token_bytes(48),
                ),
            )
        return found

    def org_enroll_body(
        self, draft: OrgDraft, device: Actor, found: invite_format.InviteSecrets, **overrides: Any
    ) -> Json:
        """The body of an org enrollment: the device's `accept` (not yet added to the draft)."""
        body = draft.body("accept", device, invite_id=found.invite_id, subject=device.id, kind="device", pub=device.pub)
        body.update(overrides.pop("accept", {}))
        entry = sign_log_entry(body, device.identity.sig, overrides.pop("invite_key", found.signing))
        request = {
            "device_id": device.id,
            "kind": "org",
            "invite_id": found.invite_id,
            "pub": device.pub,
            "agent_version": "0.1.0",
            "os": "linux",
            "arch": "arm64",
            "capacity": dict(CAPACITY),
            "entry": entry,
        }
        request.update(overrides)
        return request

    def enroll_org(
        self, draft: OrgDraft, device: Actor, found: invite_format.InviteSecrets, **options: Any
    ) -> httpx.Response:
        """Submit an org enrollment; a success adds the `accept` to the draft."""
        body = self.org_enroll_body(draft, device, found)
        response = self.client(device).post("/v1/device/enroll", json=body, **options)
        if response.status_code == 200:
            draft.entries.append(body["entry"])
            draft.submitted = len(draft.entries)
        return response

    def org_device(self, draft: OrgDraft, wgs: dict[str, str] | None = None) -> Actor:
        """An enrolled org device of the draft's org."""
        device = new_device()
        response = self.enroll_org(draft, device, self.org_invite(draft, wgs))
        assert response.status_code == 200, response.text
        return device

    # --- platform devices ---

    def platform_invite(self, hours: float = 24) -> invite_format.InviteSecrets:
        """A `device_invite` in the platform log."""
        self.platform.ensure_root()
        found = invite_format.invite_secrets(invite_format.generate_code())
        expires = format_time(self.hub.clock() + timedelta(hours=hours))
        self.platform.post(
            {"type": "device_invite", "invite_id": found.invite_id, "pk_inv": found.pk_inv, "expires": expires}
        )
        return found

    def platform_enroll_body(
        self, device: Actor, found: invite_format.InviteSecrets, *, invite_key: Any = None, device_key: Any = None
    ) -> Json:
        """A platform enrollment signed over the enrollment text (by other keys when given)."""
        body = {
            "device_id": device.id,
            "kind": "platform",
            "invite_id": found.invite_id,
            "pub": device.pub,
            "agent_version": "0.1.0",
            "os": "linux",
            "arch": "arm64",
            "capacity": dict(CAPACITY),
        }
        signed = enroll_text.sign_enrollment(body, device.identity.sig, invite_key or found.signing)
        if device_key is not None:
            other = enroll_text.sign_enrollment(
                {**body, "pub": {**device.pub, "sig": b64u_encode(device_key.public_key().public_bytes_raw())}},
                device_key,
                found.signing,
            )
            signed["enroll_sig"] = other["enroll_sig"]
        return signed

    def enroll_platform(self, device: Actor, found: invite_format.InviteSecrets) -> httpx.Response:
        return self.client(device).post("/v1/device/enroll", json=self.platform_enroll_body(device, found))

    def certify(self, device: Actor, found: invite_format.InviteSecrets) -> int:
        """Certify an enrolled platform device; the `seq` of its `device_certify`."""
        request = self.platform_enroll_body(device, found)
        fields = {name: request[name] for name in ("pub", "invite_id", "enroll_sig", "inv_sig", "agent_version")}
        fields.update({name: request[name] for name in ("os", "arch", "capacity")})
        self.platform.post({"type": "device_certify", "device": device.id, **fields})
        return len(self.platform.entries) - 1

    def platform_device(self) -> tuple[Actor, int]:
        """An enrolled and certified platform device and the `seq` of its certification."""
        device = new_device()
        found = self.platform_invite()
        response = self.enroll_platform(device, found)
        assert response.status_code == 200, response.text
        return device, self.certify(device, found)

    # --- rows ---

    def rows(self, query: str, *params: object) -> list[tuple[Any, ...]]:
        return self.world.rows(query, *params)

    def execute(self, query: str, *params: object) -> None:
        with self.hub.pool.connection() as connection, connection.transaction():
            connection.execute(query, params)


def org_device_scene(world: World) -> Scene:
    """The events scene (`tests/platform/hub/events/support.py`) with its devices bound to the scene's org, as an org
    enrollment stores them (`owner_org`), so that they pass the device resolver."""
    scene = Scene.build(world)
    with world.hub.pool.connection() as connection, connection.transaction():
        connection.execute(
            "UPDATE hub.device SET owner_org = %s WHERE id = ANY(%s::uuid[])",
            (scene.org, [scene.device.id, scene.other_device.id]),
        )
    return scene
