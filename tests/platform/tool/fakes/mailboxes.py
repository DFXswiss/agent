"""Mailboxes in the hub fake as `agent_hub.mailboxes.routes` and `store` run them (ADR-099, 03-api.md section 4.8).
Installed by the tests of this area (`install`), not by `HubFake.start`:

- `records` by ID: `{owner_id, primary, record, use}` (`use` the plaintext mirror or None); a user's first mailbox is
  primary;
- `GET /v1/me/mailboxes`: the caller's records with their mirror (`raw_list` sent as it is instead);
- `GET /v1/mailboxes/{id}`: an own record, else `404`;
- `PUT /v1/mailboxes/{id}`: an own or a new record signed by the caller under the current `CK` epoch (`409
  epoch_stale`), at the stored version (`409 version_conflict`); `use` replaces the mirror, without it the mirror
  stays;
- `DELETE /v1/mailboxes/{id}`: an own record, else `404`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from agent_runner.crypto.credential import verify_credential
from agent_runner.crypto.errors import CryptoError

from ..hubfake import HubFake, Json, Request, now_ms_text

KIND = "mailbox"


@dataclass
class Mailboxes:
    records: dict[str, Json] = field(default_factory=dict)
    puts: list[Json] = field(default_factory=list)
    conflicts: int = 0
    """The next PUTs answered `409 version_conflict` regardless."""
    raw_list: Json | None = None
    refuse_get: bool = False
    """Every read of a record is answered `403 forbidden`."""
    refuse_delete: bool = False
    refuse_put: bool = False
    """Every PUT is answered `403 forbidden`."""


def state_of(hub: HubFake) -> Mailboxes:
    found = getattr(hub, "kor74_mailboxes", None)
    if found is None:
        found = Mailboxes()
        setattr(hub, "kor74_mailboxes", found)  # noqa: B010
    return cast(Mailboxes, found)


def add(hub: HubFake, owner: str, record: Json, use: list[str] | None = None) -> str:
    """Seed a record (as stored) with its mirror."""
    found = state_of(hub)
    primary = not any(row["owner_id"] == owner for row in found.records.values())
    record_id = str(record["header"]["id"])
    mirror = None if use is None else {"wgs": use}
    found.records[record_id] = {"owner_id": owner, "primary": primary, "record": record, "use": mirror}
    return record_id


def install(hub: HubFake) -> None:
    found = state_of(hub)

    def own(user: str, record_id: str) -> Json | None:
        row = found.records.get(record_id)
        return row if row is not None and row["owner_id"] == user else None

    def listing(request: Request, body: Any) -> tuple[int, Any]:
        if found.raw_list is not None:
            return 200, found.raw_list
        rows = [
            {
                "id": record_id,
                "owner_id": row["owner_id"],
                "primary": row["primary"],
                "version": row["record"]["header"]["version"],
                "epoch": row["record"]["header"]["epoch"],
                "created_at": now_ms_text(),
                "updated_at": now_ms_text(),
                "use": row["use"],
            }
            for record_id, row in found.records.items()
            if row["owner_id"] == request.user
        ]
        return 200, {"mailboxes": rows}

    def get(request: Request, body: Any, record_id: str) -> tuple[int, Any]:
        if found.refuse_get:
            return 403, {"error": "forbidden"}
        row = own(cast(str, request.user), record_id)
        if row is None:
            return 404, {"error": "not_found"}
        return 200, {"owner_id": row["owner_id"], "primary": row["primary"], "record": row["record"]}

    def put(request: Request, body: Json, record_id: str) -> tuple[int, Any]:  # noqa: PLR0911
        user = hub.users[cast(str, request.user)]
        found.puts.append(body)
        if found.refuse_put:
            return 403, {"error": "forbidden"}
        if found.conflicts:
            found.conflicts -= 1
            return 409, {"error": "version_conflict", "detail": {"expected": 0}}
        row = found.records.get(record_id)
        if row is not None and row["owner_id"] != user.id:
            return 403, {"error": "forbidden"}
        record = body["record"]
        header = record["header"]
        key = user.identity.sig_public() if header["author"] == user.id else hub.agents[header["author"]].sig
        try:
            checked = verify_credential(record, key)
        except CryptoError:
            return 400, {"error": "signature_invalid"}
        if (checked["owner"], checked["kind"], checked["id"]) != (user.id, KIND, record_id):
            return 400, {"error": "header_invalid"}
        if checked["epoch"] != user.cred_epoch:
            return 409, {"error": "epoch_stale"}
        version = 0 if row is None else int(row["record"]["header"]["version"])
        if body["expected_version"] != version or checked["version"] != version + 1:
            return 409, {"error": "version_conflict", "detail": {"expected": version}}
        if row is None:
            add(hub, user.id, record)
        found.records[record_id]["record"] = record
        if "use" in body:
            found.records[record_id]["use"] = body["use"]
        return 200, {"version": checked["version"]}

    def delete(request: Request, body: Any, record_id: str) -> tuple[int, Any]:
        if found.refuse_delete:
            return 403, {"error": "forbidden"}
        if own(cast(str, request.user), record_id) is None:
            return 404, {"error": "not_found"}
        del found.records[record_id]
        return 200, {}

    path = r"/v1/mailboxes/(?P<record_id>[0-9a-f-]+)"
    hub.add_route("GET", r"/v1/me/mailboxes", listing)
    hub.add_route("GET", path, get)
    hub.add_route("PUT", path, put)
    hub.add_route("DELETE", path, delete)
