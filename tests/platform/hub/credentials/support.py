"""Test support of the credentials and requests suites (U-HUB-credentials, U-HUB-requests).

`Desk` builds on the events scene (`tests/platform/hub/events/support.py`) with its devices bound to the scene's org
(`org_device_scene`): the admin, a writer and a reader of the workgroup `wg` (each with a registered identity), two
devices assigned to `wg` (eligible for the CK of every scene user), and a stranger in another org. It adds

- `ck(user, epoch)`: the user's credential-key epochs (`scope_epoch` rows of the `cred` scope, as registration and
  rotation write them) and the 32-byte keys behind them;
- `record(...)`: a credential record sealed under the user's CK and signed by its author (user or device);
- `request(...)` / `response(...)`: device-request and response envelopes sealed and signed as browser and runner do;
- `client(device)`: a client signing with a device's own key.
"""

from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from agent_hub.crypto import aead
from agent_hub.crypto import credential as credential_format
from agent_hub.crypto import request as request_format
from agent_hub.crypto import wrap as wrap_format
from agent_hub.crypto.encoding import P, b64u_encode, canonical, format_time
from agent_hub.crypto.ids import uuid7
from agent_hub.crypto.kdf import LABEL_CREDENTIAL_SIG, credential_key
from tests.platform.hub.conftest import HubHarness, SignedClient
from tests.platform.hub.device.support import Fleet, org_device_scene
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import Actor, World

Json = dict[str, Any]
CANARY = "CANARY-hub13-7f3c9a"
"""A string that must never appear in hub storage (zero-knowledge checks)."""


@dataclass
class Desk:
    """The scene with credential keys and sealing helpers."""

    world: World
    fleet: Fleet
    scene: Scene
    keys: dict[tuple[str, int], bytes] = field(default_factory=dict)

    @classmethod
    def build(cls, world: World) -> Desk:
        scene = org_device_scene(world)
        desk = cls(world, Fleet(world), scene)
        for user in (scene.admin, scene.writer, scene.reader, scene.stranger):
            desk.ck(user, 1)
        return desk

    @property
    def hub(self) -> HubHarness:
        return self.world.hub

    def client(self, device: Actor) -> SignedClient:
        return self.fleet.client(device)

    @staticmethod
    def uuid(actor: Actor) -> uuid.UUID:
        return uuid.UUID(actor.id)

    def stranger_device(self) -> Actor:
        """An enrolled org device of another org (eligible for nobody of the scene)."""
        return self.fleet.org_device(self.world.public_org())

    def ck(self, user: Actor, epoch: int) -> bytes:
        """The user's CK of `epoch`, stored as an epoch at the hub on first use."""
        key = self.keys.get((user.id, epoch))
        if key is None:
            key = self.keys[(user.id, epoch)] = secrets.token_bytes(32)
            with self.hub.pool.connection() as connection, connection.transaction():
                connection.execute(
                    "INSERT INTO hub.scope_epoch (scope, scope_id, epoch, created_at) VALUES ('cred', %s, %s, %s)",
                    (user.id, epoch, self.hub.clock()),
                )
        return key

    def key_only(self, user: Actor, epoch: int) -> bytes:
        """A CK of `epoch` known to the browser only (a rotation not yet sent)."""
        return self.keys.setdefault((user.id, epoch), secrets.token_bytes(32))

    def record(
        self,
        owner: Actor,
        kind: str = "github",
        *,
        version: int = 1,
        epoch: int = 1,
        author: Actor | None = None,
        record_id: str | None = None,
        body: Json | None = None,
        key: bytes | None = None,
        header: Json | None = None,
    ) -> Json:
        """A sealed record; `header` overrides fields of the header (before sealing)."""
        writer = author or owner
        fields = credential_format.new_header(
            id=record_id or uuid7(),
            owner=owner.id,
            kind=kind,
            version=version,
            epoch=epoch,
            author=writer.id,
            ts=format_time(self.hub.clock()),
        )
        fields.update(header or {})
        plaintext = json.dumps(body or {"token": f"{CANARY}-token", "login": CANARY}, sort_keys=True).encode()
        cred_key = key if key is not None else self.keys.get((owner.id, epoch)) or self.key_only(owner, epoch)
        if header:
            return _seal_unchecked(cred_key, fields, plaintext, writer)
        return credential_format.seal_credential(cred_key, fields, plaintext, writer.identity.sig)

    def put(self, owner: Actor, record: Json, expected: int, kind: str | None = None, **options: Any) -> httpx.Response:
        found_kind = kind or record["header"]["kind"]
        return owner.browser.put(
            f"/v1/me/credentials/{found_kind}", json={"record": record, "expected_version": expected}, **options
        )

    def stored(self, owner: Actor, kind: str = "github") -> list[tuple[Any, ...]]:
        return self.world.rows(
            "SELECT id, version, epoch, header_text, ct, sig FROM hub.credential WHERE owner_id = %s AND kind = %s",
            owner.id,
            kind,
        )

    def wrap(self, wrapper: Actor, owner: Actor, epoch: int, recipient: Actor) -> Json:
        """A `cred` wrap of the owner's CK of `epoch` for `recipient`, signed by `wrapper`."""
        sealed = wrap_format.wrap_key(
            recipient.identity.kem_public(),
            scope="cred",
            scope_id=owner.id,
            epoch=epoch,
            recipient=recipient.id,
            key=self.key_only(owner, epoch),
        )
        signature = wrap_format.sign_wrap(
            wrapper.identity.sig, scope="cred", scope_id=owner.id, epoch=epoch, recipient=recipient.id, wrap=sealed
        )
        return {
            "scope": "cred",
            "scope_id": owner.id,
            "epoch": epoch,
            "recipient_id": recipient.id,
            "wrap": sealed,
            "sig": signature,
        }

    # --- device requests ---

    def request(
        self,
        user: Actor,
        device: Actor,
        kind: str = "github.connect",
        *,
        follows: str = "",
        request_id: str | None = None,
        signer: Actor | None = None,
        body: Json | None = None,
        **header: Any,
    ) -> Json:
        fields: Json = {
            "v": 1,
            "id": request_id or uuid7(),
            "user": user.id,
            "device": device.id,
            "kind": kind,
            "follows": follows,
            "ts": format_time(self.hub.clock()),
            "enc_alg": request_format.ENC_ALG,
        }
        fields.update(header)
        plaintext = json.dumps(body or {"note": CANARY}).encode()
        return request_format.seal_request(
            fields, device.identity.kem_public(), plaintext, (signer or user).identity.sig
        )

    def response(
        self,
        device: Actor,
        user: Actor,
        request_id: str,
        kind: str = "github.connect",
        *,
        seq: int = 1,
        final: bool = False,
        body: Json | None = None,
        header: Json | None = None,
    ) -> Json:
        fields: Json = {
            "v": 1,
            "id": uuid7(),
            "user": user.id,
            "device": device.id,
            "kind": kind,
            "in_reply_to": request_id,
            "seq": seq,
            "final": final,
            "ts": format_time(self.hub.clock()),
            "enc_alg": request_format.ENC_ALG,
        }
        fields.update(header or {})
        plaintext = json.dumps(body or {"step": "device_code", "user_code": CANARY}).encode()
        return request_format.seal_response(fields, user.identity.kem_public(), plaintext, device.identity.sig)

    def send(self, user: Actor, envelope: Json, **options: Any) -> httpx.Response:
        return user.browser.post("/v1/device-requests", json={"envelope": envelope}, **options)

    def state(self, request_id: str) -> str:
        return str(self.world.rows("SELECT state FROM hub.device_request WHERE id = %s", request_id)[0][0])

    def audit(self, action: str) -> list[tuple[Any, ...]]:
        return self.world.rows(
            "SELECT actor_kind, actor_id, device_id, target_kind, target_id, detail FROM hub.audit WHERE action = %s"
            " ORDER BY seq",
            action,
        )


def _seal_unchecked(cred_key: bytes, fields: Json, plaintext: bytes, writer: Actor) -> Json:
    """Seal a record whose header breaks the field rules (for refusal tests): seal a valid one, then sign the edited
    header again so that only the edited field is wrong."""
    salt, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
    header = {**fields, "salt": b64u_encode(salt), "nonce": b64u_encode(nonce)}
    ct = aead.seal(credential_key(cred_key, salt), nonce, plaintext, canonical(header))
    sig = writer.identity.sig.sign(P + LABEL_CREDENTIAL_SIG + canonical(header) + ct)
    return {"header": header, "ct": b64u_encode(ct), "sig": b64u_encode(sig)}


def database_dump(desk: Desk) -> list[str]:
    """Every row of every table of the hub schema as text (a full database dump for content checks)."""
    tables = desk.world.rows(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'hub' ORDER BY table_name"
    )
    return [str(text) for (table,) in tables for (text,) in desk.world.rows(f"SELECT t::text FROM hub.{table} t")]
