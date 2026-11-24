"""Test support of the GitHub identities suite (U-HUB-github, ADR-076).

`Studio` builds on the credentials desk (`tests/platform/hub/credentials/support.py`: an org with `admin`, `writer`
(write on `wg`), `reader` (read on `wg`), the org devices `device` and `other_device` assigned to `wg`, and a
`stranger` in another org; each user with a `CK` of epoch 1). It adds

- `personal(...)` / `put(...)`: a user's identity record sealed under the user's `CK` and its `PUT`;
- `company(...)` / `put_company(...)`: a company record of the scene's org sealed under a fresh epoch of the org
  credential key and the `key {epoch, wraps}` the admin sends with it (wraps for every reader device, signed by the
  writing admin, KM-35);
- `announce(...)`: the feature tokens a runner announced on its live channel (`device.features`);
- `raw_identity(...)`: a `github` row written directly (for the checks that do not open records).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

import httpx

from agent_hub.crypto import wrap as wrap_format
from agent_hub.crypto.ids import uuid7
from agent_hub.device.features import GITHUB_IDENTITIES
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.credentials.support import Desk, Json
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import Actor

NO_CONTRACT = {"contract": False}


@dataclass
class Studio:
    """The credentials desk plus identity helpers (module docstring)."""

    desk: Desk
    org_keys: dict[int, bytes] = field(default_factory=dict)

    @property
    def scene(self) -> Scene:
        return self.desk.scene

    @property
    def hub(self) -> HubHarness:
        return self.desk.hub

    @property
    def org(self) -> str:
        return self.scene.org

    @property
    def org_actor(self) -> Actor:
        """The org as the owner of a record (its ID; the identity is unused)."""
        return Actor(self.org, "org", self.scene.admin.identity)

    def rows(self, query: str, *params: object) -> list[tuple[Any, ...]]:
        return self.desk.world.rows(query, *params)

    def execute(self, query: str, *params: object) -> None:
        with self.hub.pool.connection() as connection, connection.transaction():
            connection.execute(query, params)

    def promote(self, user: Actor) -> Actor:
        """Make a member an admin of the org (log entry `role`); the scene's admin reads `wg` by inheritance only, so
        the devices of `wg` are not eligible for its `CK` and it cannot address them with device requests."""
        draft = self.scene.draft
        draft.add("role", self.scene.admin, subject=user.id, role="admin")
        response = draft.post(self.scene.admin)
        assert response.status_code == 200, response.text
        return user

    def announce(self, *devices: Actor, features: tuple[str, ...] = (GITHUB_IDENTITIES,)) -> None:
        for device in devices or (self.scene.device, self.scene.other_device):
            self.execute("UPDATE hub.device SET features = %s WHERE id = %s", list(features), device.id)

    # --- personal records ---

    def personal(
        self,
        owner: Actor,
        *,
        record_id: str | None = None,
        version: int = 1,
        epoch: int = 1,
        author: Actor | None = None,
    ) -> Json:
        return self.desk.record(owner, record_id=record_id or uuid7(), version=version, epoch=epoch, author=author)

    def put(
        self,
        caller: Actor,
        record: Json,
        expected: int,
        *,
        owner_id: str | None = None,
        key: Json | None = None,
        path_id: str | None = None,
        **options: Any,
    ) -> httpx.Response:
        body: Json = {"owner_id": owner_id or record["header"]["owner"], "record": record, "expected_version": expected}
        if key is not None:
            body["key"] = key
        return caller.browser.put(f"/v1/github-identities/{path_id or record['header']['id']}", json=body, **options)

    def created(self, owner: Actor, **options: Any) -> Json:
        """A stored personal record of `owner` (version 1)."""
        record = self.personal(owner, **options)
        response = self.put(owner, record, 0)
        assert response.status_code == 200, response.text
        return record

    # --- company records ---

    def org_epoch(self) -> int:
        found = self.rows("SELECT max(epoch) FROM hub.scope_epoch WHERE scope = 'cred' AND scope_id = %s", self.org)
        return int(found[0][0] or 0)

    def org_key(self, epoch: int) -> bytes:
        return self.org_keys.setdefault(epoch, secrets.token_bytes(32))

    def wrap(self, wrapper: Actor, epoch: int, recipient: Actor, *, scope_id: str | None = None) -> Json:
        scope = scope_id or self.org
        sealed = wrap_format.wrap_key(
            recipient.identity.kem_public(),
            scope="cred",
            scope_id=scope,
            epoch=epoch,
            recipient=recipient.id,
            key=self.org_key(epoch),
        )
        signature = wrap_format.sign_wrap(
            wrapper.identity.sig, scope="cred", scope_id=scope, epoch=epoch, recipient=recipient.id, wrap=sealed
        )
        return {
            "scope": "cred",
            "scope_id": scope,
            "epoch": epoch,
            "recipient_id": recipient.id,
            "wrap": sealed,
            "sig": signature,
        }

    def key(self, epoch: int, recipients: list[Actor] | None = None, wrapper: Actor | None = None) -> Json:
        readers = [self.scene.device, self.scene.other_device] if recipients is None else recipients
        return {"epoch": epoch, "wraps": [self.wrap(wrapper or self.scene.admin, epoch, item) for item in readers]}

    def company(
        self, *, record_id: str | None = None, version: int = 1, epoch: int | None = None, author: Actor | None = None
    ) -> Json:
        found = self.org_epoch() + 1 if epoch is None else epoch
        return self.desk.record(
            self.org_actor,
            record_id=record_id or uuid7(),
            version=version,
            epoch=found,
            author=author or self.scene.admin,
            key=self.org_key(found),
        )

    def put_company(
        self, record: Json, expected: int, *, caller: Actor | None = None, key: Json | None = None, **options: Any
    ) -> httpx.Response:
        found_key = key if key is not None else self.key(record["header"]["epoch"])
        return self.put(caller or self.scene.admin, record, expected, key=found_key, **options)

    def created_company(self) -> Json:
        """A stored company record of the scene's org (version 1)."""
        record = self.company()
        response = self.put_company(record, 0)
        assert response.status_code == 200, response.text
        return record

    # --- rows ---

    def raw_identity(self, owner: str, *, owner_kind: str = "user", primary: bool = False) -> str:
        """A `github` row written directly (header and ciphertext are placeholders)."""
        record_id = uuid7()
        now = self.hub.clock()
        self.execute(
            "INSERT INTO hub.credential (id, owner_id, kind, version, epoch, header_text, ct, sig, updated_at,"
            " owner_kind, created_at, is_primary) VALUES (%s, %s, 'github', 1, 1, '{}', %s, %s, %s, %s, %s, %s)",
            record_id,
            owner,
            secrets.token_bytes(28),
            secrets.token_bytes(64),
            now,
            owner_kind,
            now,
            primary,
        )
        return record_id

    def primary_of(self, owner: Actor) -> list[str]:
        return [
            str(row[0])
            for row in self.rows(
                "SELECT id FROM hub.credential WHERE owner_id = %s AND kind = 'github' AND is_primary", owner.id
            )
        ]

    def audit(self, action: str) -> list[tuple[Any, ...]]:
        return self.desk.audit(action)
